# -*- coding: utf-8 -*-
"""Structured candidate artifact generation for V5.1 labs."""

from __future__ import annotations

import ast
import importlib.util
import json
import os
import re
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict

import requests

from hub.io_utils import write_json
from hub.llm_client import LLMClient


MAX_FEATURE_COUNT = 16
FEATURE_NAME_RE = re.compile(r"^(feat|inter)_[A-Za-z0-9_]+$")
ALLOWED_FORMULA_FUNCS = {
    "col",
    "shift",
    "rolling_mean",
    "rolling_std",
    "rolling_sum",
    "safe_div",
    "clip",
    "abs",
    "log1p_abs",
    "zscore",
}
ALLOWED_AST_NODES = (
    ast.Expression,
    ast.BinOp,
    ast.UnaryOp,
    ast.Call,
    ast.Name,
    ast.Load,
    ast.Constant,
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.Pow,
    ast.USub,
    ast.UAdd,
)
TRAIN_OVERRIDE_SCHEMA = {
    "sample_weight_mode": {"type": str, "choices": {"none", "recent_exponential"}},
    "feature_cap": {"type": int, "min": 1, "max": 256},
    "clip_label_quantile": {"type": float, "min": 0.0, "max": 0.2},
}
TRAIN_OVERRIDE_VALUE_ALIASES = {
    "sample_weight_mode": {
        "uniform": "none",
        "balanced": "none",
        "equal": "none",
        "flat": "none",
        "disabled": "none",
        "recent": "recent_exponential",
        "exp": "recent_exponential",
        "exponential": "recent_exponential",
        "recent_decay": "recent_exponential",
        "exponential_decay": "recent_exponential",
    }
}
MODEL_FAMILY_PARAM_SCHEMA = {
    "hist_gbdt": {
        "max_depth": {"type": int, "min": 2, "max": 12},
        "learning_rate": {"type": float, "min": 0.005, "max": 0.5},
        "max_iter": {"type": int, "min": 50, "max": 800},
    },
    "elastic_net": {
        "alpha": {"type": float, "min": 0.0001, "max": 10.0},
        "l1_ratio": {"type": float, "min": 0.0, "max": 1.0},
    },
    "ridge_ranker": {
        "alpha": {"type": float, "min": 0.1, "max": 20.0},
    },
    "extra_trees": {
        "n_estimators": {"type": int, "min": 50, "max": 600},
        "max_depth": {"type": int, "min": 3, "max": 16},
        "min_samples_leaf": {"type": int, "min": 1, "max": 64},
    },
    "random_forest": {
        "n_estimators": {"type": int, "min": 50, "max": 600},
        "max_depth": {"type": int, "min": 3, "max": 16},
        "min_samples_leaf": {"type": int, "min": 1, "max": 64},
    },
    "formula_blend": {
        "top_n": {"type": int, "min": 1, "max": 20},
    },
}
MODEL_FAMILY_ALIASES = {
    "xgboost": "hist_gbdt",
    "xgb": "hist_gbdt",
    "gradient_boosting": "hist_gbdt",
    "extratrees": "extra_trees",
    "rf": "random_forest",
    "randomforest": "random_forest",
}
MODEL_PARAM_ALIASES = {
    "num_trees": "n_estimators",
    "num_boost_round": "n_estimators",
    "iterations": "max_iter",
    "eta": "learning_rate",
    "lambda": "alpha",
}
TREE_STYLE_HINT_PARAMS = {"n_estimators", "num_trees", "min_child_weight", "subsample", "colsample_bytree"}
FEATURE_SPEC_FALLBACK: Dict[str, Any] = {
    "kind": "feature_spec_v1",
    "features": [
        {
            "name": "feat_mom_spread_5_20",
            "formula": "col('ret_5') - col('ret_20')",
            "description": "Short-vs-medium momentum spread.",
        },
        {
            "name": "feat_vol_liq_ratio",
            "formula": "safe_div(col('vol_20'), abs(col('amount_mean_20')) + 1.0)",
            "description": "Volatility scaled by liquidity.",
        },
        {
            "name": "feat_alpha_vol_mix",
            "formula": "safe_div(col('alpha_ret_20_vs_hs300'), abs(col('vol_20')) + 1e-6)",
            "description": "Alpha adjusted by volatility.",
        },
    ],
}
TRAIN_SPEC_FALLBACK: Dict[str, Any] = {
    "kind": "train_override_spec_v1",
    "plan": {
        "sample_weight_mode": "recent_exponential",
        "feature_cap": 80,
        "clip_label_quantile": 0.01,
    },
}
MODEL_SPEC_FALLBACK: Dict[str, Any] = {
    "kind": "model_spec_v1",
    "family": "hist_gbdt",
    "params": {
        "max_depth": 6,
        "learning_rate": 0.05,
        "max_iter": 260,
    },
}


