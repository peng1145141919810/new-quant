"""
Pre-release target adjustment: intraday proxy tradability, turnover cap, diversification, cash headroom.

Uses Tushare-derived proxy quotes/lists/ticks under `intraday_proxy/latest` (research_proxy / non-exchange truth).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Tuple

import numpy as np
import pandas as pd


def _text(value: Any) -> str:
    return str(value or "").strip()


def _norm_ts(value: Any) -> str:
    raw = _text(value).upper()
    if not raw:
        return ""
    if "." in raw:
        return raw
    if len(raw) == 6 and raw.isdigit():
        suf = "SH" if raw.startswith(("5", "6", "9")) else "SZ"
        return f"{raw}.{suf}"
    return raw


def _intraday_latest_root(config: Dict[str, Any]) -> Path:
    mp = dict(config.get("market_pipeline", {}) or {})
    raw = _text(mp.get("intraday_proxy_root"))
    if raw:
        return (Path(raw).resolve() / "latest")
    tc = _text(dict(config.get("paths", {}) or {}).get("trade_clock_root"))
    base = Path(tc).resolve() if tc else Path(__file__).resolve().parents[3] / "data" / "trade_clock"
    return (base / "intraday_proxy" / "latest").resolve()


def _load_proxy_feature_frame(config: Dict[str, Any]) -> pd.DataFrame:
    root = _intraday_latest_root(config)
    quote_path = root / "intraday_quote_snapshot.csv"
    list_path = root / "intraday_list_snapshot.csv"
    tick_path = root / "intraday_tick_summary.csv"
    if not quote_path.exists():
        return pd.DataFrame()
    try:
        quote = pd.read_csv(quote_path)
    except Exception:
        return pd.DataFrame()
    if quote.empty or "ts_code" not in quote.columns:
        return pd.DataFrame()
    quote.columns = [str(c).strip().lower() for c in quote.columns]
    quote["ts_code"] = quote["ts_code"].astype(str).str.strip().str.upper()
    bid = pd.to_numeric(quote.get("b1_p", quote.get("bid1")), errors="coerce")
    ask = pd.to_numeric(quote.get("a1_p", quote.get("ask1")), errors="coerce")
    mid = (bid + ask) / 2.0
    spread_pct = (ask - bid) / mid.replace(0, np.nan)
    quote["proxy_spread_pct"] = spread_pct.clip(lower=0.0)
    quote["proxy_top_list_hit"] = 0
    if list_path.exists():
        try:
            lst = pd.read_csv(list_path)
            if not lst.empty and "ts_code" in lst.columns:
                hot = set(lst["ts_code"].astype(str).str.upper().str.strip().tolist())
                quote["proxy_top_list_hit"] = quote["ts_code"].map(lambda x: 1 if x in hot else 0)
        except Exception:
            pass
    quote["proxy_tick_imbalance"] = np.nan
    if tick_path.exists():
        try:
            tick = pd.read_csv(tick_path)
            if not tick.empty and "ts_code" in tick.columns:
                tick.columns = [str(c).strip().lower() for c in tick.columns]
                tick["ts_code"] = tick["ts_code"].astype(str).str.strip().str.upper()
                ba = pd.to_numeric(tick.get("buy_amount"), errors="coerce").fillna(0.0)
                sa = pd.to_numeric(tick.get("sell_amount"), errors="coerce").fillna(0.0)
                tot = (ba + sa).replace(0, np.nan)
                imb = (ba - sa).abs() / tot
                tick["imb"] = imb.fillna(0.0)
                imb_map = dict(zip(tick["ts_code"].tolist(), tick["imb"].tolist()))
                quote["proxy_tick_imbalance"] = quote["ts_code"].map(lambda c: imb_map.get(c, np.nan))
        except Exception:
            pass
    return quote[["ts_code", "proxy_spread_pct", "proxy_top_list_hit", "proxy_tick_imbalance"]].copy()


def _per_symbol_multiplier(row: pd.Series, max_spread: float) -> float:
    sp = row.get("proxy_spread_pct")
    spv = float(sp) if sp is not None and not (isinstance(sp, float) and np.isnan(sp)) else None
    if spv is None or spv < 0:
        m_sp = 0.94
    else:
        cap = max(float(max_spread or 0.03), 1e-5)
        ratio = min(1.5, spv / cap)
        m_sp = float(max(0.28, 1.0 - 0.82 * ratio))
    imb = row.get("proxy_tick_imbalance")
    if imb is None or (isinstance(imb, float) and np.isnan(imb)):
        m_im = 1.0
    else:
        m_im = float(max(0.82, 1.0 - 0.35 * min(1.0, float(imb))))
    m_list = 1.045 if int(row.get("proxy_top_list_hit") or 0) == 1 else 1.0
    return float(min(1.15, max(0.25, m_sp * m_im * m_list)))


def apply_pre_release_proxy_objective(
    pos_df: pd.DataFrame,
    prev_df: pd.DataFrame,
    config: Dict[str, Any],
    rec_cfg: Dict[str, Any],
    account_ctx: Dict[str, Any],
    total_exposure_cap: float,
    single_name_cap: float,
) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    if pos_df.empty or "portfolio_weight" not in pos_df.columns:
        return pos_df, {"applied": False, "reason": "empty_or_no_weights"}

    key_col = "ts_code" if "ts_code" in pos_df.columns else ("code" if "code" in pos_df.columns else pos_df.columns[0])
    out = pos_df.copy()
    out["__ts"] = out[key_col].map(_norm_ts)

    proxy_cfg = {
        "max_spread_pct": float(rec_cfg.get("pre_release_proxy_max_spread_pct", 0.028) or 0.028),
        "turnover_enforce": bool(rec_cfg.get("pre_release_proxy_turnover_enforce", True)),
        "diversification_flatten": bool(rec_cfg.get("pre_release_proxy_diversification_flatten", True)),
        "cash_headroom_floor": float(rec_cfg.get("pre_release_proxy_cash_headroom_floor", 0.04) or 0.04),
        "hhi_trigger": float(rec_cfg.get("pre_release_proxy_hhi_trigger", 0.18) or 0.18),
        "flatten_gamma": float(rec_cfg.get("pre_release_proxy_flatten_gamma", 0.94) or 0.94),
    }
    control_cfg = dict(config.get("portfolio_control", {}) or {})
    max_turnover = float(control_cfg.get("max_daily_turnover_ratio", 0.25) or 0.25)

    feat = _load_proxy_feature_frame(config)
    summary: Dict[str, Any] = {
        "applied": True,
        "proxy_rows": int(len(feat.index)),
        "config": proxy_cfg,
        "source": "research_proxy_intraday_snapshot",
    }

    merged = out.copy()
    if feat.empty:
        merged["pre_release_proxy_mult"] = 1.0
        summary["note"] = "no_intraday_proxy_csv_falling_back_to_neutral_multipliers"
    else:
        f2 = feat.copy()
        f2["ts_code"] = f2["ts_code"].map(_norm_ts)
        f2["pre_release_proxy_mult"] = f2.apply(lambda r: _per_symbol_multiplier(r, proxy_cfg["max_spread_pct"]), axis=1)
        mult_map = dict(zip(f2["ts_code"].tolist(), f2["pre_release_proxy_mult"].tolist()))
        spread_map = dict(zip(f2["ts_code"].tolist(), pd.to_numeric(f2["proxy_spread_pct"], errors="coerce").tolist()))
        merged["pre_release_proxy_mult"] = merged["__ts"].map(lambda t: float(mult_map.get(t, 0.94)))
        merged["pre_release_proxy_spread_pct"] = merged["__ts"].map(lambda t: spread_map.get(t))

    w = pd.to_numeric(merged["portfolio_weight"], errors="coerce").fillna(0.0).values
    pm = pd.to_numeric(merged["pre_release_proxy_mult"], errors="coerce").fillna(1.0).values
    turnover_before = 0.0
    prev_map: Dict[str, float] = {}
    if prev_df is not None and not prev_df.empty and "portfolio_weight" in prev_df.columns:
        pk = "ts_code" if "ts_code" in prev_df.columns else ("code" if "code" in prev_df.columns else prev_df.columns[0])
        for _, row in prev_df.iterrows():
            prev_map[_norm_ts(row.get(pk))] = float(row.get("portfolio_weight") or 0.0)
    w_prev = np.array([prev_map.get(ts, 0.0) for ts in merged["__ts"].tolist()])
    turnover_before = float(np.abs(w - w_prev).sum())

    nav = float(account_ctx.get("nav") or 0.0)
    cash = float(account_ctx.get("cash") or 0.0)
    cash_ratio = cash / max(nav, 1e-6) if nav > 0 else 1.0
    floor = proxy_cfg["cash_headroom_floor"]
    cash_scale = 1.0 if cash_ratio >= floor else float(max(0.55, cash_ratio / max(floor, 1e-6)))
    summary["account"] = {"nav": nav, "cash": cash, "cash_ratio": round(cash_ratio, 6), "cash_scale": round(cash_scale, 6)}

    w2 = w * pm * cash_scale
    s = float(w2.sum())
    if s > 1e-12 and total_exposure_cap > 0:
        w2 = w2 * (float(total_exposure_cap) / s)
    turnover_mid = float(np.abs(w2 - w_prev).sum())

    if proxy_cfg["turnover_enforce"] and turnover_mid > max_turnover > 0:
        t = max_turnover / max(turnover_mid, 1e-12)
        w2 = w_prev + t * (w2 - w_prev)

    if proxy_cfg["diversification_flatten"] and len(w2) > 2:
        tw = float(w2.sum())
        if tw > 1e-12:
            pw = w2 / tw
            hhi = float(np.sum(pw ** 2))
            summary["hhi_before"] = round(hhi, 6)
            if hhi > proxy_cfg["hhi_trigger"]:
                pw2 = np.power(pw, proxy_cfg["flatten_gamma"])
                w2 = pw2 / max(pw2.sum(), 1e-12) * tw
                summary["diversification_flatten_applied"] = True
            else:
                summary["diversification_flatten_applied"] = False

    w2 = np.clip(w2, 0.0, float(single_name_cap))
    s2 = float(w2.sum())
    if s2 > 1e-12 and total_exposure_cap > 0 and s2 > total_exposure_cap:
        w2 = w2 * (total_exposure_cap / s2)

    turnover_after = float(np.abs(w2 - w_prev).sum())
    merged["portfolio_weight"] = w2
    merged["pre_release_cash_scale"] = cash_scale

    keep_cols = [c for c in pos_df.columns]
    extra_cols = [c for c in merged.columns if c.startswith("pre_release_")]
    out2 = merged[[c for c in keep_cols if c in merged.columns] + [c for c in extra_cols if c not in keep_cols]].copy()
    out2 = out2.drop(columns=["__ts"], errors="ignore")

    summary["turnover_l1_before"] = round(turnover_before, 6)
    summary["turnover_l1_after"] = round(turnover_after, 6)
    summary["max_daily_turnover_ratio"] = max_turnover
    return out2, summary
