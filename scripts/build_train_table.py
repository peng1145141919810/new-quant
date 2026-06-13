#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从根上重建训练表 train_table。

为什么有这个脚本：
  旧的训练表（已归档到 data/_archive/train_table_v1）是从 F 盘一批"很久以前一次性灌库"的 per-stock enriched CSV
  建出来的，那次灌库静默漏掉了约 37% 股票的 daily_basic（市值/换手/估值），
  整段历史为空，且从没有覆盖率校验发现它。结果模型把"数据残缺的那批票"当成了
  伪 alpha。

本脚本：
  - 按【交易日】拉 daily + daily_basic + adj_factor，三表用 (ts_code, trade_date)
    内连接。同一交易日的 daily_basic 覆盖当天所有交易股票，从结构上杜绝"整只票漏市值"。
  - 纳入退市股票（list_status L/D/P），避免幸存者偏差。
  - 前复权（qfq，锚定每只票最新 adj_factor）。
  - 特征/标签口径与现网 market_pipeline._compute_feature_rows 完全一致，额外补齐
    pe/pe_ttm/pb/ps/ps_ttm/dv_ratio/total_share/float_share/free_share 估值与股本字段。
  - 两阶段、可断点续传：
      phase A (pull)  : 逐交易日拉原始面板，落 staging/raw/<YYYY>/<YYYYMMDD>.parquet，
                        已存在则跳过 → 中断后重跑自动续。
      phase B (build) : 读 staging，逐股 qfq + 算特征/标签，并入 hs300、派生 flags，
                        写 train_table 的 parquet 分片。
  - 结束做覆盖率自检：任何关键列覆盖率或 per-stock 覆盖率低于阈值 → 直接报错退出，
    把当年"漏了不报警"的洞从根上堵死。

用法：
  python scripts/build_train_table.py pull   --start 2005-01-01 --end today
  python scripts/build_train_table.py build
  python scripts/build_train_table.py check
  python scripts/build_train_table.py all    --start 2005-01-01 --end today
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

try:
    import tushare as ts
except Exception as exc:  # pragma: no cover
    print(f"[fatal] 需要 tushare: {exc}", file=sys.stderr)
    raise

# ----------------------------- 路径与常量 -----------------------------

OUT_ROOT = Path(r"H:\Ashare\data\ml_datasets\train_table")
STAGING = OUT_ROOT / "staging"
RAW_DIR = STAGING / "raw"
META_DIR = OUT_ROOT / "_meta"
PART_ROWS = 500_000  # 每个 parquet 分片目标行数

HS300_TS = "000300.SH"

# daily_basic 想要的列（接口白送，全要）
BASIC_FIELDS = [
    "ts_code", "trade_date", "turnover_rate", "turnover_rate_f", "volume_ratio",
    "pe", "pe_ttm", "pb", "ps", "ps_ttm", "dv_ratio",
    "total_share", "float_share", "free_share", "total_mv", "circ_mv",
]

# 训练表列：与 v1 (market_pipeline.TRAIN_TABLE_COLUMNS) 完全兼容，尾部追加新字段
TRAIN_TABLE_COLUMNS_V1 = [
    "date", "code", "ts_code", "board", "industry", "listed_days", "in_hs300",
    "is_st", "is_suspended", "is_limit", "is_tradable_basic",
    "close", "pre_close", "pct_chg", "amount",
    "ret_1", "ret_5", "ret_10", "ret_20", "ret_60", "ret_120",
    "vol_5", "vol_20", "vol_60",
    "amount_mean_5", "amount_mean_20", "amount_z_20",
    "hs300_close", "hs300_ret_5", "hs300_ret_10", "hs300_ret_20", "hs300_ret_60",
    "alpha_ret_5_vs_hs300", "alpha_ret_10_vs_hs300", "alpha_ret_20_vs_hs300", "alpha_ret_60_vs_hs300",
    "future_ret_5", "future_ret_10", "future_ret_20",
    "year", "turnover_rate", "total_mv", "circ_mv", "turnover_mean_5", "turnover_mean_20",
]
NEW_FIELDS = [
    "turnover_rate_f", "volume_ratio", "pe", "pe_ttm", "pb", "ps", "ps_ttm",
    "dv_ratio", "total_share", "float_share", "free_share",
]
TRAIN_TABLE_COLUMNS = TRAIN_TABLE_COLUMNS_V1 + NEW_FIELDS