@dataclass
class ProviderTier:
    provider: str
    enabled: bool
    attempts: int
    model: str = ""
    timeout_seconds: int = 90
    base_url: str = ""
    api_key_env: str = ""


def _spec_to_json(spec: Dict[str, Any]) -> str:
    return json.dumps(spec, ensure_ascii=False, indent=2, sort_keys=True)


def _python_literal(value: Any) -> str:
    return repr(value)


def _validate_formula(expr: str) -> None:
    try:
        tree = ast.parse(str(expr or "").strip(), mode="eval")
    except SyntaxError as exc:
        raise ValueError(f"invalid formula syntax: {exc}") from exc
    for node in ast.walk(tree):
        if not isinstance(node, ALLOWED_AST_NODES):
            raise ValueError(f"formula uses unsupported syntax: {type(node).__name__}")
        if isinstance(node, ast.Name) and node.id not in ALLOWED_FORMULA_FUNCS:
            raise ValueError(f"formula uses unknown name: {node.id}")
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name):
                raise ValueError("formula only allows direct helper calls")
            if node.func.id not in ALLOWED_FORMULA_FUNCS:
                raise ValueError(f"formula uses unknown helper: {node.func.id}")


def _coerce_number(value: Any, expected_type: type) -> Any:
    if expected_type is int:
        if isinstance(value, bool):
            raise ValueError("bool is not a valid int")
        return int(value)
    if expected_type is float:
        if isinstance(value, bool):
            raise ValueError("bool is not a valid float")
        return float(value)
    if expected_type is str:
        text = str(value or "").strip()
        if not text:
            raise ValueError("string value is empty")
        return text
    raise ValueError(f"unsupported expected type: {expected_type}")


def _validate_feature_spec(spec: Dict[str, Any]) -> Dict[str, Any]:
    if str(spec.get("kind", "")).strip() != "feature_spec_v1":
        raise ValueError("feature spec kind must be feature_spec_v1")
    features = list(spec.get("features", []) or [])
    if not features:
        raise ValueError("feature spec must contain at least one feature")
    if len(features) > MAX_FEATURE_COUNT:
        raise ValueError(f"feature spec exceeds max feature count {MAX_FEATURE_COUNT}")
    normalized_features = []
    names_seen: set[str] = set()
    for item in features:
        if not isinstance(item, dict):
            raise ValueError("feature entry must be an object")
        name = str(item.get("name", "")).strip()
        formula = str(item.get("formula", "")).strip()
        description = str(item.get("description", "")).strip()
        if not FEATURE_NAME_RE.match(name):
            raise ValueError(f"invalid feature name: {name}")
        if name in names_seen:
            raise ValueError(f"duplicate feature name: {name}")
        if not formula:
            raise ValueError(f"feature {name} missing formula")
        _validate_formula(formula)
        names_seen.add(name)
        normalized_features.append({"name": name, "formula": formula, "description": description})
    return {"kind": "feature_spec_v1", "features": normalized_features}


def _validate_train_override_spec(spec: Dict[str, Any]) -> Dict[str, Any]:
    if str(spec.get("kind", "")).strip() != "train_override_spec_v1":
        raise ValueError("train override spec kind must be train_override_spec_v1")
    plan = dict(spec.get("plan", {}) or {})
    normalized: Dict[str, Any] = {}
    context = dict(spec.get("_context", {}) or {})
    try:
        feature_pool_size = len(list(context.get("feature_pool", []) or []))
    except Exception:
        feature_pool_size = 0
    for key, value in plan.items():
        if key not in TRAIN_OVERRIDE_SCHEMA:
            raise ValueError(f"unsupported training override key: {key}")
        rule = TRAIN_OVERRIDE_SCHEMA[key]
        if key == "sample_weight_mode":
            value = TRAIN_OVERRIDE_VALUE_ALIASES.get(key, {}).get(str(value or "").strip().lower(), value)
        if key == "feature_cap" and isinstance(value, float) and 0.0 < float(value) <= 1.0:
            base_cap = max(feature_pool_size * 8, 32)
            value = max(1, int(round(base_cap * float(value))))
        if key == "clip_label_quantile" and isinstance(value, (list, tuple)) and len(value) == 2:
            try:
                lo = float(value[0])
                hi = float(value[1])
                if 0.0 <= lo < hi <= 1.0:
                    value = min(lo, 1.0 - hi)
            except Exception:
                pass
        coerced = _coerce_number(value, rule["type"])
        if "choices" in rule and coerced not in rule["choices"]:
            raise ValueError(f"{key} must be one of {sorted(rule['choices'])}")
        if "min" in rule and coerced < rule["min"]:
            raise ValueError(f"{key} below minimum {rule['min']}")
        if "max" in rule and coerced > rule["max"]:
            raise ValueError(f"{key} above maximum {rule['max']}")
        normalized[key] = coerced
    return {"kind": "train_override_spec_v1", "plan": normalized}


