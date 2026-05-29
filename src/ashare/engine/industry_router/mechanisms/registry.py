from __future__ import annotations

from typing import Dict, List

from ..contracts import MECHANISM_GROUPS
from .macro_style import POLICY as MACRO_STYLE_POLICY
from .price_inventory import POLICY as PRICE_INVENTORY_POLICY
from .trend_capex import POLICY as TREND_CAPEX_POLICY

_POLICY_MAP: Dict[str, object] = {
    TREND_CAPEX_POLICY.name: TREND_CAPEX_POLICY,
    PRICE_INVENTORY_POLICY.name: PRICE_INVENTORY_POLICY,
    MACRO_STYLE_POLICY.name: MACRO_STYLE_POLICY,
}

assert tuple(_POLICY_MAP.keys()) == MECHANISM_GROUPS


def get_policy_map() -> Dict[str, object]:
    return dict(_POLICY_MAP)


def get_mechanism_policies() -> List[object]:
    return [dict(_POLICY_MAP)[name] for name in MECHANISM_GROUPS]
