# -*- coding: utf-8 -*-
"""候选实验工厂。

V5.1 的核心修正：
1. 保留 V5 的“同轮去重、跨轮允许复验”；
2. 正式加入 GPU 模型家族；
3. 候选实验显式携带资源约束、GPU 档案与模型选项，避免运行时再猜。
"""

from __future__ import annotations

import copy
import hashlib
import json
import random
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

from hub.codegen import CodegenLab
from hub.io_utils import write_csv, write_json
from hub.llm_client import LLMClient
from hub.research_routes import allocate_route_budget, route_hypotheses

MAX_BRIDGE_INPUT_JSON_BYTES = 5 * 1024 * 1024

# 结构性高回撤的毒特征包：家族表里凡用 momentum_cross_section 的回撤无一例外 -26%~-42%，
# P7 深修提了 IC 也压不住回撤。直接拉黑，任何来源（route_space / 继承父代 / 冷启动种子）
# 出现都换成 generated_feature_pack。详见分析记录。
BLACKLISTED_FEATURE_PROFILES = {'momentum_cross_section'}
SAFE_DEFAULT_FEATURE_PROFILE = 'generated_feature_pack'


def _sanitize_feature_profile(profile: Any) -> str:
    """把被拉黑的特征包替换成安全默认值。"""
    fp = str(profile or '').strip()
    if not fp or fp in BLACKLISTED_FEATURE_PROFILES:
        return SAFE_DEFAULT_FEATURE_PROFILE
    return fp


def _clean_feature_profiles(profiles: List[Any]) -> List[str]:
    """清洗特征包候选池：剔除黑名单并去重保序。"""
    out: List[str] = []
    for p in profiles:
        fp = str(p or '').strip()
        if not fp or fp in BLACKLISTED_FEATURE_PROFILES:
            continue
        if fp not in out:
            out.append(fp)
    return out or [SAFE_DEFAULT_FEATURE_PROFILE]


def _pick(seq: List[Any], idx: int, cycle_index: int, rng: Optional[random.Random]) -> Any:
    """从候选池取一个。

    有 rng 时随机抽样（每轮不同，实现真探索）；无 rng 时退回旧的确定性取模，
    保证不传 rng 的老调用方行为不变。
    """
    seq = list(seq)
    if not seq:
        return None
    if rng is not None:
        return rng.choice(seq)
    return seq[(idx + cycle_index) % len(seq)]


def stable_hash(payload: Dict[str, Any], length: int = 12) -> str:
    """稳定哈希。

    Args:
        payload: 输入字典。
        length: 截断长度。

    Returns:
        哈希字符串。
    """
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.md5(raw.encode('utf-8')).hexdigest()[:length]


def _ordered_model_choices(space: Dict[str, Any], default: List[str] | None = None) -> List[str]:
    values = [str(item).strip() for item in list(space.get('model_families', default or ['ridge_ranker'])) if str(item).strip()]
    return values or list(default or ['ridge_ranker'])


def _pick_model_family(space: Dict[str, Any], preferred_order: List[str], idx: int, cycle_index: int, fallback: str = 'ridge_ranker') -> str:
    allowed = _ordered_model_choices(space, default=[fallback])
    ranked = [item for item in preferred_order if item in allowed] + [item for item in allowed if item not in preferred_order]
    ranked = ranked or allowed or [fallback]
    return ranked[(idx + cycle_index) % len(ranked)]


def _seed_parents(base_config: Dict[str, Any]) -> List[Dict[str, Any]]:
    """冷启动父代种子。

    Args:
        base_config: 配置字典。

    Returns:
        父代列表。
    """
    strategy = dict(base_config.get('strategy', {}))
    seeds = []
    for feature_profile in ['baseline_plus', 'vol_liq_quality', 'generated_feature_pack']:
        for model_family in ['xgboost_gpu', 'lightgbm_gpu', 'ridge_ranker', 'lightgbm_auto']:
            for training_logic in ['baseline', 'weighted_recent', 'feature_select']:
                seeds.append({
                    'strategy_name': strategy.get('strategy_name', 'v5_1_seed'),
                    'feature_profile': feature_profile,
                    'model_family': model_family,
                    'training_logic': training_logic,
                    'label_horizon': int(strategy.get('label_horizon', 5) or 5),
                    'top_k': int(strategy.get('top_k', 20) or 20),
                    'strategy_key': stable_hash({'feature_profile': feature_profile, 'model_family': model_family, 'training_logic': training_logic}, 12),
                })
    return seeds


