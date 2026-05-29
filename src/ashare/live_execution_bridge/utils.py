from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List

import pandas as pd


def ensure_dir(path: str | Path) -> Path:
    """确保目录存在。

    Args:
        path: 目录路径。

    Returns:
        Path: 目录对象。
    """
    out = Path(path)
    out.mkdir(parents=True, exist_ok=True)
    return out


def now_str() -> str:
    """生成时间戳字符串。

    Args:
        None

    Returns:
        str: 时间戳文本。
    """
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def load_json(path: str | Path) -> Any:
    """读取 JSON 文件。

    Args:
        path: 文件路径。

    Returns:
        Any: 解析后的对象。
    """
    p = Path(path)
    return json.loads(p.read_text(encoding="utf-8"))


def dump_json(path: str | Path, obj: Any) -> None:
    """写出 JSON 文件。

    Args:
        path: 文件路径。
        obj: 需要写出的对象。

    Returns:
        None
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def write_dataframe(path: str | Path, rows: Iterable[Dict[str, Any]]) -> None:
    """把字典列表写成 CSV。

    Args:
        path: 输出文件路径。
        rows: 行记录集合。

    Returns:
        None
    """
    frame = pd.DataFrame(list(rows))
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(path, index=False, encoding="utf-8-sig")


def normalize_symbol(raw: Any) -> str:
    """规范化证券代码为 600000.SH / 000001.SZ 形式。

    Args:
        raw: 原始证券代码。

    Returns:
        str: 统一后的证券代码。
    """
    text = str(raw).strip().upper()
    if not text:
        return text
    text = text.replace("SHSE.", "").replace("SZSE.", "")
    text = text.replace("SH.", "").replace("SZ.", "")
    if text.endswith(".XSHG"):
        return text.replace(".XSHG", ".SH")
    if text.endswith(".XSHE"):
        return text.replace(".XSHE", ".SZ")
    if text.endswith(".SH") or text.endswith(".SZ") or text.endswith(".BJ"):
        return text
    pure = text.replace(".", "")
    if pure.startswith(("60", "68", "90", "11")):
        return f"{pure[:6]}.SH"
    if pure.startswith(("00", "30", "12", "15")):
        return f"{pure[:6]}.SZ"
    if pure.startswith(("43", "83", "87", "92")):
        return f"{pure[:6]}.BJ"
    return text


def to_gm_symbol(symbol: str) -> str:
    """把内部证券代码转成掘金格式。

    Args:
        symbol: 600000.SH / 000001.SZ 形式代码。

    Returns:
        str: SHSE.600000 / SZSE.000001 形式代码。
    """
    norm = normalize_symbol(symbol)
    if norm.endswith(".SH"):
        return f"SHSE.{norm[:6]}"
    if norm.endswith(".SZ"):
        return f"SZSE.{norm[:6]}"
    if norm.endswith(".BJ"):
        return f"BJSE.{norm[:6]}"
    return norm


def from_gm_symbol(symbol: str) -> str:
    """把掘金格式代码转回内部格式。

    Args:
        symbol: SHSE.600000 / SZSE.000001 形式代码。

    Returns:
        str: 600000.SH / 000001.SZ 形式代码。
    """
    return normalize_symbol(symbol)


def safe_float(value: Any, default: float = 0.0) -> float:
    """安全转浮点。

    Args:
        value: 输入值。
        default: 失败时默认值。

    Returns:
        float: 浮点结果。
    """
    try:
        return float(value)
    except Exception:
        return float(default)


def safe_int(value: Any, default: int = 0) -> int:
    """安全转整数。

    Args:
        value: 输入值。
        default: 失败时默认值。

    Returns:
        int: 整数结果。
    """
    try:
        return int(value)
    except Exception:
        return int(default)


def choose_first_existing(paths: List[str | Path]) -> Path | None:
    """从候选路径中返回首个存在的路径。

    Args:
        paths: 候选路径列表。

    Returns:
        Path | None: 找到的路径或空。
    """
    for item in paths:
        p = Path(item)
        if p.exists():
            return p
    return None