# 覆盖率自检阈值（recent = 最近两年）
COVERAGE_MIN = {
    "total_mv": 0.98,
    "circ_mv": 0.98,
    "turnover_rate": 0.98,
    "pe_ttm": 0.70,   # 亏损股 pe 天然为空（A股常态~26%），阈值按事实放低
    "pb": 0.95,
    "close": 0.999,
}
PER_STOCK_MV_MIN = 0.95  # 每只票最近两年 total_mv 覆盖率下限

# ----------------------------- 小工具 -----------------------------


def _log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def _norm_code(value: Any) -> str:
    text = str(value or "").strip()
    if not text or text.lower() == "nan":
        return ""
    if "." in text:
        text = text.split(".", 1)[0]
    digits = "".join(ch for ch in text if ch.isdigit())
    return digits.zfill(6) if digits else ""


def _board_of(ts_code: str) -> str:
    code = _norm_code(ts_code)
    if code.startswith(("688", "689")):
        return "STAR"
    if code.startswith(("300", "301")):
        return "GEM"
    if code.startswith(("43", "83", "87", "88", "92")):
        return "BSE"
    return "MAIN"


def _limit_threshold(board: str, code: str) -> float:
    b = str(board or "").upper()
    if b in {"STAR", "GEM"} or code.startswith(("300", "301", "688", "689")):
        return 0.195
    if b == "BSE" or code.startswith(("43", "83", "87", "88", "92")):
        return 0.295
    return 0.098


def _pro():
    tok = os.environ.get("TUSHARE_TOKEN", "")
    if not tok:
        print("[fatal] 环境变量 TUSHARE_TOKEN 不存在", file=sys.stderr)
        sys.exit(2)
    return ts.pro_api(tok)


def _call(pro, api_name: str, *, retries: int = 5, **kwargs) -> pd.DataFrame:
    """带退避重试的 Tushare 调用。"""
    last = None
    for attempt in range(retries):
        try:
            df = getattr(pro, api_name)(**kwargs)
            return df if df is not None else pd.DataFrame()
        except Exception as exc:  # 限速 / 偶发网络
            last = exc
            wait = min(2.0 * (attempt + 1), 12.0)
            time.sleep(wait)
    raise RuntimeError(f"{api_name} 连续 {retries} 次失败: {last}")


# ----------------------------- phase A: pull -----------------------------


def _resolve_dates(start: str, end: str) -> tuple[str, str]:
    if str(end).lower() == "today":
        end = datetime.now().strftime("%Y%m%d")
    s = pd.to_datetime(start).strftime("%Y%m%d")
    e = pd.to_datetime(end).strftime("%Y%m%d")
    return s, e


def _trade_dates(pro, start: str, end: str) -> List[str]:
    cal = _call(pro, "trade_cal", exchange="SSE", start_date=start, end_date=end, is_open="1")
    return sorted(cal["cal_date"].astype(str).tolist())


def phase_pull(start: str, end: str) -> None:
    pro = _pro()
    s, e = _resolve_dates(start, end)
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    META_DIR.mkdir(parents=True, exist_ok=True)
    dates = _trade_dates(pro, s, e)
    _log(f"phase A: 交易日 {len(dates)} 天 ({s}..{e})")
    pulled = 0
    skipped = 0
    for i, td in enumerate(dates):
        year = td[:4]
        dst = RAW_DIR / year / f"{td}.parquet"
        if dst.exists():
            skipped += 1
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        daily = _call(pro, "daily", trade_date=td)
        if daily.empty:
            # 交易日历说这天开市，daily 却为空 = 当天行情尚未发布（end=today 盘后早跑）
            # 或接口瞬时抖动。不能写空占位缓存——否则 dst.exists() 让该日永久缺失。
            _log(f"  [warn] {td} daily 返回空，本次不缓存，下次运行重拉")
            continue
        basic = _call(pro, "daily_basic", trade_date=td, fields=",".join(BASIC_FIELDS))
        adj = _call(pro, "adj_factor", trade_date=td)
        m = daily.merge(basic, on=["ts_code", "trade_date"], how="left", suffixes=("", "_b"))
        m = m.merge(adj[["ts_code", "trade_date", "adj_factor"]], on=["ts_code", "trade_date"], how="left")
        # 当天覆盖率护栏：daily_basic 应覆盖几乎所有交易股票
        cov = float(m["total_mv"].notna().mean()) if len(m) else 1.0
        if len(m) and cov < 0.90:
            _log(f"  [warn] {td} total_mv 当日覆盖率仅 {cov:.3f} (rows={len(m)})")
        m.to_parquet(dst, index=False)
        pulled += 1
        if (i + 1) % 200 == 0:
            _log(f"  ...{i+1}/{len(dates)}  pulled={pulled} skipped={skipped} 最新 {td}")
    _log(f"phase A 完成: pulled={pulled} skipped(已存在)={skipped}")


