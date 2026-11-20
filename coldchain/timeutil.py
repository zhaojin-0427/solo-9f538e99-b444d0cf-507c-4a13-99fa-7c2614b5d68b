"""UTC parsing and canonical formatting helpers.

All instants are normalised to UTC. Internally we keep integer microseconds
since the Unix epoch so interval comparisons never depend on float rounding or
string ordering.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

UTC = timezone.utc
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
MAX_US = 9_223_372_036_854_775_807

_ISO_HAS_TIME = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")


def parse_utc(value, field="时间"):
    """Parse an ISO-8601 instant and return an aware UTC datetime.

    An explicit offset is honoured and converted to UTC. A missing offset is
    interpreted as UTC: the application is a UTC-only ledger. ``Z``/``z`` is
    accepted. Date-only strings are rejected because they do not identify an
    outbound instant.
    """
    if value is None:
        raise ValueError(f"{field}不能为空")
    text = str(value).strip()
    if not text:
        raise ValueError(f"{field}不能为空")
    if not _ISO_HAS_TIME.match(text):
        raise ValueError(
            f"{field}必须是 ISO-8601 日期时间（含时分），例如 2026-03-01T08:00:00Z"
        )
    candidate = text[:-1] + "+00:00" if text[-1:] in ("Z", "z") else text
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise ValueError(f"{field}不是合法的 ISO-8601 时间：{text}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    else:
        parsed = parsed.astimezone(UTC)
    if not 1 <= parsed.year <= 9999:
        raise ValueError(f"{field}超出支持范围")
    return parsed


def parse_utc_us(value, field="时间"):
    return datetime_to_us(parse_utc(value, field=field))


def datetime_to_us(value):
    delta = value - EPOCH
    return (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds


def format_utc(value):
    """Canonical wire/storage representation: 2026-03-01T08:00:00.000000Z."""
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def display_utc(value):
    if not value:
        return "至今"
    text = value
    if text.endswith("Z"):
        text = text[:-1]
    if "." in text:
        base, frac = text.split(".", 1)
        frac = frac.rstrip("0")
        text = f"{base}.{frac}" if frac else base
    return text.replace("T", " ") + " UTC"


def month_of(value):
    return value.astimezone(UTC).strftime("%Y-%m")


def validate_month(value):
    text = str(value or "").strip()
    if not re.fullmatch(r"\d{4}-\d{2}", text):
        raise ValueError("月份必须为 YYYY-MM 格式")
    year, month = int(text[:4]), int(text[5:])
    if not 1 <= year <= 9999 or not 1 <= month <= 12:
        raise ValueError("月份不是合法日历月份")
    return text


def now_utc():
    return datetime.now(UTC)


def now_str():
    return format_utc(now_utc())
