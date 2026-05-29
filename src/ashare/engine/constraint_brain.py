# -*- coding: utf-8 -*-
"""
ConstraintBrain — 智能约束中枢

设计原则
--------
替换 OPERATOR_CONSTRAINT_MODE（一个蠢拨盘）以及 execution_manager 里
串行叠加的四层 _apply_*_overrides 链。

各信号源独立评估，汇聚到一个中枢后做统一决策：
  - 不是每个信号各自独立拦截
  - 不是全松或全堵的二元开关
  - 而是：读所有上下文 → 多维评分 → 最小必要响应

Verdict 含义
-----------
proceed           所有信号绿灯，正常执行
proceed_degraded  有警告信号，降低换手/仓位规模后继续
reduce_only       风险信号达到阈值，只允许减仓
defer             时序/释放条件不满足，跳过本次（不是 HALT，稍后重试）
block             真正的安全紧急情况（系统 HALT、市场熔断、broker 完全失联且持仓不为零）

Hard-block 触发条件（极少情况）
  1. 系统已处于 HALT 状态
  2. 市场涨跌停率超过 panic 阈值（熔断级别）
  3. broker 完全无法联通 + 当前持仓 > 0（不是空仓启动）

其余所有情况产生梯度降级，不硬拦。

输出的 config_overrides 是一份合并好的 dict，在 execution_manager 里
一次性应用，替换原来的 4 次串行 deepcopy+merge。
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .outer_intelligence import arbitrate_execution

# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

VERDICTS = ("proceed", "proceed_degraded", "reduce_only", "defer", "block")


@dataclass
class SignalDimension:
    name: str
    score: float           # 0.0 = 完全阻断, 1.0 = 完全绿灯
    verdict: str           # "ok" / "caution" / "warning" / "block"
    reason: str
    can_bypass_in_sim: bool = True   # simulation 模式下是否豁免
    is_hard_blocker: bool = False    # True → 直接触发 BLOCK，不被投票压制


@dataclass
class ConstraintDecision:
    verdict: str                              # proceed / proceed_degraded / reduce_only / defer / block
    overall_score: float                      # 0.0–1.0
    turnover_multiplier: float                # 作用于 max_daily_turnover_ratio
    size_multiplier: float                    # 作用于 single_name_cap / max_symbol_add_ratio
    reduce_only: bool
    blocked_symbols: List[str]
    favored_symbols: List[str]
    dimensions: List[SignalDimension]
    config_overrides: Dict[str, Any]          # 直接 deep-merge 到 execution_config
    summary: str
    is_simulation: bool = False

    def to_audit_dict(self) -> Dict[str, Any]:
        return {
            "verdict": self.verdict,
            "overall_score": round(self.overall_score, 3),
            "turnover_multiplier": round(self.turnover_multiplier, 4),
            "size_multiplier": round(self.size_multiplier, 4),
            "reduce_only": self.reduce_only,
            "blocked_symbols": self.blocked_symbols,
            "favored_symbols": self.favored_symbols,
            "is_simulation": self.is_simulation,
            "summary": self.summary,
            "dimensions": [
                {
                    "name": d.name,
                    "score": round(d.score, 3),
                    "verdict": d.verdict,
                    "reason": d.reason,
                }
                for d in self.dimensions
            ],
        }


# ---------------------------------------------------------------------------
# 信号评估函数
# ---------------------------------------------------------------------------

def _score_system_halt(safety: Dict[str, Any]) -> SignalDimension:
    system_mode = str(safety.get("system_mode", "") or "").upper()
    if system_mode == "HALT":
        return SignalDimension(
            name="system_halt",
            score=0.0,
            verdict="block",
            reason=f"system_mode=HALT halt_reason={safety.get('halt_reason', 'unknown')}",
            can_bypass_in_sim=False,
            is_hard_blocker=True,
        )
    return SignalDimension(name="system_halt", score=1.0, verdict="ok", reason="system_mode_normal")


def _score_market_panic(safety: Dict[str, Any]) -> SignalDimension:
    regime = str(safety.get("market_safety_regime", "") or "").lower()
    if regime == "panic":
        return SignalDimension(
            name="market_panic",
            score=0.0,
            verdict="block",
            reason=f"market_safety_regime=panic: {safety.get('market_panic_detail', '')}",
            can_bypass_in_sim=True,
            is_hard_blocker=True,
        )
    if regime == "caution":
        return SignalDimension(
            name="market_panic",
            score=0.45,
            verdict="warning",
            reason=f"market_safety_regime=caution: {safety.get('market_caution_detail', '')}",
        )
    return SignalDimension(name="market_panic", score=1.0, verdict="ok", reason=f"regime={regime or 'normal'}")


def _score_broker_health(safety: Dict[str, Any], positions_count: int) -> SignalDimension:
    broker_ok = bool(safety.get("broker_reachable", True))
    health_score = float(safety.get("broker_health_score", 1.0) or 1.0)
    if not broker_ok and positions_count > 0:
        return SignalDimension(
            name="broker_health",
            score=0.0,
            verdict="block",
            reason="broker_unreachable with open positions",
            can_bypass_in_sim=True,
            is_hard_blocker=True,
        )
    if not broker_ok:
        return SignalDimension(
            name="broker_health",
            score=0.3,
            verdict="warning",
            reason="broker_unreachable but no open positions (bootstrap allowed)",
        )
    if health_score < 0.5:
        return SignalDimension(
            name="broker_health",
            score=health_score * 0.8,
            verdict="caution",
            reason=f"broker_health_score={health_score:.2f}",
        )
    return SignalDimension(name="broker_health", score=1.0, verdict="ok", reason=f"broker_health_score={health_score:.2f}")


def _score_oms_state(safety: Dict[str, Any]) -> SignalDimension:
    unfinished = int(safety.get("unfinished_orders_count", 0) or 0)
    fail_ratio = float(safety.get("execution_fail_ratio", 0.0) or 0.0)

    if fail_ratio >= 0.75:
        return SignalDimension(
            name="oms_state",
            score=0.1,
            verdict="warning",
            reason=f"execution_fail_ratio={fail_ratio:.2f} above halt threshold",
        )
    if fail_ratio >= 0.35:
        return SignalDimension(
            name="oms_state",
            score=0.4,
            verdict="warning",
            reason=f"execution_fail_ratio={fail_ratio:.2f} above degraded threshold → reduce_only mode",
        )
    if unfinished > 3:
        return SignalDimension(
            name="oms_state",
            score=0.55,
            verdict="caution",
            reason=f"unfinished_orders={unfinished}: proceed with reconcile caution",
        )
    if unfinished > 0:
        return SignalDimension(
            name="oms_state",
            score=0.75,
            verdict="caution",
            reason=f"unfinished_orders={unfinished}: minor friction",
        )
    return SignalDimension(name="oms_state", score=1.0, verdict="ok", reason="oms_clean")


def _score_account_health(safety: Dict[str, Any], account_snapshot: Dict[str, Any]) -> SignalDimension:
    account_ok = bool(safety.get("account_state_ok", True))
    nav = float(account_snapshot.get("nav", 0.0) or 0.0)
    state_age = int(safety.get("account_state_age_seconds", 0) or 0)
    stale_threshold = int(safety.get("account_state_max_age_seconds", 900) or 900)

    if not account_ok:
        return SignalDimension(
            name="account_health",
            score=0.3,
            verdict="warning",
            reason="account_state_unavailable",
        )
    if state_age > stale_threshold * 2:
        return SignalDimension(
            name="account_health",
            score=0.5,
            verdict="caution",
            reason=f"account_state stale: {state_age}s > {stale_threshold * 2}s",
        )
    if state_age > stale_threshold:
        return SignalDimension(
            name="account_health",
            score=0.75,
            verdict="caution",
            reason=f"account_state slightly stale: {state_age}s > {stale_threshold}s",
        )
    return SignalDimension(name="account_health", score=1.0, verdict="ok", reason=f"nav={nav:.0f} state_age={state_age}s")


def _score_concentration(account_snapshot: Dict[str, Any], clock_snapshot: Dict[str, Any]) -> SignalDimension:
    top1 = float(clock_snapshot.get("concentration_top1", 0.0) or 0.0)
    hhi = float(clock_snapshot.get("concentration_hhi", 0.0) or 0.0)
    risk_tier = str(clock_snapshot.get("risk_tier", "normal") or "normal").lower()

    if risk_tier == "high_risk":
        return SignalDimension(
            name="concentration",
            score=0.45,
            verdict="warning",
            reason=f"risk_tier=high_risk top1={top1:.2f} hhi={hhi:.2f}: sizes capped",
        )
    if risk_tier == "elevated":
        return SignalDimension(
            name="concentration",
            score=0.70,
            verdict="caution",
            reason=f"risk_tier=elevated top1={top1:.2f} hhi={hhi:.2f}: mild size reduction",
        )
    return SignalDimension(name="concentration", score=1.0, verdict="ok", reason=f"top1={top1:.2f} hhi={hhi:.2f}")


def _score_market_state_policy(market_state: Dict[str, Any]) -> Tuple[SignalDimension, bool]:
    """Returns (dimension, reduce_only_flag)."""
    policy = str(market_state.get("new_position_policy", "allow") or "allow").lower()
    turnover_mult = float(market_state.get("turnover_multiplier", 1.0) or 1.0)
    regime = str(market_state.get("market_regime", "") or "").lower()

    if policy in ("no_new_positions", "reduce_only"):
        return SignalDimension(
            name="market_state_policy",
            score=0.3,
            verdict="warning",
            reason=f"new_position_policy={policy} regime={regime}",
        ), True
    if turnover_mult < 0.5:
        return SignalDimension(
            name="market_state_policy",
            score=0.5,
            verdict="caution",
            reason=f"turnover_multiplier={turnover_mult:.2f} regime={regime}",
        ), False
    if turnover_mult < 0.8:
        return SignalDimension(
            name="market_state_policy",
            score=0.75,
            verdict="caution",
            reason=f"turnover_multiplier={turnover_mult:.2f} regime={regime}",
        ), False
    return SignalDimension(
        name="market_state_policy",
        score=min(1.0, turnover_mult),
        verdict="ok",
        reason=f"policy={policy} regime={regime} turnover_mult={turnover_mult:.2f}",
    ), False


def _score_llm_review(llm_review: Dict[str, Any]) -> Tuple[SignalDimension, List[str], List[str], float]:
    """Returns (dimension, blocked_symbols, favored_symbols, turnover_multiplier)."""
    if not bool(llm_review.get("applied", False)):
        return (
            SignalDimension(name="llm_review", score=1.0, verdict="ok", reason="llm_review_not_applied"),
            [], [], 1.0,
        )
    review = dict(llm_review.get("review", {}) or {})
    risk_level = str(review.get("risk_level", "low") or "low").lower()
    reduce_only_flag = bool(review.get("reduce_only", False))
    turnover_mult = float(review.get("turnover_multiplier", 1.0) or 1.0)
    blocked = list(review.get("blocked_symbols", []) or [])
    favored = list(review.get("favored_symbols", []) or [])

    if reduce_only_flag:
        return SignalDimension(
            name="llm_review",
            score=0.35,
            verdict="warning",
            reason=f"llm_risk_level={risk_level} → reduce_only",
        ), blocked, favored, turnover_mult

    if risk_level in ("high", "extreme"):
        return SignalDimension(
            name="llm_review",
            score=0.5,
            verdict="caution",
            reason=f"llm_risk_level={risk_level} turnover_mult={turnover_mult:.2f}",
        ), blocked, favored, turnover_mult

    if risk_level == "medium" or turnover_mult < 0.85:
        return SignalDimension(
            name="llm_review",
            score=0.75,
            verdict="caution",
            reason=f"llm_risk_level={risk_level} turnover_mult={turnover_mult:.2f}",
        ), blocked, favored, turnover_mult

    return SignalDimension(
        name="llm_review",
        score=1.0,
        verdict="ok",
        reason=f"llm_risk_level={risk_level} turnover_mult={turnover_mult:.2f}",
    ), blocked, favored, turnover_mult


def _score_intraday_state(intraday_state: Dict[str, Any], is_simulation: bool) -> SignalDimension:
    if is_simulation:
        return SignalDimension(
            name="intraday_state",
            score=1.0,
            verdict="ok",
            reason="intraday_state bypassed for simulation",
            can_bypass_in_sim=True,
        )
    refresh_ok = bool(intraday_state.get("ok", True))
    strict_gate = bool(intraday_state.get("strict_gate_would_block", False))

    if not refresh_ok and strict_gate:
        return SignalDimension(
            name="intraday_state",
            score=0.4,
            verdict="warning",
            reason=f"intraday_state_refresh failed and strict_gate=True: {intraday_state.get('reason', '')}",
        )
    if not refresh_ok:
        return SignalDimension(
            name="intraday_state",
            score=0.65,
            verdict="caution",
            reason=f"intraday_state_refresh failed (fail_open): {intraday_state.get('reason', '')}",
        )
    return SignalDimension(
        name="intraday_state",
        score=1.0,
        verdict="ok",
        reason=f"intraday_state ok phase={intraday_state.get('current_phase', 'unknown')}",
    )


def _score_trade_discipline(trade_discipline: Dict[str, Any], positions_count: int) -> Tuple[SignalDimension, bool]:
    posture = str(trade_discipline.get("posture", "") or "").strip().lower()
    add_multiplier = float(trade_discipline.get("add_multiplier", 1.0) or 1.0)
    sell_pressure = float(trade_discipline.get("sell_pressure", 0.0) or 0.0)
    concentration_risk = str(trade_discipline.get("concentration_risk", "") or "").strip().lower()
    if not posture:
        return SignalDimension(name="trade_discipline", score=1.0, verdict="ok", reason="trade_discipline_unavailable"), False
    if posture == "reduce_only":
        return SignalDimension(
            name="trade_discipline",
            score=0.25 if positions_count > 0 else 0.55,
            verdict="warning",
            reason=f"trade_discipline posture=reduce_only sell_pressure={sell_pressure:.2f}",
        ), True
    if posture == "defensive":
        return SignalDimension(
            name="trade_discipline",
            score=max(min(add_multiplier, 1.0), 0.45),
            verdict="caution",
            reason=f"trade_discipline posture=defensive add_multiplier={add_multiplier:.2f} concentration={concentration_risk or 'ok'}",
        ), False
    if sell_pressure >= 0.7:
        return SignalDimension(
            name="trade_discipline",
            score=0.72,
            verdict="caution",
            reason=f"trade_discipline sell_pressure={sell_pressure:.2f}",
        ), False
    return SignalDimension(
        name="trade_discipline",
        score=min(max(add_multiplier, 0.85), 1.0),
        verdict="ok",
        reason=f"trade_discipline posture={posture} add_multiplier={add_multiplier:.2f}",
    ), False


# ---------------------------------------------------------------------------
# 核心评分逻辑
# ---------------------------------------------------------------------------

def _aggregate_score(dimensions: List[SignalDimension], is_simulation: bool) -> float:
    """
    加权平均，但受以下规则约束：
    - hard_blocker 维度分数为 0 → 总分直接 0
    - 非 hard_blocker 的 warning/block 维度使用 min(score) 锚定下限
    - simulation 模式豁免所有 can_bypass_in_sim=True 的维度
    """
    effective = [
        d for d in dimensions
        if not (is_simulation and d.can_bypass_in_sim and d.verdict in ("caution", "warning", "block"))
    ]

    for d in effective:
        if d.is_hard_blocker and d.score == 0.0:
            return 0.0

    if not effective:
        return 1.0

    weights = {
        "system_halt": 3.0,
        "market_panic": 2.5,
        "broker_health": 2.0,
        "oms_state": 1.5,
        "account_health": 1.0,
        "concentration": 0.8,
        "market_state_policy": 1.2,
        "llm_review": 1.0,
        "intraday_state": 0.7,
        "trade_discipline": 1.1,
    }
    total_weight = 0.0
    weighted_sum = 0.0
    min_non_hard = 1.0
    for d in effective:
        w = weights.get(d.name, 1.0)
        weighted_sum += d.score * w
        total_weight += w
        if d.verdict in ("warning", "block") and not d.is_hard_blocker:
            min_non_hard = min(min_non_hard, d.score)

    raw = weighted_sum / total_weight if total_weight > 0 else 1.0
    return max(min(raw, 1.0), min_non_hard * 0.9)


def _verdict_from_score(
    score: float,
    has_hard_blocker: bool,
    has_reduce_only: bool,
    has_defer: bool,
    is_simulation: bool,
    positions_count: int,
) -> str:
    if has_hard_blocker and score == 0.0 and not is_simulation:
        return "block"
    if has_defer:
        return "defer"
    if has_reduce_only and positions_count > 0:
        return "reduce_only"
    if score >= 0.85:
        return "proceed"
    if score >= 0.55:
        return "proceed_degraded"
    if score >= 0.30:
        return "reduce_only"
    return "block"


def _compute_turnover_multiplier(score: float, market_turnover: float, llm_turnover: float, safety_turnover: float) -> float:
    """
    不再串行叠乘三个来源，而是取最小值后再做评分修正：
    - safety / market_state / llm 三者各有主张
    - 取最保守的作为基准
    - 用综合 score 在 [0.4, 1.0] 范围再缩放
    """
    base = min(market_turnover, llm_turnover, safety_turnover)
    score_factor = 0.4 + 0.6 * max(0.0, min(score, 1.0))
    return round(max(base * score_factor, 0.05), 4)


def _compute_size_multiplier(score: float, concentration_dim: Optional[SignalDimension]) -> float:
    if concentration_dim and concentration_dim.verdict == "warning":
        base = 0.55
    elif concentration_dim and concentration_dim.verdict == "caution":
        base = 0.80
    else:
        base = 1.0
    score_factor = 0.5 + 0.5 * max(0.0, min(score, 1.0))
    return round(base * score_factor, 4)


# ---------------------------------------------------------------------------
# 主接口
# ---------------------------------------------------------------------------

def evaluate(
    config: Dict[str, Any],
    safety: Dict[str, Any],
    market_state: Dict[str, Any],
    llm_review: Dict[str, Any],
    account_snapshot: Dict[str, Any],
    intraday_state: Optional[Dict[str, Any]] = None,
    clock_snapshot: Optional[Dict[str, Any]] = None,
    trade_discipline: Optional[Dict[str, Any]] = None,
) -> ConstraintDecision:
    """
    收集所有信号，统一决策。

    Parameters
    ----------
    config          : 当前 runtime config
    safety          : assess_system_safety() 的结果
    market_state    : 市场状态 (regime, new_position_policy, turnover_multiplier …)
    llm_review      : review_execution_plan() 的结果
    account_snapshot: 账户快照 (nav, cash, positions_count)
    intraday_state  : 盘中状态机输出（可选）
    clock_snapshot  : clock_account_snapshot（可选，用于仓位集中度）
    """
    account_mode = str(
        config.get("execution_policy", {}).get("account_mode", "simulation") or "simulation"
    ).strip().lower()
    is_simulation = account_mode == "simulation"
    positions_count = int(account_snapshot.get("positions_count", 0) or 0)

    # ---- 评估各维度 ----
    dim_halt = _score_system_halt(safety)
    dim_panic = _score_market_panic(safety)
    dim_broker = _score_broker_health(safety, positions_count)
    dim_oms = _score_oms_state(safety)
    dim_account = _score_account_health(safety, account_snapshot)
    dim_concentration = _score_concentration(account_snapshot, clock_snapshot or {})
    dim_market_policy, market_reduce_only = _score_market_state_policy(market_state)
    dim_llm, blocked_symbols, favored_symbols, llm_turnover_mult = _score_llm_review(llm_review)
    dim_intraday = _score_intraday_state(intraday_state or {}, is_simulation)
    dim_discipline, discipline_reduce_only = _score_trade_discipline(trade_discipline or {}, positions_count)

    all_dims = [
        dim_halt, dim_panic, dim_broker, dim_oms, dim_account,
        dim_concentration, dim_market_policy, dim_llm, dim_intraday, dim_discipline,
    ]

    # ---- 聚合评分 ----
    score = _aggregate_score(all_dims, is_simulation)

    # ---- 特殊标志 ----
    has_hard_blocker = any(d.is_hard_blocker and d.score == 0.0 for d in all_dims)
    has_reduce_only = (
        market_reduce_only
        or discipline_reduce_only
        or bool(dim_llm.verdict == "warning" and (llm_review.get("review") or {}).get("reduce_only", False))
        or dim_oms.score < 0.45
    )
    has_defer = False  # defer 由调用层根据 gate (timing/release validity) 决定，不属于 brain 范畴

    # 空仓特殊处理：reduce_only 对空账户没有意义
    if has_reduce_only and positions_count == 0:
        has_reduce_only = False
        score = max(score, 0.55)

    outer_decision = arbitrate_execution(
        safety=safety,
        market_state=market_state,
        llm_review=llm_review,
        account_snapshot=account_snapshot,
    )
    verdict = str(outer_decision.get("verdict", "") or "") or _verdict_from_score(
        score=score,
        has_hard_blocker=has_hard_blocker,
        has_reduce_only=has_reduce_only,
        has_defer=has_defer,
        is_simulation=is_simulation,
        positions_count=positions_count,
    )

    # simulation 模式下 hard_blocker 降为 reduce_only（不 block）
    if is_simulation and verdict == "block" and not dim_halt.is_hard_blocker:
        verdict = "reduce_only"

    # ---- 计算乘数 ----
    safety_turnover = float(safety.get("effective_turnover_multiplier", 1.0) or 1.0)
    market_turnover = float(market_state.get("turnover_multiplier", 1.0) or 1.0)
    turnover_multiplier = float(outer_decision.get("turnover_multiplier", 0.0) or 0.0) or _compute_turnover_multiplier(score, market_turnover, llm_turnover_mult, safety_turnover)
    size_multiplier = float(outer_decision.get("size_multiplier", 0.0) or 0.0) or _compute_size_multiplier(score, dim_concentration)

    # 如果 verdict 是 reduce_only/block，换手率进一步收紧
    if verdict == "reduce_only":
        turnover_multiplier = min(turnover_multiplier, 0.45)
        size_multiplier = min(size_multiplier, 0.5)
    elif verdict == "block":
        turnover_multiplier = 0.0
        size_multiplier = 0.0

    # ---- 构造 config_overrides ----
    config_overrides = _build_config_overrides(
        config=config,
        verdict=verdict,
        turnover_multiplier=turnover_multiplier,
        size_multiplier=size_multiplier,
        reduce_only=has_reduce_only or verdict in ("reduce_only", "block"),
        blocked_symbols=blocked_symbols,
        favored_symbols=favored_symbols,
        market_state=market_state,
        trade_discipline=trade_discipline or {},
    )

    # ---- summary ----
    warning_dims = [d for d in all_dims if d.verdict in ("warning", "block", "caution")]
    if warning_dims:
        top_reason = "; ".join(f"{d.name}={d.verdict}({d.reason})" for d in warning_dims[:3])
    else:
        top_reason = "all_clear"
    summary = (
        f"verdict={verdict} score={score:.2f} "
        f"turnover_mult={turnover_multiplier:.2f} size_mult={size_multiplier:.2f} "
        f"sim={is_simulation} | {top_reason} | outer={outer_decision.get('summary', '')}"
    )

    return ConstraintDecision(
        verdict=verdict,
        overall_score=score,
        turnover_multiplier=turnover_multiplier,
        size_multiplier=size_multiplier,
        reduce_only=has_reduce_only or verdict in ("reduce_only", "block"),
        blocked_symbols=blocked_symbols,
        favored_symbols=favored_symbols,
        dimensions=all_dims,
        config_overrides=config_overrides,
        summary=summary,
        is_simulation=is_simulation,
    )


def _build_config_overrides(
    config: Dict[str, Any],
    verdict: str,
    turnover_multiplier: float,
    size_multiplier: float,
    reduce_only: bool,
    blocked_symbols: List[str],
    favored_symbols: List[str],
    market_state: Dict[str, Any],
    trade_discipline: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """
    产生一个合并好的 overrides dict，调用方一次 deep_merge 即可。
    不再需要 _apply_market_state_execution_overrides 和
    _apply_llm_execution_review_overrides 两次单独的 deepcopy+merge。
    """
    portfolio_control = copy.deepcopy(dict(config.get("portfolio_control", {}) or {}))
    current_turnover = float(portfolio_control.get("max_daily_turnover_ratio", 0.25) or 0.25)

    portfolio_control["max_daily_turnover_ratio"] = round(current_turnover * turnover_multiplier, 6)
    portfolio_control["reduce_only"] = reduce_only
    portfolio_control["llm_blocked_symbols"] = blocked_symbols
    portfolio_control["llm_favored_symbols"] = favored_symbols

    if size_multiplier < 1.0:
        current_cap = float(portfolio_control.get("account_size_max_single_name_cap", 0.35) or 0.35)
        portfolio_control["account_size_max_single_name_cap"] = round(current_cap * size_multiplier, 4)

    overrides: Dict[str, Any] = {
        "portfolio_control": portfolio_control,
        "market_state_runtime": {
            "market_regime": str(market_state.get("market_regime", "") or ""),
            "style_bias": str(market_state.get("style_bias", "") or ""),
            "mechanism_bias": str(market_state.get("mechanism_bias", "") or ""),
            "risk_budget_multiplier": float(market_state.get("risk_budget_multiplier", 1.0) or 1.0),
            "turnover_multiplier": float(market_state.get("turnover_multiplier", 1.0) or 1.0),
            "entry_strictness": float(market_state.get("entry_strictness", 0.5) or 0.5),
            "new_position_policy": str(market_state.get("new_position_policy", "allow") or "allow"),
        },
        "trade_discipline_runtime": dict(trade_discipline or {}),
        "constraint_brain_verdict": verdict,
    }
    return overrides


def apply_to_config(base_config: Dict[str, Any], decision: ConstraintDecision) -> Dict[str, Any]:
    """一次性将 ConstraintDecision 的 overrides 应用到 config，返回新 config（不修改原始）。"""
    result = copy.deepcopy(base_config)
    for k, v in decision.config_overrides.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = {**result[k], **v}
        else:
            result[k] = v
    return result