def _validate_model_spec(spec: Dict[str, Any]) -> Dict[str, Any]:
    if str(spec.get("kind", "")).strip() != "model_spec_v1":
        raise ValueError("model spec kind must be model_spec_v1")
    family = str(spec.get("family", "")).strip().lower()
    family = MODEL_FAMILY_ALIASES.get(family, family)
    raw_params = dict(spec.get("params", {}) or {})
    if family == "hist_gbdt" and any(str(key).strip() in TREE_STYLE_HINT_PARAMS for key in raw_params):
        if any(key in raw_params for key in ("min_child_weight", "subsample", "colsample_bytree")):
            family = "extra_trees"
    if family not in MODEL_FAMILY_PARAM_SCHEMA:
        raise ValueError(f"unsupported generated model family: {family}")
    params: Dict[str, Any] = {}
    for key, value in raw_params.items():
        key_text = str(key or "").strip()
        if not key_text:
            continue
        params[MODEL_PARAM_ALIASES.get(key_text, key_text)] = value
    if family == "hist_gbdt" and "n_estimators" in params and "max_iter" not in params:
        params["max_iter"] = params.pop("n_estimators")
    if family in {"extra_trees", "random_forest"} and "max_iter" in params and "n_estimators" not in params:
        params["n_estimators"] = params.pop("max_iter")
    if family in {"extra_trees", "random_forest"} and "min_child_weight" in params and "min_samples_leaf" not in params:
        raw_leaf = params.pop("min_child_weight")
        try:
            params["min_samples_leaf"] = max(1, int(round(float(raw_leaf))))
        except Exception as exc:
            raise ValueError(f"could not normalize min_child_weight: {exc}") from exc
    for ignored_key in ("subsample", "colsample_bytree", "gamma", "reg_lambda", "reg_alpha"):
        params.pop(ignored_key, None)
    rules = MODEL_FAMILY_PARAM_SCHEMA[family]
    normalized_params: Dict[str, Any] = {}
    for key, value in params.items():
        if key not in rules:
            raise ValueError(f"unsupported model param for {family}: {key}")
        rule = rules[key]
        coerced = _coerce_number(value, rule["type"])
        if "min" in rule and coerced < rule["min"]:
            raise ValueError(f"{family}.{key} below minimum {rule['min']}")
        if "max" in rule and coerced > rule["max"]:
            raise ValueError(f"{family}.{key} above maximum {rule['max']}")
        normalized_params[key] = coerced
    return {"kind": "model_spec_v1", "family": family, "params": normalized_params}