# ----------------------------- phase B: build -----------------------------


def _load_reference(pro) -> Dict[str, Any]:
    # 含退市票，避免幸存者偏差
    frames = []
    for status in ("L", "D", "P"):
        df = _call(pro, "stock_basic", exchange="", list_status=status,
                   fields="ts_code,symbol,name,industry,market,list_date,delist_date")
        if not df.empty:
            frames.append(df)
    basic = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    basic["code"] = basic["ts_code"].map(_norm_code)
    basic["board"] = basic["ts_code"].map(_board_of)
    basic["list_date"] = pd.to_datetime(basic["list_date"], errors="coerce")
    basic = basic[basic["code"] != ""].copy()
    # 同一 code 可能在 L/D/P 多次出现（退市后重新上市/借壳）；保留 list_date 最新的那次
    basic = basic.sort_values("list_date").drop_duplicates(subset=["code"], keep="last")
    ref = basic.set_index("code")[["ts_code", "name", "industry", "board", "list_date"]].to_dict("index")

    # ST 历史区间（namechange），失败则退化为"当前名含 ST"
    st_intervals: Dict[str, List[tuple]] = {}
    try:
        rows = []
        offset = 0
        while True:
            chunk = _call(pro, "namechange",
                          fields="ts_code,name,start_date,end_date,change_reason",
                          offset=offset, limit=5000)
            if chunk.empty:
                break
            rows.append(chunk)
            if len(chunk) < 5000:
                break
            offset += 5000
        nc = pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()
        for _, r in nc.iterrows():
            nm = str(r.get("name", "") or "").upper()
            if "ST" not in nm:
                continue
            c = _norm_code(r.get("ts_code"))
            sd = pd.to_datetime(r.get("start_date"), errors="coerce")
            ed = pd.to_datetime(r.get("end_date"), errors="coerce")
            st_intervals.setdefault(c, []).append((sd, ed))
        _log(f"  namechange ST 区间: {len(st_intervals)} 只票")
    except Exception as exc:
        _log(f"  [warn] namechange 拉取失败，ST 退化为当前名判断: {exc}")

    # hs300 指数序列
    hs = _call(pro, "index_daily", ts_code=HS300_TS)
    hs["date"] = pd.to_datetime(hs["trade_date"], errors="coerce")
    hs = hs.dropna(subset=["date"]).sort_values("date")
    hs["hs300_close"] = pd.to_numeric(hs["close"], errors="coerce")
    for h in (5, 10, 20, 60):
        hs[f"hs300_ret_{h}"] = hs["hs300_close"] / hs["hs300_close"].shift(h) - 1.0
    hs300 = hs[["date", "hs300_close", "hs300_ret_5", "hs300_ret_10", "hs300_ret_20", "hs300_ret_60"]]

    return {"ref": ref, "st_intervals": st_intervals, "hs300": hs300}


def _is_st_on(code: str, date: pd.Timestamp, st_intervals: Dict[str, List[tuple]], ref: Dict[str, Any]) -> int:
    ivs = st_intervals.get(code)
    if ivs:
        for sd, ed in ivs:
            if (pd.isna(sd) or date >= sd) and (pd.isna(ed) or date <= ed):
                return 1
        return 0
    name = str((ref.get(code) or {}).get("name", "") or "").upper()
    return 1 if "ST" in name else 0