def _top_parents(registry_df, n: int, base_config: Dict[str, Any]) -> List[Dict[str, Any]]:
    """取父代池。

    Args:
        registry_df: 注册表。
        n: 需要的数量。
        base_config: 配置字典。

    Returns:
        父代列表。
    """
    if registry_df.empty:
        return _seed_parents(base_config)[:n]
    cols = ['total_score', 'sharpe', 'valid_ic', 'test_ic']
    return registry_df.sort_values(cols, ascending=[False, False, False, False]).head(max(n, 1)).to_dict(orient='records')


CHAMPION_SPEC_FILES = ('feature_pack.spec.json', 'train_override.spec.json', 'generated_model.spec.json')


def _champion_specs_source(parent: Dict[str, Any], cycle_dir: Path) -> Optional[Path]:
    """定位冠军父代的三个 spec.json 来源目录，用于 verbatim 复刻其特征包/训练计划/模型。

    优先用稳定存档（champions_archive/<strategy_key>）；存档没有就回退到父代原始 lab
    （registry 的 workspace_dir）。两处都拿到时，把原始 lab 的 spec 快照进存档——
    这样即使旧 cycle 的 labs 日后被清理，冠军基因仍能确定性复刻。返回 None=无可用来源。
    """
    def _has_specs(d: Optional[Path]) -> bool:
        return bool(d) and all((d / name).exists() for name in CHAMPION_SPEC_FILES)

    strategy_key = str(parent.get('strategy_key', '') or '').strip()
    archive_dir: Optional[Path] = None
    if strategy_key:
        try:
            archive_dir = cycle_dir.parent.parent / 'champions_archive' / strategy_key
        except Exception:
            archive_dir = None

    if _has_specs(archive_dir):
        return archive_dir

    raw_ws = str(parent.get('workspace_dir', '') or '').strip()
    if not raw_ws:
        return None
    ws = Path(raw_ws)
    if not _has_specs(ws):
        return None
    if archive_dir is not None:
        try:
            archive_dir.mkdir(parents=True, exist_ok=True)
            for name in CHAMPION_SPEC_FILES:
                shutil.copy2(ws / name, archive_dir / name)
            return archive_dir
        except Exception:
            pass
    return ws


def _load_json_if_small(path: Path) -> Dict[str, Any]:
    try:
        if path.stat().st_size > MAX_BRIDGE_INPUT_JSON_BYTES:
            return {}
        return dict(json.loads(path.read_text(encoding='utf-8')) or {})
    except Exception:
        return {}


def _compact_scheduler_decision(decision: Dict[str, Any]) -> Dict[str, Any]:
    src = dict(decision or {})
    return {
        'version': src.get('version'),
        'generated_at': src.get('generated_at'),
        'status': src.get('status'),
        'final_verdict': src.get('final_verdict'),
        'policy_posture': src.get('policy_posture'),
        'reason_chain': list(src.get('reason_chain', []) or [])[:60],
        'research_plan': dict(src.get('research_plan', {}) or {}),
        'portfolio_construction': dict(src.get('portfolio_construction', {}) or {}),
        'route_weights': dict(src.get('route_weights', {}) or {}),
        'route_budget': dict(src.get('route_budget', {}) or {}),
        'route_space_overrides': dict(src.get('route_space_overrides', {}) or {}),
        'preferred_model_families': list(src.get('preferred_model_families', []) or []),
        'ban_model_families': list(src.get('ban_model_families', []) or []),
        'research_brain_overrides': dict(src.get('research_brain_overrides', {}) or {}),
        'strategy_overrides': dict(src.get('strategy_overrides', {}) or {}),
        'portfolio_overrides': dict(src.get('portfolio_overrides', {}) or {}),
    }


