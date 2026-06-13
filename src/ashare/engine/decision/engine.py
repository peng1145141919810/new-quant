# -*- coding: utf-8 -*-
"""单遍决策引擎。

输入：候选（带 alpha 分数）、当前持仓、regime 快照、统一约束。
输出：目标权重 List[TargetPosition] —— 直接喂给 portfolio_control 落地。

与旧“仲裁塔”的根本区别：
- 旧：7-8 层各自重算 size/reduce_only/turnover，连乘后只剩 10-20%。
- 新：一个函数走完 选名 -> 定权 -> 单名封顶(min) -> regime 闸门(一次) -> 归一。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from live_execution_bridge.models import AccountState  # type: ignore
from .constraints import DecisionConstraints


# regime -> 处置档：active 正常 / caution 缩规模 / panic 只减不加
_PANIC = {"panic", "halt", "crisis"}
# risk_off 是 market_state 的默认防御档，必须按 caution 缩仓，否则会走 active 满仓到 1.0。
_CAUTION = {"caution", "defensive", "reduce", "warn", "risk_off", "riskoff", "risk-off"}


def _posture(regime: Any) -> str:
    """把 regime 快照归一成三档之一。"""
    if regime is None:
        return "active"
    if isinstance(regime, str):
        token = regime.strip().lower()
    elif isinstance(regime, dict):
        token = str(
            regime.get("posture")
            or regime.get("regime")
            or regime.get("state")
            or ""
        ).strip().lower()
    else:
        token = str(getattr(regime, "posture", "") or "").strip().lower()
    if token in _PANIC:
        return "panic"
    if token in _CAUTION:
        return "caution"
    return "active"


def _held_symbols(account_state: Optional[AccountState]) -> Dict[str, float]:
    """当前持仓 symbol -> 占净值权重。"""
    if account_state is None:
        return {}
    nav = max(float(account_state.nav()), 0.0)
    if nav <= 0:
        return {}
    out: Dict[str, float] = {}
    for pos in account_state.positions or []:
        out[str(pos.symbol)] = max(float(pos.market_value()) / nav, 0.0)
    return out


@dataclass
class DecisionResult:
    """决策输出。

    Args:
        targets: 目标仓位列表（symbol -> target_weight）。
        posture: 本轮 regime 处置档。
        notes: 决策过程的可读说明（审计用，不影响下游）。
    """

    targets: List["TargetPosition"]  # noqa: F821
    posture: str
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "posture": self.posture,
            "targets": [t.to_dict() for t in self.targets],
            "notes": list(self.notes),
        }


def _rank_candidates(candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """按 alpha 分数降序，分数缺失视为 0。"""
    def _score(item: Dict[str, Any]) -> float:
        try:
            return float(item.get("score", 0.0) or 0.0)
        except (TypeError, ValueError):
            return 0.0

    cleaned = [c for c in (candidates or []) if str(c.get("symbol", "")).strip()]
    return sorted(cleaned, key=_score, reverse=True)


def decide_target_weights(
    candidates: List[Dict[str, Any]],
    account_state: Optional[AccountState],
    regime: Any,
    constraints: DecisionConstraints,
) -> DecisionResult:
    """单遍产出目标权重。

    Args:
        candidates: 研究层候选，每项至少含 {symbol, score}，可含 raw。
        account_state: 当前账户/持仓状态，可为 None（首日空仓）。
        regime: regime 快照（str / dict / 对象皆可）。
        constraints: 统一约束（已 validated）。

    Returns:
        DecisionResult: 目标权重 + 处置档 + 说明。
    """
    # 延迟导入，避免包级循环。
    from live_execution_bridge.models import TargetPosition

    c = constraints.validated()
    posture = _posture(regime)
    notes: List[str] = [f"posture={posture}"]
    held = _held_symbols(account_state)

    ranked = _rank_candidates(candidates)

    # --- panic：只减不加。目标 = 现有持仓，权重原样保留（下游按 reduce_only 收口），
    #     不引入任何新名。
    if posture == "panic" and c.panic_only_reduce:
        notes.append("panic_only_reduce: 维持现有持仓、禁止新开仓，下游执行减仓")
        targets = [
            TargetPosition(symbol=sym, target_weight=w, score=None, raw={"hold": True})
            for sym, w in held.items()
        ]
        return DecisionResult(targets=targets, posture=posture, notes=notes)

    # --- 选名：取分数最高的 max_names 只（正分优先）。
    positive = [c0 for c0 in ranked if float(c0.get("score", 0.0) or 0.0) > 0]
    chosen = (positive or ranked)[: c.max_names]
    if not chosen:
        notes.append("无可用候选，输出空目标")
        return DecisionResult(targets=[], posture=posture, notes=notes)

    # --- 定权：按分数比例，单名封顶（取 min(single_name_cap, 比例权重)）。
    scores = [max(float(x.get("score", 0.0) or 0.0), 0.0) for x in chosen]
    total_score = sum(scores)
    n = len(chosen)
    if total_score <= 0:
        base = [1.0 / n] * n  # 分数全 0 时等权
    else:
        base = [s / total_score for s in scores]

    caps = [c.single_name_cap] * n

    # --- regime 缩放（只此一次）。
    exposure_cap = c.total_exposure_cap
    if posture == "caution":
        exposure_cap = min(exposure_cap, c.total_exposure_cap * c.caution_exposure_scale)
        notes.append(f"caution: 总仓位缩放至 {exposure_cap:.2f}")

    # 现金底线。
    exposure_cap = min(exposure_cap, 1.0 - c.cash_floor)

    # --- 在 exposure_cap 内按分数水填充。单名 cap 是绝对上限，降档敞口下
    #     换算成相对上限再填充，避免可行的减仓目标被二次打折成欠配。
    weights = _fill_to_exposure(base, caps, exposure_cap)

    # --- 去碎仓：低于 min_name_weight 的剔除，权重回填给其余名。
    targets_raw = list(zip(chosen, weights))
    kept = [(item, w) for item, w in targets_raw if w >= c.min_name_weight]
    if not kept:  # 全被 min_name_weight 砍掉时，保留最高分一只
        kept = [targets_raw[0]]
    if len(kept) < len(targets_raw):
        notes.append(f"剔除碎仓 {len(targets_raw) - len(kept)} 只（< {c.min_name_weight:.2f}）")
        kw = _fill_to_exposure(
            [1.0 / len(kept)] * len(kept),
            [c.single_name_cap] * len(kept),
            exposure_cap,
        )
        kept = [(kept[i][0], kw[i]) for i in range(len(kept))]

    targets = [
        TargetPosition(
            symbol=str(item.get("symbol")),
            target_weight=round(float(w), 6),
            score=float(item.get("score", 0.0) or 0.0),
            raw=dict(item.get("raw", {}) or {}),
        )
        for item, w in kept
    ]
    notes.append(
        f"选 {len(targets)} 只，单名上限 {c.single_name_cap:.2f}，"
        f"总仓位 {sum(t.target_weight for t in targets):.2f}"
    )
    return DecisionResult(targets=targets, posture=posture, notes=notes)


def _water_fill(base: List[float], caps: List[float]) -> List[float]:
    """把 base 权重归一并施加单名上限，溢出部分按未封顶名再分配。

    base 不必预先归一；返回的权重和 = min(1.0, sum(caps))。
    """
    n = len(base)
    if n == 0:
        return []
    total = sum(base)
    if total <= 0:
        w = [1.0 / n] * n
    else:
        w = [b / total for b in base]
    for _ in range(n + 2):  # 至多 n 轮即可收敛
        overflow = 0.0
        room_idx: List[int] = []
        for i in range(n):
            if w[i] > caps[i]:
                overflow += w[i] - caps[i]
                w[i] = caps[i]
            elif w[i] < caps[i]:
                room_idx.append(i)
        if overflow <= 1e-12 or not room_idx:
            break
        room_total = sum(caps[i] - w[i] for i in room_idx)
        if room_total <= 0:
            break
        for i in room_idx:
            share = (caps[i] - w[i]) / room_total
            w[i] += overflow * share
    return w


def _fill_to_exposure(base: List[float], caps: List[float], exposure_cap: float) -> List[float]:
    """把 exposure_cap 总仓位按 base 比例分给各名，caps 为绝对单名上限。

    把绝对 cap 换算成相对 exposure 的上限再水填充，保证单名 <= cap 且
    总和 = exposure_cap（caps 总额不足时 = sum(caps)）。
    """
    if exposure_cap <= 0:
        return [0.0] * len(base)
    rel_caps = [min(cap / exposure_cap, 1.0) for cap in caps]
    return [w * exposure_cap for w in _water_fill(base, rel_caps)]