def _compile_feature_module(spec: Dict[str, Any]) -> str:
    lines = [
        "# -*- coding: utf-8 -*-",
        '"""Compiled candidate feature transform module."""',
        "",
        "from __future__ import annotations",
        "",
        "import numpy as np",
        "import pandas as pd",
        "",
        "def _col(df: pd.DataFrame, name: str) -> pd.Series:",
        "    if name in df.columns:",
        "        return pd.to_numeric(df[name], errors='coerce').astype(float)",
        "    return pd.Series(0.0, index=df.index, dtype=float)",
        "",
        "def _safe_div(a: pd.Series, b: pd.Series) -> pd.Series:",
        "    denom = pd.to_numeric(b, errors='coerce').replace(0.0, np.nan)",
        "    out = pd.to_numeric(a, errors='coerce') / denom",
        "    return out.replace([np.inf, -np.inf], np.nan).fillna(0.0).astype(float)",
        "",
        "def _clip(s: pd.Series, lo: float, hi: float) -> pd.Series:",
        "    return pd.to_numeric(s, errors='coerce').clip(lower=lo, upper=hi).astype(float)",
        "",
        "def _zscore(s: pd.Series, window: int) -> pd.Series:",
        "    base = pd.to_numeric(s, errors='coerce').astype(float)",
        "    mean = base.rolling(window=window, min_periods=1).mean()",
        "    std = base.rolling(window=window, min_periods=1).std().replace(0.0, np.nan)",
        "    return ((base - mean) / std).replace([np.inf, -np.inf], np.nan).fillna(0.0).astype(float)",
        "",
        "def transform_features(df: pd.DataFrame) -> pd.DataFrame:",
        "    out = df.copy()",
        "    def col(name: str) -> pd.Series:",
        "        return _col(out, name)",
        "    def shift(series: pd.Series, periods: int) -> pd.Series:",
        "        return pd.to_numeric(series, errors='coerce').shift(int(periods)).astype(float)",
        "    def rolling_mean(series: pd.Series, window: int) -> pd.Series:",
        "        return pd.to_numeric(series, errors='coerce').rolling(window=int(window), min_periods=1).mean().astype(float)",
        "    def rolling_std(series: pd.Series, window: int) -> pd.Series:",
        "        return pd.to_numeric(series, errors='coerce').rolling(window=int(window), min_periods=1).std().fillna(0.0).astype(float)",
        "    def rolling_sum(series: pd.Series, window: int) -> pd.Series:",
        "        return pd.to_numeric(series, errors='coerce').rolling(window=int(window), min_periods=1).sum().astype(float)",
        "    def safe_div(a: pd.Series, b: pd.Series) -> pd.Series:",
        "        return _safe_div(a, b)",
        "    def clip(series: pd.Series, lo: float, hi: float) -> pd.Series:",
        "        return _clip(series, lo, hi)",
        "    def abs(series: pd.Series) -> pd.Series:",
        "        return pd.to_numeric(series, errors='coerce').abs().astype(float)",
        "    def log1p_abs(series: pd.Series) -> pd.Series:",
        "        return np.log1p(pd.to_numeric(series, errors='coerce').abs()).astype(float)",
        "    def zscore(series: pd.Series, window: int) -> pd.Series:",
        "        return _zscore(series, window)",
    ]
    for item in list(spec["features"]):
        lines.append(f"    out[{item['name']!r}] = ({item['formula']}).replace([np.inf, -np.inf], np.nan).fillna(0.0).astype(float)")
    lines.extend(["    return out", ""])
    return "\n".join(lines)


def _compile_train_override_module(spec: Dict[str, Any]) -> str:
    payload = _python_literal(dict(spec["plan"]))
    return "\n".join(
        [
            "# -*- coding: utf-8 -*-",
            '"""Compiled training override module."""',
            "",
            "from __future__ import annotations",
            "",
            "from typing import Any, Dict",
            "",
            "def override_training_plan(plan: Dict[str, Any]) -> Dict[str, Any]:",
            "    out = dict(plan)",
            f"    out.update({payload})",
            "    return out",
            "",
        ]
    )


def _compile_model_module(spec: Dict[str, Any]) -> str:
    family = spec["family"]
    params_literal = _python_literal(dict(spec["params"]))
    body_by_family = {
        "hist_gbdt": [
            "from sklearn.ensemble import HistGradientBoostingRegressor",
            "",
            "def build_model(random_state: int = 42):",
            f"    params = dict({params_literal})",
            "    params.setdefault('random_state', random_state)",
            "    return HistGradientBoostingRegressor(**params)",
        ],
        "elastic_net": [
            "from sklearn.linear_model import ElasticNet",
            "",
            "def build_model(random_state: int = 42):",
            f"    params = dict({params_literal})",
            "    params.setdefault('random_state', random_state)",
            "    return ElasticNet(**params)",
        ],
        "ridge_ranker": [
            "from hub.model_families import StableRidgeRanker",
            "",
            "def build_model(random_state: int = 42):",
            f"    params = dict({params_literal})",
            "    params.setdefault('random_state', random_state)",
            "    return StableRidgeRanker(**params)",
        ],
        "extra_trees": [
            "from sklearn.ensemble import ExtraTreesRegressor",
            "",
            "def build_model(random_state: int = 42):",
            f"    params = dict({params_literal})",
            "    params.setdefault('random_state', random_state)",
            "    params.setdefault('n_jobs', -1)",
            "    return ExtraTreesRegressor(**params)",
        ],
        "random_forest": [
            "from sklearn.ensemble import RandomForestRegressor",
            "",
            "def build_model(random_state: int = 42):",
            f"    params = dict({params_literal})",
            "    params.setdefault('random_state', random_state)",
            "    params.setdefault('n_jobs', -1)",
            "    return RandomForestRegressor(**params)",
        ],
        "formula_blend": [
            "from hub.model_families import FormulaModel",
            "",
            "def build_model(random_state: int = 42):",
            f"    params = dict({params_literal})",
            "    top_n = int(params.get('top_n', 5) or 5)",
            "    base_features = ['ret_5', 'ret_20', 'alpha_ret_20_vs_hs300', 'vol_20', 'amount_z_20']",
            "    weights = {name: float(top_n - idx) / float(top_n) for idx, name in enumerate(base_features[:top_n])}",
            "    return FormulaModel(weights=weights)",
        ],
    }
    return "\n".join(
        [
            "# -*- coding: utf-8 -*-",
            '"""Compiled generated model module."""',
            "",
            "from __future__ import annotations",
            "",
            *body_by_family[family],
            "",
        ]
    )