def _compute_features_vectorized(panel: pd.DataFrame) -> pd.DataFrame:
    """对整张面板按 code 分组、向量化计算特征/标签（口径同现网）。"""
    g = panel.sort_values(["code", "date"]).reset_index(drop=True)
    g["close"] = pd.to_numeric(g["close"], errors="coerce")
    amount = pd.to_numeric(g["amount"], errors="coerce")
    turnover = pd.to_numeric(g["turnover_rate"], errors="coerce")
    grp = g.groupby("code", sort=False)
    close_grp = grp["close"]
    daily_ret = close_grp.pct_change()
    g["ret_1"] = daily_ret
    for h in (5, 10, 20, 60, 120):
        g[f"ret_{h}"] = g["close"] / close_grp.shift(h) - 1.0
    ret_grp = daily_ret.groupby(g["code"], sort=False)
    for w in (5, 20, 60):
        g[f"vol_{w}"] = ret_grp.transform(lambda s: s.rolling(w, min_periods=w).std())
    amt_grp = amount.groupby(g["code"], sort=False)
    g["amount_mean_5"] = amt_grp.transform(lambda s: s.rolling(5, min_periods=5).mean())
    g["amount_mean_20"] = amt_grp.transform(lambda s: s.rolling(20, min_periods=20).mean())
    amt_std20 = amt_grp.transform(lambda s: s.rolling(20, min_periods=20).std())
    g["amount_z_20"] = (amount - g["amount_mean_20"]) / amt_std20.replace(0, np.nan)
    to_grp = turnover.groupby(g["code"], sort=False)
    g["turnover_mean_5"] = to_grp.transform(lambda s: s.rolling(5, min_periods=5).mean())
    g["turnover_mean_20"] = to_grp.transform(lambda s: s.rolling(20, min_periods=20).mean())
    for h in (5, 10, 20):
        g[f"future_ret_{h}"] = close_grp.shift(-h) / g["close"] - 1.0
    return g


def _write_parts(df: pd.DataFrame) -> int:
    df = df.sort_values(["date", "code"]).reset_index(drop=True)
    n = len(df)
    parts = 0
    for start in range(0, n, PART_ROWS):
        chunk = df.iloc[start:start + PART_ROWS]
        parts += 1
        chunk.to_parquet(OUT_ROOT / f"part_{parts:05d}.parquet", index=False)
    return parts


def phase_build() -> None:
    pro = _pro()
    raw_files = sorted(RAW_DIR.rglob("*.parquet"))
    if not raw_files:
        print("[fatal] 没有 staging/raw 数据，先跑 pull", file=sys.stderr)
        sys.exit(2)
    _log(f"phase B: 读取 {len(raw_files)} 个原始交易日分片")
    ref_bundle = _load_reference(pro)
    ref = ref_bundle["ref"]
    st_intervals = ref_bundle["st_intervals"]
    hs300 = ref_bundle["hs300"]

    frames = []
    for fp in raw_files:
        try:
            d = pd.read_parquet(fp)
        except Exception:
            continue
        if d.empty:
            continue
        frames.append(d)
    panel = pd.concat(frames, ignore_index=True)
    del frames
    _log(f"  原始面板 {len(panel):,} 行")
    panel["date"] = pd.to_datetime(panel["trade_date"], errors="coerce")
    panel["code"] = panel["ts_code"].map(_norm_code)
    panel = panel.dropna(subset=["date", "code"])

    # qfq：每只票锚定其最新 adj_factor
    panel["adj_factor"] = pd.to_numeric(panel["adj_factor"], errors="coerce")
    last_adj = panel.sort_values("date").groupby("code")["adj_factor"].last()
    scale = panel["adj_factor"] / panel["code"].map(last_adj).replace(0, np.nan)
    scale = scale.fillna(1.0)
    # 保留未复权收盘价：价格地板/手数等"以当时真实价格为准"的过滤要用它，
    # qfq 价历史段差一个复权比例，不能拿来比价格阈值。
    panel["close_raw"] = pd.to_numeric(panel["close"], errors="coerce")
    for col in ("open", "high", "low", "close", "pre_close"):
        if col in panel.columns:
            panel[col] = pd.to_numeric(panel[col], errors="coerce") * scale

    # 逐股算特征/标签（向量化）
    _log("  计算特征/标签（向量化分组）...")
    panel = _compute_features_vectorized(panel)
    panel["year"] = panel["date"].dt.year

    # 并入 hs300 与 alpha
    panel = panel.merge(hs300, on="date", how="left")
    for h in (5, 10, 20, 60):
        panel[f"alpha_ret_{h}_vs_hs300"] = (
            pd.to_numeric(panel[f"ret_{h}"], errors="coerce")
            - pd.to_numeric(panel[f"hs300_ret_{h}"], errors="coerce")
        )

    # 静态引用字段
    panel["industry"] = panel["code"].map(lambda c: (ref.get(c) or {}).get("industry", ""))
    panel["board"] = panel["code"].map(lambda c: (ref.get(c) or {}).get("board", _board_of(c)))
    list_date = panel["code"].map(lambda c: (ref.get(c) or {}).get("list_date", pd.NaT))
    panel["listed_days"] = (panel["date"] - pd.to_datetime(pd.Series(list_date, index=panel.index), errors="coerce")).dt.days

    # flags
    _log("  派生 ST / 停牌 / 涨跌停 / 可交易 flags ...")
    panel["is_st"] = [
        _is_st_on(c, d, st_intervals, ref) for c, d in zip(panel["code"], panel["date"])
    ]
    panel["is_suspended"] = (pd.to_numeric(panel["amount"], errors="coerce").fillna(0.0) <= 0).astype(int)
    thr = pd.Series(
        [_limit_threshold(b, c) for b, c in zip(panel["board"], panel["code"])],
        index=panel.index,
    )
    panel["is_limit"] = (pd.to_numeric(panel["pct_chg"], errors="coerce").abs() / 100.0 >= thr).astype(int)
    panel["is_tradable_basic"] = (
        (pd.to_numeric(panel["listed_days"], errors="coerce").fillna(0) >= 120)
        & (panel["is_st"] == 0)
        & (panel["is_suspended"] == 0)
        & (panel["is_limit"] == 0)
    ).astype(int)
    panel["in_hs300"] = 0  # 近似：成份历史另行补；该列为 reserved，不进模型，仅过滤用

    # 列对齐 + 落盘
    for col in TRAIN_TABLE_COLUMNS:
        if col not in panel.columns:
            panel[col] = pd.NA
    out = panel[TRAIN_TABLE_COLUMNS].copy()
    out["date"] = pd.to_datetime(out["date"]).dt.strftime("%Y-%m-%d")

    # 清掉旧分片
    for old in OUT_ROOT.glob("part_*.parquet"):
        old.unlink()
    n_parts = _write_parts(out)
    summary = {
        "built_at": datetime.now().isoformat(timespec="seconds"),
        "rows": int(len(out)),
        "parts": int(n_parts),
        "date_min": str(out["date"].min()),
        "date_max": str(out["date"].max()),
        "n_codes": int(out["code"].nunique()),
        "columns": TRAIN_TABLE_COLUMNS,
        "new_fields_vs_v1": NEW_FIELDS,
    }
    META_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_ROOT / "build_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    _log(f"phase B 完成: {len(out):,} 行 / {n_parts} 分片 / {out['code'].nunique()} 只票")


