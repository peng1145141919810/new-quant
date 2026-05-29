
# -*- coding: utf-8 -*-
from __future__ import annotations
import json, re
from pathlib import Path

def as_list(x):
    if x is None:
        return []
    if isinstance(x, list):
        return x
    return [x]

def safe_get_dict(x):
    return x if isinstance(x, dict) else {}

def collect_label_horizons(brief):
    hs=[]
    la=safe_get_dict(brief.get('label_actions'))
    for k in ['primary_horizons','secondary_horizons']:
        for v in as_list(la.get(k,[])):
            try:
                hs.append(int(v))
            except (ValueError, TypeError):
                pass
    for thesis in as_list(brief.get('research_thesis')):
        if isinstance(thesis, dict):
            for lbl in as_list(thesis.get('target_labels',[])):
                m=re.search(r'(\d+)d', str(lbl))
                if m: hs.append(int(m.group(1)))
        elif isinstance(thesis, str):
            for m in re.finditer(r'(\d+)d', thesis):
                hs.append(int(m.group(1)))
    for exp in as_list(brief.get('candidate_experiments')):
        if isinstance(exp, dict):
            for v in as_list(exp.get('labels',[])):
                try:
                    hs.append(int(v))
                except (ValueError, TypeError):
                    m=re.search(r'(\d+)', str(v))
                    if m: hs.append(int(m.group(1)))
        elif isinstance(exp, str):
            for m in re.finditer(r'(\d+)', exp):
                hs.append(int(m.group(1)))
    hs=sorted(set(h for h in hs if 0 < h <= 60))
    return hs or [5, 10, 20]

def feature_profiles_from_brief(brief):
    profiles=[]
    texts=[]
    fa=safe_get_dict(brief.get('feature_actions'))
    for section in ['recompute_now','build_now_from_existing_data','new_feature_candidates']:
        for item in as_list(fa.get(section,[])):
            if isinstance(item, dict):
                texts.append((item.get('feature_name','')+' '+item.get('definition','')+' '+item.get('reason','')).lower())
            else:
                texts.append(str(item).lower())
    for thesis in as_list(brief.get('research_thesis')):
        if isinstance(thesis, dict):
            texts.extend([str(x).lower() for x in as_list(thesis.get('required_features',[]))])
            texts.append(str(thesis.get('title','')).lower())
            texts.append(str(thesis.get('hypothesis','')).lower())
        else:
            texts.append(str(thesis).lower())
    for exp in as_list(brief.get('candidate_experiments')):
        if isinstance(exp, dict):
            texts.extend([str(x).lower() for x in as_list(exp.get('features',[]))])
            texts.append(str(exp.get('hypothesis','')).lower())
        else:
            texts.append(str(exp).lower())
    blob=' '.join(texts)
    if any(k in blob for k in ['event_','shareholder','buyback','earnings','supply_shock','litigation','actor_role','share_change','surprise','density','回购','增减持','事件','业绩','诉讼','风控','数据刷新']):
        profiles.append('generated_feature_pack')
    if any(k in blob for k in ['momentum','cross section','cross_section','横截面','价量']):
        profiles.append('momentum_cross_section')
    if any(k in blob for k in ['vol','liq','quality','turnover','流动性','波动','质量']):
        profiles.append('vol_liq_quality')
    return list(dict.fromkeys(profiles or ['generated_feature_pack']))

def preferred_models(brief):
    models=[]
    ma=safe_get_dict(brief.get('model_actions'))
    for sect in ['promote','keep_as_baseline']:
        for item in as_list(ma.get(sect,[])):
            if isinstance(item, dict) and item.get('model_family'):
                models.append(str(item['model_family']))
            elif isinstance(item, str):
                models.append(item)
    for exp in as_list(brief.get('candidate_experiments')):
        if isinstance(exp, dict):
            models.extend([str(x) for x in as_list(exp.get('models',[])) if x])
    for d in ['xgboost_gpu','ridge_ranker','lightgbm_auto']:
        if d not in models:
            models.append(d)
    return list(dict.fromkeys(models))

def ban_models(brief):
    bans=[]
    ma=safe_get_dict(brief.get('model_actions'))
    for item in as_list(ma.get('branch_pause',[])):
        if isinstance(item, dict):
            branch=str(item.get('branch',''))
            if 'annual_report_title_only' in branch:
                bans.append('annual_report_title_only_alpha')
    return list(dict.fromkeys(bans))

def route_override_from_brief(brief):
    weights={'feature':2,'data':2,'risk':1,'portfolio':1,'training':1,'model':1,'hybrid':1}
    thesis=as_list(brief.get('research_thesis'))
    exps=as_list(brief.get('candidate_experiments'))
    if len(thesis) >= 3:
        weights['feature'] += 1; weights['hybrid'] += 1
    if len(exps) >= 6:
        weights['feature'] += 1; weights['portfolio'] += 1
    if brief.get('data_actions'):
        weights['data'] += 1
    text=json.dumps(brief, ensure_ascii=False)
    if any(k in text for k in ['增持','减持','回购','supply_shock','earnings','业绩','事件驱动']):
        weights['feature'] += 1; weights['portfolio'] += 1
    if any(k in text for k in ['风险','诉讼','终止上市','风险提示','monitor_metrics']):
        weights['risk'] += 1
    if brief.get('model_actions'):
        weights['model'] += 1
    for k in weights:
        weights[k] = min(max(weights[k], 1), 5)
    return weights

def top_ks_from_horizons(hs):
    return [10, 20] if min(hs) <= 5 else [20, 30]

def rebuild_from_brief(brief_path, bridge_dir):
    brief=json.loads(Path(brief_path).read_text(encoding='utf-8'))
    hs=collect_label_horizons(brief)
    candidate_override={
        'feature_profiles': feature_profiles_from_brief(brief),
        'label_horizons': hs,
        'top_ks': top_ks_from_horizons(hs),
        'preferred_model_families': preferred_models(brief),
        'ban_model_families': ban_models(brief),
    }
    route_override=route_override_from_brief(brief)
    enriched_context={
        'research_thesis': brief.get('research_thesis',[]),
        'why_now': brief.get('why_now',''),
        'priority_events': brief.get('priority_events',[]),
        'data_actions': brief.get('data_actions',[]),
        'feature_actions': brief.get('feature_actions',[]),
        'label_actions': brief.get('label_actions',[]),
        'model_actions': brief.get('model_actions',[]),
        'portfolio_actions': brief.get('portfolio_actions',[]),
        'risk_actions': brief.get('risk_actions',[]),
        'candidate_experiments': brief.get('candidate_experiments',[]),
        'stop_conditions': brief.get('stop_conditions',[]),
        'ban_items': brief.get('ban_items',[]),
    }
    bridge=Path(bridge_dir)
    bridge.mkdir(parents=True, exist_ok=True)
    (bridge/'candidate_override.json').write_text(json.dumps(candidate_override, ensure_ascii=False, indent=2), encoding='utf-8')
    (bridge/'llm_route_override.json').write_text(json.dumps(route_override, ensure_ascii=False, indent=2), encoding='utf-8')
    (bridge/'enriched_context.json').write_text(json.dumps(enriched_context, ensure_ascii=False, indent=2), encoding='utf-8')
    print('bridge rebuilt to', bridge)

if __name__ == '__main__':
    rebuild_from_brief('research_brief.json', 'bridge')