def _load_module_validation(path: Path) -> Dict[str, Any]:
    try:
        source = path.read_text(encoding="utf-8")
        compile(source, str(path), "exec")
        spec = importlib.util.spec_from_file_location(path.stem, path)
        module = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        spec.loader.exec_module(module)
        return {"ok": True, "error": ""}
    except Exception:
        return {"ok": False, "error": traceback.format_exc()}


class CodegenLab:
    """Generate structured specs and deterministically compile candidate lab artifacts."""

    def __init__(self, llm_client: LLMClient):
        self.llm_client = llm_client
        self.cfg = dict(getattr(llm_client, "cfg", {}) or {})

    def _spec_prompt(self, kind: str) -> str:
        if kind == "feature_spec":
            return (
                "You are a quant research feature-spec generator. "
                "Return a JSON object only. "
                "The object must be {\"kind\":\"feature_spec_v1\",\"features\":[...]} and each feature must contain name, formula, description. "
                "Feature names must start with feat_ or inter_. "
                "Formula may only use these helpers: col, shift, rolling_mean, rolling_std, rolling_sum, safe_div, clip, abs, log1p_abs, zscore. "
                "Formula must be a pure arithmetic expression, not Python statements."
            )
        if kind == "train_override_spec":
            return (
                "You are a quant research training-plan spec generator. "
                "Return a JSON object only. "
                "The object must be {\"kind\":\"train_override_spec_v1\",\"plan\":{...}}. "
                "The plan may only contain sample_weight_mode, feature_cap, clip_label_quantile. "
                "sample_weight_mode must be exactly one of: none, recent_exponential. "
                "Do not use balanced, uniform, equal, class_balanced, or recent. "
                "feature_cap must be an integer feature count like 20, 40, 80, not a ratio like 0.95. "
                "clip_label_quantile must be one float like 0.01 or 0.05, not a [low, high] pair. "
                "Valid example: {\"kind\":\"train_override_spec_v1\",\"plan\":{\"sample_weight_mode\":\"recent_exponential\",\"feature_cap\":80,\"clip_label_quantile\":0.01}}. "
                "Another valid example: {\"kind\":\"train_override_spec_v1\",\"plan\":{\"sample_weight_mode\":\"none\",\"feature_cap\":40}}."
            )
        return (
            "You are a quant research generated-model spec generator. "
            "Return a JSON object only. "
            "The object must be {\"kind\":\"model_spec_v1\",\"family\":\"...\",\"params\":{...}}. "
            "Family must be one of hist_gbdt, elastic_net, ridge_ranker, extra_trees, random_forest, formula_blend. "
            "For hist_gbdt use only max_depth, learning_rate, max_iter. "
            "For extra_trees/random_forest use only n_estimators, max_depth, min_samples_leaf. "
            "For elastic_net use only alpha, l1_ratio. "
            "For ridge_ranker use only alpha. "
            "For formula_blend use only top_n. "
            "Do not emit xgboost-specific params like min_child_weight, colsample_bytree, subsample, gamma, reg_alpha, reg_lambda."
        )

    def _intent_prompt(self, kind: str) -> str:
        if kind == "feature_spec":
            return (
                "You are a quant research design planner. "
                "Return a JSON object only describing the feature intent before implementation. "
                "The object must contain short fields: objective, transformations, risk_guard, preferred_feature_count. "
                "Focus on useful feature ideas, not executable syntax."
            )
        if kind == "train_override_spec":
            return (
                "You are a quant research design planner. "
                "Return a JSON object only describing the training-plan intent before implementation. "
                "The object must contain short fields: weighting_goal, feature_budget_goal, label_clip_goal, rationale. "
                "Describe intent, not final schema values."
            )
        return (
            "You are a quant research design planner. "
            "Return a JSON object only describing the model intent before implementation. "
            "The object must contain short fields: model_bias, complexity_budget, regularization_goal, rationale. "
            "Describe the desired modeling idea, not final framework-specific params."
        )

    def _spec_fallback(self, kind: str) -> Dict[str, Any]:
        if kind == "feature_spec":
            return dict(FEATURE_SPEC_FALLBACK)
        if kind == "train_override_spec":
            return dict(TRAIN_SPEC_FALLBACK)
        return dict(MODEL_SPEC_FALLBACK)

    def _validator(self, kind: str):
        if kind == "feature_spec":
            return _validate_feature_spec
        if kind == "train_override_spec":
            return _validate_train_override_spec
        return _validate_model_spec

    def _compiler(self, kind: str):
        if kind == "feature_spec":
            return _compile_feature_module
        if kind == "train_override_spec":
            return _compile_train_override_module
        return _compile_model_module

    def _provider_tiers(self) -> list[ProviderTier]:
        cfg = self.cfg
        if not bool(cfg.get("enabled", False)):
            return []
        raw_tiers = list(cfg.get("provider_tiers", []) or [])
        if raw_tiers:
            tiers: list[ProviderTier] = []
            for item in raw_tiers:
                if not isinstance(item, dict):
                    continue
                tiers.append(
                    ProviderTier(
                        provider=str(item.get("provider", "")).strip(),
                        enabled=bool(item.get("enabled", True)),
                        attempts=max(int(item.get("attempts", 1) or 1), 1),
                        model=str(item.get("model", "") or ""),
                        timeout_seconds=max(int(item.get("timeout_seconds", cfg.get("timeout_seconds", 90)) or 90), 15),
                        base_url=str(item.get("base_url", "") or ""),
                        api_key_env=str(item.get("api_key_env", "") or ""),
                    )
                )
            return tiers
        return [
            ProviderTier(
                provider="local_ollama",
                enabled=True,
                attempts=1,
                model=str(cfg.get("local_ollama_model", "qwen2.5:7b") or "qwen2.5:7b"),
                timeout_seconds=max(int(cfg.get("local_ollama_timeout_seconds", 90) or 90), 15),
                base_url=str(cfg.get("local_ollama_base_url", "http://127.0.0.1:11434") or "http://127.0.0.1:11434"),
            ),
            ProviderTier(
                provider="deepseek",
                enabled=bool(os.environ.get(str(cfg.get("deepseek_api_key_env", "DEEPSEEK_API_KEY") or "DEEPSEEK_API_KEY"), "").strip()),
                attempts=max(int(cfg.get("deepseek_attempts", 2) or 2), 1),
                model=str(cfg.get("deepseek_model", "deepseek-chat") or "deepseek-chat"),
                timeout_seconds=max(int(cfg.get("deepseek_timeout_seconds", cfg.get("timeout_seconds", 90)) or 90), 15),
                base_url=str(cfg.get("deepseek_base_url", "https://api.deepseek.com/v1") or "https://api.deepseek.com/v1"),
                api_key_env=str(cfg.get("deepseek_api_key_env", "DEEPSEEK_API_KEY") or "DEEPSEEK_API_KEY"),
            ),
            ProviderTier(
                provider="openai",
                enabled=bool(os.environ.get(str(cfg.get("openai_api_key_env", "OPENAI_API_KEY") or "OPENAI_API_KEY"), "").strip()),
                attempts=max(int(cfg.get("openai_attempts", 2) or 2), 1),
                model=str(cfg.get("openai_model", "gpt-4.1") or "gpt-4.1"),
                timeout_seconds=max(int(cfg.get("openai_timeout_seconds", 120) or 120), 15),
                base_url=str(cfg.get("openai_base_url", "https://api.openai.com/v1") or "https://api.openai.com/v1"),
                api_key_env=str(cfg.get("openai_api_key_env", "OPENAI_API_KEY") or "OPENAI_API_KEY"),
            ),
        ]

    def _call_provider_json(self, tier: ProviderTier, system_prompt: str, user_prompt: str, temperature: float) -> Dict[str, Any]:
        if tier.provider == "local_ollama":
            url = f"{tier.base_url.rstrip('/')}/api/generate"
            payload = {
                "model": tier.model,
                "stream": False,
                "format": "json",
                "prompt": f"{system_prompt}\n\n{user_prompt}",
                "options": {"temperature": temperature},
            }
            resp = requests.post(url, json=payload, timeout=tier.timeout_seconds)
            resp.raise_for_status()
            data = resp.json()
            text = str(data.get("response", "") or "").strip()
            parsed = json.loads(text)
            if not isinstance(parsed, dict):
                raise ValueError("local_ollama did not return a JSON object")
            return parsed
        if tier.provider == "deepseek":
            client = LLMClient(
                {
                    "enabled": True,
                    "base_url": tier.base_url.rstrip("/v1"),
                    "api_key_env": tier.api_key_env,
                    "model": tier.model,
                    "timeout_seconds": tier.timeout_seconds,
                }
            )
            result = client.chat_json(system_prompt=system_prompt, user_prompt=user_prompt, temperature=temperature)
            if not isinstance(result, dict) or not result:
                raise ValueError("deepseek returned empty payload")
            return result
        if tier.provider == "openai":
            client = LLMClient(
                {
                    "enabled": True,
                    "base_url": tier.base_url,
                    "api_key_env": tier.api_key_env,
                    "model": tier.model,
                    "timeout_seconds": tier.timeout_seconds,
                }
            )
            result = client.chat_json(system_prompt=system_prompt, user_prompt=user_prompt, temperature=temperature)
            if not isinstance(result, dict) or not result:
                raise ValueError("openai returned empty payload")
            return result
        raise ValueError(f"unsupported provider tier: {tier.provider}")

    def _request_spec(self, tier: ProviderTier, kind: str, context: Dict[str, Any]) -> Dict[str, Any]:
        user_prompt = json.dumps({"kind": kind, "context": context}, ensure_ascii=False)
        payload = self._call_provider_json(tier, self._spec_prompt(kind), user_prompt, temperature=0.15)
        if kind == "train_override_spec" and isinstance(payload, dict):
            payload["_context"] = dict(context or {})
        return payload

    def _request_intent(self, tier: ProviderTier, kind: str, context: Dict[str, Any]) -> Dict[str, Any]:
        user_prompt = json.dumps({"kind": kind, "context": context}, ensure_ascii=False)
        payload = self._call_provider_json(tier, self._intent_prompt(kind), user_prompt, temperature=0.2)
        return payload if isinstance(payload, dict) else {}

    def _request_spec_from_intent(
        self,
        tier: ProviderTier,
        kind: str,
        context: Dict[str, Any],
        intent: Dict[str, Any],
    ) -> Dict[str, Any]:
        user_prompt = json.dumps({"kind": kind, "context": context, "intent": intent}, ensure_ascii=False)
        payload = self._call_provider_json(tier, self._spec_prompt(kind), user_prompt, temperature=0.1)
        if kind == "train_override_spec" and isinstance(payload, dict):
            payload["_context"] = dict(context or {})
        return payload

    def _repair_spec(
        self,
        tier: ProviderTier,
        kind: str,
        context: Dict[str, Any],
        intent: Dict[str, Any],
        current_spec: Dict[str, Any],
        validation_error: str,
        attempt: int,
    ) -> Dict[str, Any]:
        user_prompt = json.dumps(
            {
                "kind": kind,
                "attempt": attempt,
                "context": context,
                "intent": intent,
                "current_spec": current_spec,
                "validation_error": validation_error,
                "instruction": "Return a corrected JSON object only. Preserve valid parts, fix only the schema or legality issue.",
            },
            ensure_ascii=False,
        )
        payload = self._call_provider_json(tier, self._spec_prompt(kind), user_prompt, temperature=0.0)
        if kind == "train_override_spec" and isinstance(payload, dict):
            payload["_context"] = dict(context or {})
        return payload

    def _judge_spec(self, kind: str, raw_spec: Dict[str, Any]) -> Dict[str, Any]:
        validator = self._validator(kind)
        compiler = self._compiler(kind)
        normalized_spec = validator(dict(raw_spec or {}))
        module_source = compiler(normalized_spec)
        return {"normalized_spec": normalized_spec, "module_source": module_source}

    def _build_artifact(self, workspace_dir: Path, artifact_name: str, kind: str, context: Dict[str, Any]) -> Dict[str, Any]:
        spec_path = workspace_dir / f"{artifact_name}.spec.json"
        module_path = workspace_dir / f"{artifact_name}.py"
        fallback = self._spec_fallback(kind)
        attempts: list[Dict[str, Any]] = []
        tiers = [tier for tier in self._provider_tiers() if tier.enabled]
        if not tiers:
            spec_path.write_text(_spec_to_json(fallback), encoding="utf-8")
            return {
                "ok": False,
                "error": "no_enabled_codegen_provider",
                "spec_path": str(spec_path),
                "module_path": str(module_path),
                "spec_kind": kind,
                "selected_provider": "",
                "repair_attempts": attempts,
                "repair_exhausted": True,
                "fallback_used": True,
            }

        last_error = "unknown_codegen_failure"
        current_spec: Dict[str, Any] = {}
        current_intent: Dict[str, Any] = {}
        for tier in tiers:
            for provider_attempt in range(tier.attempts):
                action = "generate" if provider_attempt == 0 else "repair"
                try:
                    if provider_attempt == 0 or not current_spec:
                        current_intent = self._request_intent(tier, kind, context)
                        current_spec = self._request_spec_from_intent(tier, kind, context, current_intent)
                    else:
                        current_spec = self._repair_spec(tier, kind, context, current_intent, current_spec, last_error, provider_attempt)
                    judged = self._judge_spec(kind, current_spec)
                    spec_path.write_text(_spec_to_json(judged["normalized_spec"]), encoding="utf-8")
                    module_path.write_text(str(judged["module_source"]), encoding="utf-8")
                    module_validation = _load_module_validation(module_path)
                    if not module_validation.get("ok"):
                        raise ValueError(module_validation.get("error", "compiled module validation failed"))
                    attempts.append(
                        {
                            "provider": tier.provider,
                            "model": tier.model,
                            "attempt": provider_attempt,
                            "action": action,
                            "ok": True,
                            "intent": current_intent,
                            "error": "",
                        }
                    )
                    return {
                        "ok": True,
                        "error": "",
                        "spec_path": str(spec_path),
                        "module_path": str(module_path),
                        "spec_kind": kind,
                        "selected_provider": tier.provider,
                        "selected_model": tier.model,
                        "selected_intent": current_intent,
                        "repair_attempts": attempts,
                        "repair_exhausted": False,
                        "fallback_used": False,
                    }
                except Exception as exc:
                    last_error = traceback.format_exc() if not isinstance(exc, ValueError) else str(exc)
                    attempts.append(
                        {
                            "provider": tier.provider,
                            "model": tier.model,
                            "attempt": provider_attempt,
                            "action": action,
                            "ok": False,
                            "intent": current_intent,
                            "error": last_error,
                        }
                    )
            current_spec = {}
            current_intent = {}

        spec_path.write_text(_spec_to_json(fallback), encoding="utf-8")
        module_path.write_text(self._compiler(kind)(self._validator(kind)(fallback)), encoding="utf-8")
        return {
            "ok": False,
            "error": last_error,
            "spec_path": str(spec_path),
            "module_path": str(module_path),
            "spec_kind": kind,
            "selected_provider": "fallback",
            "selected_model": "",
            "selected_intent": {},
            "repair_attempts": attempts,
            "repair_exhausted": True,
            "fallback_used": True,
        }

    def build_workspace(self, workspace_dir: Path, context: Dict[str, Any]) -> Dict[str, Any]:
        workspace_dir.mkdir(parents=True, exist_ok=True)
        feature_result = self._build_artifact(workspace_dir, "feature_pack", "feature_spec", context)
        train_result = self._build_artifact(workspace_dir, "train_override", "train_override_spec", context)
        model_result = self._build_artifact(workspace_dir, "generated_model", "model_spec", context)
        validations = {
            "feature_pack": feature_result,
            "train_override": train_result,
            "generated_model": model_result,
        }
        write_json(workspace_dir / "workspace_validation.json", validations)
        return {
            "workspace_dir": str(workspace_dir),
            "feature_pack_path": feature_result["module_path"],
            "feature_pack_spec_path": feature_result["spec_path"],
            "train_override_path": train_result["module_path"],
            "train_override_spec_path": train_result["spec_path"],
            "generated_model_path": model_result["module_path"],
            "generated_model_spec_path": model_result["spec_path"],
            "validations": validations,
        }
