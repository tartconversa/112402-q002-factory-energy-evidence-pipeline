"""通用校验、时间和版本排序辅助函数。"""
from __future__ import annotations

import calendar
import datetime as _dt
import hashlib
import json
import math
import re
from typing import Any

from .errors import ValidationError

PERIOD_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MAPPING_STATUSES = ("confirmed", "partial", "pending")
CONFIRMATION_STATUSES = ("confirmed", "pending")


def utc_now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def validate_period(period: Any, field: str = "period") -> str:
    text = "" if period is None else str(period).strip()
    if not PERIOD_RE.match(text):
        raise ValidationError(f"{field} 必须是 YYYY-MM，收到 {period!r}")
    return text


def period_start(period: str) -> str:
    return f"{validate_period(period)}-01"


def period_end(period: str) -> str:
    year, month = map(int, validate_period(period).split("-"))
    last = calendar.monthrange(year, month)[1]
    return f"{year:04d}-{month:02d}-{last:02d}"


def day_before(date_text: str) -> str:
    return (_dt.date.fromisoformat(validate_date(date_text, "date")) - _dt.timedelta(days=1)).isoformat()


def day_after(date_text: str) -> str:
    return (_dt.date.fromisoformat(validate_date(date_text, "date")) + _dt.timedelta(days=1)).isoformat()


def validate_date(value: Any, field: str) -> str:
    text = "" if value is None else str(value).strip()
    if not DATE_RE.match(text):
        raise ValidationError(f"{field} 必须是 YYYY-MM-DD，收到 {value!r}")
    try:
        _dt.date.fromisoformat(text)
    except ValueError as exc:
        raise ValidationError(f"{field} 不是有效日期：{value!r}") from exc
    return text


def parse_number(value: Any, field: str, *, required: bool = True) -> float | None:
    if value is None or (isinstance(value, str) and value.strip() == ""):
        if required:
            raise ValidationError(f"{field} 不能为空")
        return None
    text = str(value).strip()
    try:
        number = float(text)
    except ValueError as exc:
        raise ValidationError(f"{field} 不是数值：{value!r}") from exc
    if not math.isfinite(number):
        raise ValidationError(f"{field} 必须是有限数值：{value!r}")
    return number


def parse_revision(value: Any) -> int:
    text = "" if value is None else str(value).strip()
    if text == "":
        raise ValidationError("修订版本不能为空")
    try:
        revision = int(text)
    except ValueError as exc:
        raise ValidationError(f"修订版本必须是整数，收到 {value!r}") from exc
    if revision < 0:
        raise ValidationError(f"修订版本不能为负数：{revision}")
    return revision


def version_key(version: Any) -> tuple:
    """自然排序版本号，例如 v2 > v1、10 > 9。"""
    text = str(version)
    key: list[tuple[int, Any]] = []
    for piece in re.split(r"(\d+)", text):
        if not piece:
            continue
        if piece.isdigit():
            key.append((0, int(piece)))
        else:
            key.append((1, piece.lower()))
    return tuple(key) or ((1, ""),)


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def payload_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def text_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def normalize_scope(value: Any) -> str | None:
    """把常见来源写法归一为 final 或 includes-rework。"""
    text = text_or_none(value)
    if text is None:
        return None
    compact = re.sub(r"[\s_\-]+", "", text).lower()
    if compact in {"final", "finalgoodunits", "goodunitsfinal", "goodunits", "qualified", "合格品", "合格品最终"}:
        return "final"
    if compact in {
        "includesrework",
        "reworkincluded",
        "withrework",
        "含返工",
        "含返工品",
        "返工品计入",
    }:
        return "includes-rework"
    return f"unknown:{text}"
