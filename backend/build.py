import hashlib
import json
import re
from datetime import datetime

KIND = {"IN": "进闸", "OUT": "出闸", "RS": "倒箱"}
BOX = re.compile(r"^[A-Z]{4}\d{7}$")

# 字段字典：贴源原文里认得的键。其余顶层键都算新键。
KNOWN_KEYS = ("scan", "box", "at", "gate")

# 原文键收进明细层后对应的列。
KNOWN_COLUMNS = {
    "scan": "event_kind",
    "box": "container_no",
    "at": "event_at",
    "gate": "terminal_code",
}

# 扩列办法只有两种，选定后钉住，所有业务日统一使用。
POLICY_NAMES = {
    "drop": "丢掉字典外的键再入库",
    "expand": "把新键扩进明细列",
}

OUTCOME_NAMES = {
    "pending": "待定",
    "dropped": "已丢掉",
    "expanded": "已扩进",
    "deviation": "偏了未落库",
    "kept": "已对照",
}


def extra_keys(payload: dict):
    """原文里字典外的顶层键。键名为空的键不算新键，也不进新键队列。"""
    found = []
    for key, value in payload.items():
        name = str(key)
        if name in KNOWN_KEYS:
            continue
        if not name.strip():
            continue
        found.append((name, value))
    return found


def empty_key_count(payload: dict) -> int:
    """键名为空、不进新键队列的原文键数量。"""
    count = 0
    for key in payload:
        name = str(key)
        if name in KNOWN_KEYS:
            continue
        if not name.strip():
            count += 1
    return count


def value_text(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def detail_column_for(key_name: str, taken: set[str] | None = None) -> str:
    """新键扩进明细层后的列名。加 ext_ 前缀，避免撞上明细自有列。"""
    name = f"ext_{key_name}".replace("\x00", "").replace('"', "")
    encoded = name.encode("utf-8")
    if len(encoded) > 63:
        digest = hashlib.sha1(encoded).hexdigest()[:8]
        name = encoded[:50].decode("utf-8", "ignore") + f"_{digest}"
    name = name or "ext_key"
    if taken is None:
        return name
    base = name
    index = 2
    while name in taken:
        suffix = f"_{index}"
        name = base[: 63 - len(suffix)] + suffix
        index += 1
    return name


def clean_scan(payload: dict):
    scan = str(payload.get("scan") or "").strip().upper()
    box = str(payload.get("box") or "").strip().upper()
    stamp = str(payload.get("at") or "").strip()
    if scan not in KIND:
        return None, "事件码不在字典"
    if not BOX.match(box):
        return None, "箱号不是四位字母加七位数字"
    try:
        event_at = datetime.fromisoformat(stamp)
    except ValueError:
        return None, "扫描时间无法读取"
    return {
        "container_no": box,
        "event_kind": KIND[scan],
        "event_at": event_at,
    }, None