def _compact_bridge_input_value(name: str, value: Dict[str, Any]) -> Dict[str, Any]:
    src = dict(value or {})
    if name != 'performance_feedback.json':
        return src
    out = {
        'generated_at': src.get('generated_at'),
        'available': src.get('available'),
        'authority_role': src.get('authority_role'),
        'regime': src.get('regime'),
        'metrics': dict(src.get('metrics', {}) or {}),
        'signal_trace': list(src.get('signal_trace', []) or [])[:20],
        'route_weights': dict(src.get('route_weights', {}) or {}),
        'route_budget': dict(src.get('route_budget', {}) or {}),
        'route_space_overrides': dict(src.get('route_space_overrides', {}) or {}),
        'preferred_model_families': list(src.get('preferred_model_families', []) or []),
        'ban_model_families': list(src.get('ban_model_families', []) or []),
        'research_brain_overrides': dict(src.get('research_brain_overrides', {}) or {}),
        'strategy_overrides': dict(src.get('strategy_overrides', {}) or {}),
        'portfolio_overrides': dict(src.get('portfolio_overrides', {}) or {}),
    }
    decision = dict(src.get('scheduler_budget_decision') or src.get('scheduler_research_decision') or {})
    if decision:
        out['scheduler_decision_summary'] = _compact_scheduler_decision(decision)
    return out


def _compact_bridge_inputs_for_artifact(bridge_inputs: Dict[str, Any]) -> Dict[str, Any]:
    return {
        name: _compact_bridge_input_value(name, dict(value or {}))
        for name, value in dict(bridge_inputs or {}).items()
    }




def _load_bridge_inputs(base_config: Dict[str, Any]) -> Dict[str, Any]:
    bridge_cfg = dict(base_config.get('bridge_inputs', {}) or {})
    if not bool(bridge_cfg.get('enabled', False)):
        return {}
    root = Path(str(bridge_cfg.get('bridge_root', '') or '').strip())
    if not root.exists():
        return {}
    payload: Dict[str, Any] = {}
    for name in ['llm_route_override.json', 'candidate_override.json', 'enriched_context.json', 'performance_feedback.json']:
        path = root / name
        if path.exists():
            payload[name] = _load_json_if_small(path)
    return payload


def _apply_bridge_candidate_override(route_space: Dict[str, Any], bridge_inputs: Dict[str, Any]) -> Dict[str, Any]:
    out = copy.deepcopy(route_space)
    cand = dict(bridge_inputs.get('candidate_override.json', {}) or {})
    for key in ['feature_profiles', 'label_horizons', 'top_ks']:
        extra = list(cand.get(key, []) or [])
        if extra:
            out[key] = list(dict.fromkeys(list(out.get(key, []) or []) + extra))
    preferred_models = list(cand.get('preferred_model_families', []) or [])
    if preferred_models:
        out['model_families'] = list(dict.fromkeys(preferred_models + list(out.get('model_families', []) or [])))
    ban_models = set(cand.get('ban_model_families', []) or [])
    if ban_models:
        out['model_families'] = [m for m in list(out.get('model_families', []) or []) if m not in ban_models]
    perf = dict(bridge_inputs.get('performance_feedback.json', {}) or {})
    route_space_override = dict(perf.get('route_space_overrides', {}) or {})
    for key in ['feature_profiles', 'label_horizons', 'top_ks', 'base_exposures', 'weak_exposures', 'model_families', 'training_logics']:
        if key in route_space_override and list(route_space_override.get(key, []) or []):
            out[key] = list(route_space_override.get(key, []) or [])
    preferred_models = list(perf.get('preferred_model_families', []) or [])
    if preferred_models:
        out['model_families'] = list(dict.fromkeys(preferred_models + list(out.get('model_families', []) or [])))
    ban_models = set(perf.get('ban_model_families', []) or [])
    if ban_models:
        out['model_families'] = [m for m in list(out.get('model_families', []) or []) if m not in ban_models]
    return out