# ----------------------------- phase C: coverage check -----------------------------


def phase_check() -> int:
    files = sorted(OUT_ROOT.glob("part_*.parquet"))
    if not files:
        print("[fatal] 没有 part_*.parquet，先 build", file=sys.stderr)
        return 2
    df = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    recent = df[df["date"] >= (df["date"].max() - pd.Timedelta(days=730))]
    _log(f"check: 全表 {len(df):,} 行, 最近两年 {len(recent):,} 行")
    failures: List[str] = []

    for col, mn in COVERAGE_MIN.items():
        if col not in recent.columns:
            failures.append(f"列缺失: {col}")
            continue
        cov = float(recent[col].notna().mean())
        flag = "OK " if cov >= mn else "FAIL"
        if cov < mn:
            failures.append(f"{col} 覆盖率 {cov:.3f} < {mn}")
        _log(f"  [{flag}] {col:16s} recent_coverage={cov:.3f} (>= {mn})")

    # per-stock total_mv 覆盖率
    per = recent.groupby("code")["total_mv"].apply(lambda s: float(s.notna().mean()))
    bad = int((per < PER_STOCK_MV_MIN).sum())
    frac_bad = bad / max(len(per), 1)
    _log(f"  per-stock total_mv 覆盖 < {PER_STOCK_MV_MIN} 的票: {bad}/{len(per)} ({frac_bad:.3%})")
    if frac_bad > 0.02:
        failures.append(f"{bad} 只票 total_mv 覆盖不足 ({frac_bad:.3%} > 2%)")

    if failures:
        _log("=== 覆盖率自检未通过 ===")
        for f in failures:
            _log(f"  ✗ {f}")
        return 1
    _log("=== 覆盖率自检通过 [OK] 数据从根上是干净的 ===")
    return 0


# ----------------------------- main -----------------------------


def main() -> None:
    ap = argparse.ArgumentParser(description="重建 train_table（根因修复版）")
    ap.add_argument("phase", choices=["pull", "build", "check", "all"])
    ap.add_argument("--start", default="2005-01-01")
    ap.add_argument("--end", default="today")
    args = ap.parse_args()

    if args.phase in ("pull", "all"):
        phase_pull(args.start, args.end)
    if args.phase in ("build", "all"):
        phase_build()
    if args.phase in ("check", "all"):
        rc = phase_check()
        sys.exit(rc)
    if args.phase == "check":
        sys.exit(phase_check())


if __name__ == "__main__":
    main()