def _llm_route_override(llm_client: LLMClient, diagnosis: Dict[str, Any], route_space: Dict[str, Any], parents: List[Dict[str, Any]]) -> Dict[str, Any]:
    """用 LLM 反思研究路线。

    Args:
        llm_client: LLM 客户端。
        diagnosis: 诊断结果。
        route_space: 搜索空间。
        parents: 父代池。

    Returns:
        结构化建议。
    """
    if not llm_client.is_enabled():
        return {}
    system_prompt = (
        '你是量化研究主管。你不负责调几个参数，而是判断下一轮研究预算投到哪里。'
        '只输出 JSON，字段包含 route_weights、new_feature_ideas、new_model_ideas、new_training_ideas。'
    )
    user_prompt = json.dumps({'diagnosis': diagnosis, 'route_space': route_space, 'top_parents': parents[:3]}, ensure_ascii=False)
    return llm_client.chat_json(system_prompt=system_prompt, user_prompt=user_prompt, temperature=0.2)


def _candidate_spec(
    route: str,
    parent: Dict[str, Any],
    space: Dict[str, Any],
    idx: int,
    cycle_index: int,
    hypotheses: Dict[str, List[str]],
    rng: Optional[random.Random] = None,
    exploit: bool = False,
) -> Dict[str, Any]:
    """构建单个候选结构。

    rng:    每轮独立的随机源。有它就随机抽样（真探索，每轮不同）；没有就退回
            确定性取模（老行为）。
    exploit: True 时直接 verbatim 复评父代配方（hold-the-line，专给候选1=冠军用），
            不做任何路线改写，只清洗黑名单特征包。
    """
    fps = _clean_feature_profiles(list(space.get('feature_profiles', ['baseline_plus'])))
    mfs = _ordered_model_choices(space, default=['ridge_ranker'])
    tls = list(space.get('training_logics', ['baseline']))
    lhs = list(space.get('label_horizons', [5, 10, 20]))
    tks = list(space.get('top_ks', [15, 20, 30]))
    exposures = list(space.get('base_exposures', [1.0, 0.9, 0.8]))
    weaks = list(space.get('weak_exposures', [0.5, 0.4, 0.3]))

    parent_fp = _sanitize_feature_profile(parent.get('feature_profile')) if parent.get('feature_profile') else _pick(fps, idx, cycle_index, rng)
    parent_mf = parent.get('model_family') if parent.get('model_family') in mfs else _pick(mfs, idx, cycle_index, rng)
    parent_tl = parent.get('training_logic') if parent.get('training_logic') in tls else _pick(tls, idx, cycle_index, rng)

    spec = {
        'research_route': route,
        'feature_profile': parent_fp,
        'model_family': parent_mf,
        'training_logic': parent_tl,
        'label_horizon': int(parent.get('label_horizon', _pick(lhs, idx, cycle_index, rng))),
        'top_k': int(parent.get('top_k', _pick(tks, idx, cycle_index, rng))),
        'portfolio_base_exposure': float(parent.get('portfolio_base_exposure', _pick(exposures, idx, cycle_index, rng))),
        'portfolio_weak_market_exposure': float(parent.get('portfolio_weak_market_exposure', _pick(weaks, idx, cycle_index, rng))),
        'hypothesis': hypotheses.get(route, [''])[idx % max(len(hypotheses.get(route, [''])), 1)],
    }

    # 冠军基因保护：exploit 直接复评父代完整配方，不让路线逻辑覆盖好基因。
    # 必须完全确定性（不沾 rng），否则候选1每轮 hash 漂移、认不出是冠军复评，
    # 既攒不到稳定性样本、又把“每轮换新 spec”的churn 带回来。registry 没记仓位暴露，
    # 用 config 列表首项作稳定回填。
    if exploit:
        spec['portfolio_base_exposure'] = float(parent.get('portfolio_base_exposure', exposures[0]))
        spec['portfolio_weak_market_exposure'] = float(parent.get('portfolio_weak_market_exposure', weaks[0]))
        spec['label_horizon'] = int(parent.get('label_horizon', lhs[0]))
        spec['top_k'] = int(parent.get('top_k', tks[0]))
        spec['hypothesis'] = 'hold-the-line：verbatim 复评当前冠军配方，确认稳定性并防止研究退步。'
        spec['feature_profile'] = _sanitize_feature_profile(spec['feature_profile'])
        return spec

    if route == 'feature':
        spec['feature_profile'] = _pick(fps, idx, cycle_index, rng)
    elif route == 'model':
        # 模型路线打破“全 lightgbm”单一化：固定挑最强的非 lightgbm 异构对照。
        # NaN-in-y 崩溃已被 training_engine._drop_unlabeled 修好，xgboost 现在能正常训练，
        # 所以确定性优先 xgboost（pool[0]），不再随机抽到已知废掉的 ridge，确保拿到有效对照数据。
        non_lgb = [m for m in mfs if m != 'lightgbm_gpu']
        gpu_non_lgb = [m for m in non_lgb if m in {'xgboost_gpu', 'lightgbm_auto', 'generated_family'}] + [m for m in non_lgb if m not in {'xgboost_gpu', 'lightgbm_auto', 'generated_family'}]
        pool = gpu_non_lgb or non_lgb or mfs
        spec['model_family'] = pool[0]
    elif route == 'training':
        spec['training_logic'] = _pick(tls, idx, cycle_index, rng)
    elif route == 'portfolio':
        spec['top_k'] = int(_pick(tks, idx, cycle_index, rng))
        spec['portfolio_base_exposure'] = float(_pick(exposures, idx, cycle_index, rng))
    elif route == 'risk':
        spec['portfolio_weak_market_exposure'] = float(_pick(weaks, idx, cycle_index, rng))
        spec['portfolio_dd_stage1'] = _pick([0.08, 0.10, 0.12], idx, cycle_index, rng)
        spec['portfolio_dd_stage2'] = _pick([0.15, 0.18, 0.22], idx, cycle_index, rng)
    elif route == 'data':
        spec['feature_profile'] = SAFE_DEFAULT_FEATURE_PROFILE
        spec['model_family'] = _pick_model_family(
            space,
            preferred_order=['xgboost_gpu', 'lightgbm_gpu', 'lightgbm_auto', 'ridge_ranker', 'generated_family'],
            idx=idx,
            cycle_index=cycle_index,
            fallback='ridge_ranker',
        )
    elif route == 'hybrid':
        spec['feature_profile'] = _pick(fps, idx, cycle_index, rng)
        spec['model_family'] = _pick(mfs, idx, cycle_index, rng)
        spec['training_logic'] = _pick(tls, idx, cycle_index, rng)

    spec['feature_profile'] = _sanitize_feature_profile(spec['feature_profile'])
    return spec


def build_cycle_plan(base_config: Dict[str, Any], registry_df, cycle_index: int, diagnosis: Dict[str, Any], llm_client: LLMClient, cycle_dir: Path) -> Dict[str, Any]:
    """生成本轮实验计划。"""
    brain_cfg = dict(base_config.get('research_brain', {}))
    total_candidates = int(brain_cfg.get('cycle_candidate_budget', 12) or 12)
    parent_pool_size = int(brain_cfg.get('parent_pool_size', 4) or 4)
    route_min_candidates = int(brain_cfg.get('route_min_candidates', 1) or 1)
    route_space = dict(base_config.get('route_space', {}))
    bridge_inputs = _load_bridge_inputs(base_config)
    route_space = _apply_bridge_candidate_override(route_space, bridge_inputs)
    parents = _top_parents(registry_df, parent_pool_size, base_config)
    hypotheses = route_hypotheses(diagnosis)
    budget = allocate_route_budget(diagnosis, total_candidates=total_candidates, min_each=route_min_candidates)

    llm_override = _llm_route_override(llm_client, diagnosis, route_space, parents)
    perf_feedback = dict(bridge_inputs.get('performance_feedback.json', {}) or {})
    perf_route_weights = dict(perf_feedback.get('route_weights', {}) or {})
    if perf_route_weights:
        merged_weights = dict(perf_route_weights)
        merged_weights.update(dict(llm_override.get('route_weights', {}) or {}))
        llm_override['route_weights'] = merged_weights
    bridge_route_override = dict(bridge_inputs.get('llm_route_override.json', {}) or {})
    if bridge_route_override:
        llm_override['route_weights'] = dict(bridge_route_override)
    if isinstance(llm_override.get('route_weights'), dict):
        budget = allocate_route_budget({'route_weights': llm_override['route_weights']}, total_candidates=total_candidates, min_each=route_min_candidates)

    lab = CodegenLab(llm_client)
    seen_in_cycle = set()
    rows = []
    candidate_configs = []
    cfg_dir = cycle_dir / 'configs'
    cfg_dir.mkdir(parents=True, exist_ok=True)

    route_seq: List[str] = []
    for route, count in budget.items():
        route_seq.extend([route] * int(count))
    if not route_seq:
        route_seq = ['hybrid'] * total_candidates

    gpu_profile = dict(base_config.get('gpu_profile', {}))
    resource_constraints = dict(base_config.get('resource_constraints', {}))

    # 每轮独立随机源：用带时间戳的 cycle_dir 名派生种子，保证每次 --mode batch 真生成
    # 不同候选（破除“每轮重复同 7 个 spec”），但同一轮内可复现。
    run_seed = int(hashlib.md5(str(cycle_dir.name).encode('utf-8')).hexdigest(), 16)

    for idx, route in enumerate(route_seq, start=1):
        parent = parents[(idx - 1) % len(parents)]
        # 候选1 = 冠军 hold-the-line：verbatim 复评家族最佳父代，对抗研究退步。
        exploit = (idx == 1 and not registry_df.empty)
        rng = random.Random(f"{run_seed}:{idx}:{route}")
        spec = _candidate_spec(route, parent, route_space, idx - 1, cycle_index, hypotheses, rng=rng, exploit=exploit)
        spec['alpha_label_mode'] = str(base_config['strategy'].get('alpha_label_mode', 'raw_return'))
        spec['feature_market_policy'] = str(base_config['strategy'].get('feature_market_policy', 'allow'))
        spec['feature_liquidity_policy'] = str(base_config['strategy'].get('feature_liquidity_policy', 'allow'))
        sig = dict(spec)
        sig['cycle_index'] = cycle_index
        sig['candidate_index'] = idx
        spec_hash = stable_hash(sig, 12)
        if spec_hash in seen_in_cycle:
            sig['candidate_index'] = idx * 100 + cycle_index
            spec_hash = stable_hash(sig, 12)
        seen_in_cycle.add(spec_hash)

        cfg = copy.deepcopy(base_config)
        st = cfg['strategy']
        st['label_horizon'] = int(spec['label_horizon'])
        st['top_k'] = int(spec['top_k'])
        st['portfolio_base_exposure'] = float(spec['portfolio_base_exposure'])
        st['portfolio_weak_market_exposure'] = float(spec['portfolio_weak_market_exposure'])
        if 'portfolio_dd_stage1' in spec:
            st['portfolio_dd_stage1'] = float(spec['portfolio_dd_stage1'])
        if 'portfolio_dd_stage2' in spec:
            st['portfolio_dd_stage2'] = float(spec['portfolio_dd_stage2'])

        strategy_family_name = str(base_config['strategy']['strategy_name'])
        strategy_name = f"{strategy_family_name}__{route}__c{cycle_index:03d}_{idx:03d}_{spec_hash[:8]}"
        cfg['strategy']['strategy_name'] = strategy_name
        cfg['strategy']['run_tag'] = f"{base_config['strategy']['run_tag']}_c{cycle_index:03d}_{idx:03d}_{spec_hash[:8]}"
        cfg['strategy']['train_output_subdir'] = f"{base_config['strategy']['train_output_subdir']}_c{cycle_index:03d}_{idx:03d}_{spec_hash[:8]}"

        workspace_dir = cycle_dir / 'labs' / f'candidate_{idx:03d}_{spec_hash[:8]}'
        lab_context = {'route': route, 'spec': spec, 'diagnosis': diagnosis, 'llm_override': llm_override}
        # 候选1(exploit)的 hold-the-line：若能定位冠军父代的存档 spec，就确定性重编译复刻其
        # 特征包/训练计划/模型（不调用 LLM），真正复评冠军基因；定位不到再退回正常生成。
        champion_specs = _champion_specs_source(parent, cycle_dir) if exploit else None
        if champion_specs is not None:
            lab_info = lab.build_workspace_from_specs(workspace_dir, champion_specs, context=lab_context)
        else:
            lab_info = lab.build_workspace(workspace_dir, context=lab_context)
        model_options = {
            'lightgbm_device_type': str(gpu_profile.get('lightgbm_device_type', 'gpu')),
            'gpu_platform_id': gpu_profile.get('gpu_platform_id'),
            'gpu_device_id': gpu_profile.get('gpu_device_id'),
            'gpu_use_dp': bool(gpu_profile.get('gpu_use_dp', False)),
            'xgboost_device': str(gpu_profile.get('xgboost_device', 'cuda')),
            'n_estimators': int(gpu_profile.get('default_n_estimators', 420) or 420),
            'max_bin': int(gpu_profile.get('default_max_bin', 256) or 256),
        }
        cfg['candidate'] = {
            'cycle_id': cycle_dir.name,
            'cycle_index': cycle_index,
            'candidate_index': idx,
            'strategy_family_name': strategy_family_name,
            'strategy_name': strategy_name,
            'strategy_key': spec_hash,
            'spec_hash': spec_hash,
            'parent_strategy_key': str(parent.get('strategy_key', 'seed_root')),
            'research_route': route,
            'hypothesis': spec['hypothesis'],
            'feature_profile': spec['feature_profile'],
            'model_family': spec['model_family'],
            'training_logic': spec['training_logic'],
            'label_col': str(base_config['strategy']['label_col']),
            'label_horizon': int(spec['label_horizon']),
            'alpha_label_mode': str(base_config['strategy'].get('alpha_label_mode', 'raw_return')),
            'feature_market_policy': str(base_config['strategy'].get('feature_market_policy', 'allow')),
            'feature_liquidity_policy': str(base_config['strategy'].get('feature_liquidity_policy', 'allow')),
            'top_k': int(spec['top_k']),
            'lab': lab_info,
            'resource_constraints': resource_constraints,
            'gpu_profile': gpu_profile,
            'model_options': model_options,
        }
        cfg_path = cfg_dir / f'candidate_{idx:03d}_{spec_hash[:8]}.json'
        cfg['candidate']['config_path'] = str(cfg_path)
        cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding='utf-8')
        row = {
            'cycle_id': cycle_dir.name,
            'candidate_index': idx,
            'strategy_name': strategy_name,
            'strategy_key': spec_hash,
            'research_route': route,
            'feature_profile': spec['feature_profile'],
            'model_family': spec['model_family'],
            'training_logic': spec['training_logic'],
            'hypothesis': spec['hypothesis'],
            'config_path': str(cfg_path),
            'workspace_dir': lab_info['workspace_dir'],
        }
        rows.append(row)
        candidate_configs.append(cfg)

    import pandas as pd
    manifest_df = pd.DataFrame(rows)
    write_csv(cycle_dir / 'cycle_manifest.csv', manifest_df)
    write_json(cycle_dir / 'cycle_plan.json', {
        'cycle_id': cycle_dir.name,
        'cycle_index': cycle_index,
        'n_candidates': len(candidate_configs),
        'budget': budget,
        'diagnosis': diagnosis,
        'llm_override': llm_override,
        'bridge_inputs': _compact_bridge_inputs_for_artifact(bridge_inputs),
        'gpu_profile': gpu_profile,
        'stop_reason_if_zero': 'V5.1 仍保持同轮去重、跨轮允许复验。',
    })
    return {
        'cycle_id': cycle_dir.name,
        'cycle_dir': cycle_dir,
        'budget': budget,
        'candidate_configs': candidate_configs,
        'manifest_df': manifest_df,
        'diagnosis': diagnosis,
        'llm_override': llm_override,
        'bridge_inputs': bridge_inputs,
    }
