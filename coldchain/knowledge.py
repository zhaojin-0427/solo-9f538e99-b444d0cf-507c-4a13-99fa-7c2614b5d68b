"""Temperature-zone knowledge ledger.

A knowledge version is an immutable, complete snapshot of every SKU's
[valid_from, valid_to) zone intervals. A correction replaces the whole group
for one SKU and copies every other SKU forward into a new version number.
Because versions are never edited, a later back-dated correction cannot
rewrite what an older version reported.
"""

from __future__ import annotations

import base64
import bisect
import hashlib
import hmac
import json
import re
import sqlite3

from .db import transaction
from .errors import ConflictError, NotFoundError, ValidationError
from .timeutil import (
    datetime_to_us,
    format_utc,
    now_str,
    parse_utc,
)

MAX_INTERVALS = 200
MAX_SKU_LENGTH = 64
MAX_ZONE_LENGTH = 32
MAX_REASON_LENGTH = 500
MAX_VERSION_NUMBER = 2_147_483_647
MAX_TOKEN_LENGTH = 65_536
REVIEW_TOKEN_VERSION = 1

_ZONE_FORBIDDEN = re.compile(r"[\x00-\x1f\x7f]")


def normalize_sku(value):
    text = str(value or "").strip()
    if not text:
        raise ValidationError("SKU 不能为空")
    if len(text) > MAX_SKU_LENGTH:
        raise ValidationError(f"SKU 不能超过 {MAX_SKU_LENGTH} 字符")
    if _ZONE_FORBIDDEN.search(text):
        raise ValidationError("SKU 含控制字符")
    return text


def normalize_zone(value):
    text = " ".join(str(value or "").split())
    if not text:
        raise ValidationError("温区不能为空")
    if len(text) > MAX_ZONE_LENGTH:
        raise ValidationError(f"温区名称不能超过 {MAX_ZONE_LENGTH} 字符")
    if _ZONE_FORBIDDEN.search(text):
        raise ValidationError("温区名称含控制字符")
    return text


def normalize_reason(value):
    text = " ".join(str(value or "").split())
    if not text:
        raise ValidationError("更正原因不能为空")
    if len(text) > MAX_REASON_LENGTH:
        raise ValidationError(f"更正原因不能超过 {MAX_REASON_LENGTH} 字符")
    if _ZONE_FORBIDDEN.search(text):
        raise ValidationError("更正原因含控制字符")
    return text


def parse_intervals(raw_intervals, *, require_non_empty=False):
    """Validate a whole group of intervals for one SKU.

    ``raw_intervals`` is a list of dicts with ``zone``, ``valid_from`` and
    optional ``valid_to``. Returns canonical interval dicts sorted by start.
    Both endpoints are UTC microseconds; ``valid_to`` of ``None`` is open.
    """
    if not isinstance(raw_intervals, list):
        raise ValidationError("区间必须是列表")
    if len(raw_intervals) > MAX_INTERVALS:
        raise ValidationError(f"单个 SKU 最多 {MAX_INTERVALS} 段区间")
    if require_non_empty and not raw_intervals:
        raise ValidationError("至少登记一段有效区间")

    parsed = []
    for index, entry in enumerate(raw_intervals, start=1):
        if not isinstance(entry, dict):
            raise ValidationError(f"第 {index} 段区间格式不正确")
        zone = normalize_zone(entry.get("zone"))
        from_text = entry.get("valid_from")
        to_text = entry.get("valid_to")
        try:
            start = parse_utc(from_text, field=f"第 {index} 段的起始时间")
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
        end = None
        if to_text is not None and str(to_text).strip() != "":
            try:
                end = parse_utc(to_text, field=f"第 {index} 段的结束时间")
            except ValueError as exc:
                raise ValidationError(str(exc)) from exc
            if end <= start:
                raise ValidationError(
                    f"第 {index} 段的结束时间必须晚于起始时间（区间为起含止不含）"
                )
        parsed.append(
            {
                "zone": zone,
                "valid_from": format_utc(start),
                "valid_from_us": datetime_to_us(start),
                "valid_to": format_utc(end) if end else None,
                "valid_to_us": datetime_to_us(end) if end else None,
                "_index": index,
            }
        )

    parsed.sort(key=lambda item: (item["valid_from_us"], item["_index"]))
    for previous, current in zip(parsed, parsed[1:]):
        previous_end = previous["valid_to_us"]
        if previous_end is None or current["valid_from_us"] < previous_end:
            raise ValidationError(
                "同一 SKU 的区间不得重叠："
                f"第 {previous['_index']} 段与第 {current['_index']} 段"
            )
    return parsed


def current_version(conn):
    return conn.execute(
        "SELECT * FROM knowledge_versions ORDER BY version_no DESC LIMIT 1"
    ).fetchone()


def latest_version_no(conn):
    row = conn.execute("SELECT MAX(version_no) AS v FROM knowledge_versions").fetchone()
    return row["v"]


def load_ranges(conn, version_no):
    return conn.execute(
        "SELECT sku, zone, valid_from, valid_from_us, valid_to, valid_to_us"
        " FROM version_ranges WHERE version_no = ?"
        " ORDER BY sku, valid_from_us",
        (version_no,),
    ).fetchall()


def ranges_by_sku(conn, version_no):
    grouped = {}
    for row in load_ranges(conn, version_no):
        grouped.setdefault(row["sku"], []).append(row)
    return grouped


def list_versions(conn):
    return conn.execute(
        "SELECT v.version_no, v.based_on_version, v.reason, v.created_at,"
        "       (SELECT COUNT(*) FROM version_ranges r WHERE r.version_no = v.version_no) AS range_count,"
        "       (SELECT COUNT(DISTINCT r2.sku) FROM version_ranges r2 WHERE r2.version_no = v.version_no) AS sku_count"
        " FROM knowledge_versions v ORDER BY v.version_no DESC"
    ).fetchall()


def get_version(conn, version_no):
    return conn.execute(
        "SELECT * FROM knowledge_versions WHERE version_no = ?", (version_no,)
    ).fetchone()


def register_ranges(conn, sku, intervals, reason, base_version=None,
                    review=None, secret=None):
    """Create a new immutable version with a whole-group correction for ``sku``.

    ``base_version`` is the version the operator was looking at. It must be
    the current latest version; otherwise another correction has landed in
    between and the request is rejected without writing anything.

    ``review`` is the explicit override for exactly that situation: a dict
    ``{"token": ...}`` signed by :func:`prepare_review` / :func:`review_draft`.
    It lets a stale draft be applied only after the operator has seen the
    original base, the current version and the draft side by side. The signed
    receipt binds the exact draft and the exact ledger version that was on
    screen: editing the draft, or a further correction landing in the ledger,
    invalidates it and forces a fresh review. The draft's original
    ``base_version`` is still what the new version records.
    """
    sku = normalize_sku(sku)
    reason = normalize_reason(reason)
    parsed = parse_intervals(intervals)

    with transaction(conn):
        latest = latest_version_no(conn)
        review_row = None
        if review is not None:
            review_row = _verify_review(
                conn, review, sku, reason, base_version, parsed, latest, secret
            )
            request_base = review_row["draft_base_version"]
        else:
            request_base = _check_plain_base(base_version, latest)

        if not parsed and request_base is None:
            raise ValidationError("首个知识版本不能是空区间组；请至少登记一段区间")

        carried = []
        if latest is not None:
            carried = [
                {
                    "sku": row["sku"],
                    "zone": row["zone"],
                    "valid_from": row["valid_from"],
                    "valid_from_us": row["valid_from_us"],
                    "valid_to": row["valid_to"],
                    "valid_to_us": row["valid_to_us"],
                }
                for row in load_ranges(conn, latest)
                if row["sku"] != sku
            ]

        combined_for_sku = [
            {
                "sku": sku,
                "zone": item["zone"],
                "valid_from": item["valid_from"],
                "valid_from_us": item["valid_from_us"],
                "valid_to": item["valid_to"],
                "valid_to_us": item["valid_to_us"],
            }
            for item in parsed
        ]
        previous_for_sku = [
            row
            for row in (load_ranges(conn, latest) if latest is not None else [])
            if row["sku"] == sku
        ]
        if latest is not None and _same_group(previous_for_sku, combined_for_sku):
            raise ValidationError(
                f"更正内容与 v{latest} 中 SKU {sku} 的区间完全相同，未生成新版"
            )

        version_no = 1 if latest is None else latest + 1
        if version_no > MAX_VERSION_NUMBER:
            raise ConflictError("知识版号已用尽")
        created_at = now_str()
        try:
            conn.execute(
                "INSERT INTO knowledge_versions(version_no, based_on_version, reason, created_at)"
                " VALUES(?,?,?,?)",
                (version_no, request_base, reason, created_at),
            )
            if review_row is not None:
                conn.execute(
                    "INSERT INTO version_reviews(token_id, version_no, sku,"
                    " draft_base_version, reviewed_version, created_at)"
                    " VALUES(?,?,?,?,?,?)",
                    (
                        review_row["token_id"],
                        version_no,
                        sku,
                        review_row["draft_base_version"],
                        review_row["reviewed_version"],
                        created_at,
                    ),
                )
            conn.executemany(
                "INSERT INTO version_ranges(version_no, sku, zone, valid_from, valid_from_us,"
                " valid_to, valid_to_us) VALUES(?,?,?,?,?,?,?)",
                [
                    (
                        version_no,
                        item["sku"],
                        item["zone"],
                        item["valid_from"],
                        item["valid_from_us"],
                        item["valid_to"],
                        item["valid_to_us"],
                    )
                    for item in carried + combined_for_sku
                ],
            )
        except sqlite3.IntegrityError as exc:
            raise ConflictError(f"版本写入被拒绝：{exc}（未产生任何半版）") from exc
        if review_row is not None:
            summary = (
                f"v{version_no} 经核对确认整组替换 SKU {sku}：{len(parsed)} 段，"
                f"草稿原基准 v{review_row['draft_base_version']}，"
                f"核对时当前版 v{review_row['reviewed_version']}，原因：{reason}"
            )
        else:
            summary = f"v{version_no} 更正 SKU {sku}：{len(parsed)} 段，原因：{reason}"
        conn.execute(
            "INSERT INTO audit_log(at, action, month, version_no, summary) VALUES(?,?,?,?,?)",
            (created_at, "register_ranges", None, version_no, summary),
        )
    return {
        "version_no": version_no,
        "based_on_version": request_base,
        "sku": sku,
        "range_count": len(parsed),
        "created_at": created_at,
        "reviewed_version": review_row["reviewed_version"] if review_row else None,
    }


def _check_plain_base(base_version, latest):
    """Validate an ordinary (non-reviewed) submission against the latest version."""
    if base_version is None or str(base_version).strip() == "":
        if latest is not None:
            raise ConflictError(
                f"已有知识版号 v{latest}，请载入当前版后再更正（避免覆盖他人更正）"
            )
        return None
    try:
        requested = int(str(base_version).strip())
    except (TypeError, ValueError):
        raise ValidationError("基准知识版号必须是整数") from None
    if latest is None:
        raise ConflictError("尚无任何知识版本，基准版号应为空")
    if requested != latest:
        raise ConflictError(
            f"基准版号 v{requested} 不是当前最新版 v{latest}；"
            "请刷新后基于最新版重新更正，或打开核对页并排核对后确认整组替换"
        )
    return requested


def _verify_review(conn, review, sku, reason, base_version, parsed, latest, secret):
    """Check a signed review receipt against the submitted draft and ledger."""
    if not isinstance(review, dict):
        raise ValidationError("核对确认参数格式不正确")
    draft, token_reviewed, token_id = _decode_token(review.get("token"), secret)
    try:
        reviewed_version = int(review.get("reviewed_version"))
    except (TypeError, ValueError):
        raise ValidationError("核对确认缺少已核对版号，请重新打开核对页") from None
    if reviewed_version != token_reviewed:
        raise ValidationError("核对确认的回执与已核对版号不一致，请重新核对")

    draft_sku = normalize_sku(draft.get("sku"))
    draft_reason = normalize_reason(draft.get("reason"))
    draft_base = _int_or_none(draft.get("base_version"))
    draft_intervals = parse_intervals(draft.get("intervals") or [])

    if draft_sku != sku:
        raise ValidationError("提交的 SKU 与核对页展示的草稿不一致，请重新核对")
    if draft_reason != reason:
        raise ValidationError("提交的更正原因与核对页展示的草稿不一致，请重新核对")
    if draft_base != _int_or_none(base_version):
        raise ValidationError("提交的基准版号与核对页展示的草稿不一致，请重新核对")
    if _signature(draft_intervals) != _signature(parsed):
        raise ValidationError("提交的区间与核对页展示的草稿不一致，请重新核对")

    if latest is None or reviewed_version != latest:
        raise ConflictError(
            f"知识账本在核对后又生成了新版 v{latest}；请重新打开核对页，"
            "并排核对原基准、当前版和草稿后再确认"
        )
    if draft_base is not None and get_version(conn, draft_base) is None:
        raise ValidationError(f"草稿的原基准版号 v{draft_base} 不存在，请重新核对")
    if draft_base is None and latest is not None:
        raise ConflictError("该草稿以空账本为基准，但账本已有版号；请重新核对")

    existing = conn.execute(
        "SELECT version_no FROM version_reviews WHERE token_id = ?", (token_id,)
    ).fetchone()
    if existing is not None:
        raise ConflictError(
            f"该草稿已确认并生成 v{existing['version_no']}，重复确认不会再生成新版"
        )
    return {
        "token_id": token_id,
        "draft_base_version": draft_base,
        "reviewed_version": reviewed_version,
    }


def prepare_review(conn, sku, intervals, reason, base_version, secret=None):
    """Validate a draft and return the side-by-side review state for it."""
    sku = normalize_sku(sku)
    reason = normalize_reason(reason)
    parsed = parse_intervals(intervals)
    base = _int_or_none(base_version)
    return _review_state(conn, sku, reason, parsed, base, secret, None)


def review_draft(conn, token, secret=None):
    """Re-render the review page from a previously signed receipt.

    The draft is decoded from the receipt, so the page always shows exactly
    what will be submitted. The ledger state is read fresh: if a new version
    landed since the receipt was signed, the page is re-rendered against the
    new current version and a fresh receipt is issued, which means the
    operator has to confirm again.
    """
    draft, _reviewed, _token_id = _decode_token(token, secret)
    sku = normalize_sku(draft.get("sku"))
    reason = normalize_reason(draft.get("reason"))
    parsed = parse_intervals(draft.get("intervals") or [])
    base = _int_or_none(draft.get("base_version"))
    return _review_state(conn, sku, reason, parsed, base, secret, token)


def _review_state(conn, sku, reason, parsed, draft_base, secret, previous_token):
    with transaction(conn, immediate=False):
        latest = latest_version_no(conn)
        if draft_base is not None and get_version(conn, draft_base) is None:
            raise ValidationError(f"草稿的原基准版号 v{draft_base} 不存在，无法核对")
        if draft_base is None and latest is not None:
            raise ConflictError(
                "该草稿以空账本为基准，但账本已有版号；请重新登记后再核对"
            )
        base_rows = _group_for_sku(conn, draft_base, sku)
        current_rows = _group_for_sku(conn, latest, sku)
        token_id = _draft_id(sku, reason, draft_base, parsed)
        history = _review_history(conn, token_id)

    base_ranges = _display_group(base_rows)
    current_ranges = _display_group(current_rows)
    draft_ranges = _display_group(parsed)
    already_confirmed = history[0] if history else None
    identical = latest is not None and _display_equal(current_ranges, draft_ranges)
    last_reviewed = _decode_token(previous_token, secret)[1] if previous_token else None
    # A review is a point-in-time act: if the ledger moved on since the last
    # page render, or this draft was already applied, the operator must look
    # again and confirm explicitly.
    requires_new_review = bool(history) or (
        last_reviewed is not None and last_reviewed != latest
    )
    token = _encode_token(sku, reason, draft_base, parsed, latest, secret)
    return {
        "token": token,
        "token_id": token_id,
        "sku": sku,
        "reason": reason,
        "draft": _draft_payload(sku, reason, draft_base, parsed),
        "draft_base_version": draft_base,
        "reviewed_version": latest,
        "last_reviewed_version": last_reviewed,
        "stale_since_review": last_reviewed is not None and last_reviewed != latest,
        "base_ranges": base_ranges,
        "current_ranges": current_ranges,
        "draft_ranges": draft_ranges,
        "history": history,
        "already_confirmed": already_confirmed,
        "identical_to_current": identical,
        "requires_new_review": requires_new_review,
        "requires_confirmation": latest != draft_base,
    }


def _group_for_sku(conn, version_no, sku):
    if version_no is None:
        return []
    return [row for row in load_ranges(conn, version_no) if row["sku"] == sku]


def _display_group(items):
    """Display rows keep the canonical wire values used by the confirm form."""
    return [
        {
            "zone": item["zone"],
            "valid_from": item["valid_from"],
            "valid_to": item["valid_to"],
        }
        for item in items
    ]


def _display_equal(left, right):
    if len(left) != len(right):
        return False
    for a, b in zip(left, right):
        if (
            a["zone"] != b["zone"]
            or a["valid_from"] != b["valid_from"]
            or a["valid_to"] != b["valid_to"]
        ):
            return False
    return True


def _signature(parsed):
    return [
        (item["zone"], item["valid_from_us"], item["valid_to_us"])
        for item in parsed
    ]


def _int_or_none(value):
    if value is None or str(value).strip() == "":
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        raise ValidationError("知识版号必须是整数") from None


def _draft_payload(sku, reason, draft_base, parsed):
    return {
        "sku": sku,
        "reason": reason,
        "base_version": draft_base,
        "intervals": [
            {
                "zone": item["zone"],
                "valid_from": item["valid_from"],
                "valid_to": item["valid_to"],
            }
            for item in parsed
        ],
    }


def _draft_id(sku, reason, draft_base, parsed):
    canonical = json.dumps(
        _draft_payload(sku, reason, draft_base, parsed),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _token_key(secret):
    # The receipt must be unforgeable so a confirmation can only ever submit a
    # draft that a review page actually displayed.
    return (secret if secret is not None else "").encode("utf-8")


def _encode_token(sku, reason, draft_base, parsed, reviewed_version, secret):
    payload = json.dumps(
        {
            "v": REVIEW_TOKEN_VERSION,
            "reviewed_version": reviewed_version,
            "draft": _draft_payload(sku, reason, draft_base, parsed),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    body = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    mac = hmac.new(_token_key(secret), body.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{body}.{mac}"


def _decode_token(token, secret):
    if not isinstance(token, str) or not token.strip():
        raise ValidationError("缺少核对回执，请重新打开核对页")
    text = token.strip()
    if len(text) > MAX_TOKEN_LENGTH:
        raise ValidationError("核对回执过长，请重新打开核对页")
    body, _, mac = text.rpartition(".")
    expected = hmac.new(
        _token_key(secret), body.encode("ascii", "ignore"), hashlib.sha256
    ).hexdigest()
    if not body or not hmac.compare_digest(mac, expected):
        raise ValidationError("核对回执无效或已过期，请重新打开核对页")
    try:
        raw = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, TypeError, UnicodeDecodeError):
        raise ValidationError("核对回执格式不正确，请重新打开核对页") from None
    if not isinstance(payload, dict) or payload.get("v") != REVIEW_TOKEN_VERSION:
        raise ValidationError("核对回执格式不正确，请重新打开核对页")
    draft = payload.get("draft")
    if not isinstance(draft, dict):
        raise ValidationError("核对回执缺少草稿内容，请重新打开核对页")
    try:
        reviewed_version = int(payload.get("reviewed_version"))
    except (TypeError, ValueError):
        raise ValidationError("核对回执缺少已核对版号，请重新打开核对页") from None
    token_id = _draft_id(
        normalize_sku(draft.get("sku")),
        normalize_reason(draft.get("reason")),
        _int_or_none(draft.get("base_version")),
        parse_intervals(draft.get("intervals") or []),
    )
    return draft, reviewed_version, token_id


def review_for_version(conn, version_no):
    """Return the review record that created ``version_no``, if any."""
    row = conn.execute(
        "SELECT token_id, version_no, sku, draft_base_version, reviewed_version, created_at"
        " FROM version_reviews WHERE version_no = ?",
        (version_no,),
    ).fetchone()
    return dict(row) if row else None


def _review_history(conn, token_id):
    rows = conn.execute(
        "SELECT r.version_no, r.draft_base_version, r.reviewed_version, r.created_at,"
        "       v.reason"
        " FROM version_reviews r JOIN knowledge_versions v ON v.version_no = r.version_no"
        " WHERE r.token_id = ? ORDER BY r.version_no DESC",
        (token_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def _same_group(previous_rows, new_items):
    if len(previous_rows) != len(new_items):
        return False
    for row, item in zip(previous_rows, new_items):
        if (
            row["sku"] != item["sku"]
            or row["zone"] != item["zone"]
            or row["valid_from_us"] != item["valid_from_us"]
            or row["valid_to_us"] != item["valid_to_us"]
        ):
            return False
    return True


def resolve_version(conn, requested):
    """Return version row for a query; ``None`` means no versions exist yet."""
    latest = latest_version_no(conn)
    if requested is None or str(requested).strip() == "":
        if latest is None:
            return None
        return get_version(conn, latest)
    try:
        number = int(str(requested).strip())
    except ValueError:
        raise ValidationError("知识版号必须是整数") from None
    row = get_version(conn, number)
    if row is None:
        raise NotFoundError(f"知识版号 v{number} 不存在")
    return row


def classify(ts_us, intervals, starts=None):
    """Return the interval covering ``ts_us`` or ``None``.

    ``intervals`` are sorted by start and guaranteed non-overlapping. The
    comparison is half-open: start <= ts < end.
    """
    if not intervals:
        return None
    if starts is None:
        starts = [item["valid_from_us"] for item in intervals]
    position = bisect.bisect_right(starts, ts_us) - 1
    if position < 0:
        return None
    item = intervals[position]
    if item["valid_to_us"] is None or ts_us < item["valid_to_us"]:
        return item
    return None


def _row_to_interval(row):
    return {
        "valid_from": row["valid_from"],
        "valid_from_us": row["valid_from_us"],
        "valid_to": row["valid_to"],
        "valid_to_us": row["valid_to_us"],
        "zone": row["zone"],
    }


def build_report(conn, month, requested_version=None):
    """Compute a report for exactly one version and one month.

    Facts and the selected version's ranges are read in a single read
    transaction so a concurrent correction can never leak into this report.
    """
    from .timeutil import validate_month

    try:
        month = validate_month(month)
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc
    with transaction(conn, immediate=False):
        month_row = conn.execute("SELECT * FROM months WHERE month = ?", (month,)).fetchone()
        if month_row is None:
            raise NotFoundError(f"月份 {month} 尚未上传出库事实")
        version_row = resolve_version(conn, requested_version)
        version_no = version_row["version_no"] if version_row else None

        grouped = {}
        if version_no is not None:
            for row in load_ranges(conn, version_no):
                grouped.setdefault(row["sku"], []).append(_row_to_interval(row))
        indexed = {
            sku: (items, [item["valid_from_us"] for item in items])
            for sku, items in grouped.items()
        }
        sku_interval_json = {
            sku: [
                {
                    "zone": item["zone"],
                    "valid_from": item["valid_from"],
                    "valid_to": item["valid_to"],
                }
                for item in items
            ]
            for sku, items in grouped.items()
        }

        shipments = conn.execute(
            "SELECT id, sequence_no, sku, occurred_at, occurred_at_us, boxes"
            " FROM shipments WHERE month = ? ORDER BY occurred_at_us, id",
            (month,),
        ).fetchall()

    buckets = {}
    unclassified = []
    total_boxes = 0
    for shipment in shipments:
        total_boxes += shipment["boxes"]
        intervals, starts = indexed.get(shipment["sku"], ((), ()))
        interval = classify(shipment["occurred_at_us"], intervals, starts)
        record = {
            "sequence_no": shipment["sequence_no"],
            "sku": shipment["sku"],
            "occurred_at": shipment["occurred_at"],
            "boxes": shipment["boxes"],
            "basis": (
                {
                    "zone": interval["zone"],
                    "valid_from": interval["valid_from"],
                    "valid_to": interval["valid_to"],
                }
                if interval
                else None
            ),
        }
        if interval is None:
            record["sku_intervals"] = sku_interval_json.get(shipment["sku"], [])
            unclassified.append(record)
            continue
        bucket = buckets.setdefault(
            interval["zone"], {"zone": interval["zone"], "boxes": 0, "shipments": []}
        )
        bucket["boxes"] += shipment["boxes"]
        bucket["shipments"].append(record)

    zones = []
    for zone in sorted(buckets):
        bucket = buckets[zone]
        bucket["shipment_count"] = len(bucket["shipments"])
        zones.append(bucket)
    classified_boxes = sum(bucket["boxes"] for bucket in zones)
    unclassified_boxes = sum(item["boxes"] for item in unclassified)

    return {
        "month": month,
        "month_row": {
            "month": month_row["month"],
            "row_count": month_row["row_count"],
            "total_boxes": month_row["total_boxes"],
            "frozen_at": month_row["frozen_at"],
            "source_filename": month_row["source_filename"],
        },
        "version": (
            {
                "version_no": version_row["version_no"],
                "based_on_version": version_row["based_on_version"],
                "created_at": version_row["created_at"],
                "reason": version_row["reason"],
            }
            if version_row
            else None
        ),
        "zones": zones,
        "classified_boxes": classified_boxes,
        "unclassified": {
            "boxes": unclassified_boxes,
            "shipment_count": len(unclassified),
            "shipments": unclassified,
        },
        "total_boxes": total_boxes,
        "total_shipments": len(shipments),
    }


def _shipment_json(item):
    from .timeutil import display_utc

    def interval_json(interval):
        return {
            "zone": interval["zone"],
            "valid_from": interval["valid_from"],
            "valid_from_display": display_utc(interval["valid_from"]),
            "valid_to": interval["valid_to"],
            "valid_to_display": display_utc(interval["valid_to"]),
        }

    payload = {
        "sequence_no": item["sequence_no"],
        "sku": item["sku"],
        "occurred_at": item["occurred_at"],
        "occurred_at_display": display_utc(item["occurred_at"]),
        "boxes": item["boxes"],
        "basis_interval": (
            {
                "zone": item["basis"]["zone"],
                "valid_from": item["basis"]["valid_from"],
                "valid_from_display": display_utc(item["basis"]["valid_from"]),
                "valid_to": item["basis"]["valid_to"],
                "valid_to_display": display_utc(item["basis"]["valid_to"]),
            }
            if item.get("basis")
            else None
        ),
    }
    if "sku_intervals" in item:
        payload["sku_intervals"] = [interval_json(x) for x in item["sku_intervals"]]
    return payload


def report_json(report):
    """Convert report structures into JSON-ready payloads."""
    payload = {
        "month": report["month"],
        "version": report["version"],
        "month_fact": report["month_row"],
        "zones": [
            {
                "zone": bucket["zone"],
                "boxes": bucket["boxes"],
                "shipment_count": bucket["shipment_count"],
                "shipments": [_shipment_json(item) for item in bucket["shipments"]],
            }
            for bucket in report["zones"]
        ],
        "classified_boxes": report["classified_boxes"],
        "unclassified": {
            "boxes": report["unclassified"]["boxes"],
            "shipment_count": report["unclassified"]["shipment_count"],
            "shipments": [
                _shipment_json(item) for item in report["unclassified"]["shipments"]
            ],
        },
        "total_boxes": report["total_boxes"],
        "total_shipments": report["total_shipments"],
    }
    return payload


def version_ranges_for_display(conn, version_no):
    from .timeutil import display_utc

    grouped = []
    current_sku = None
    current = None
    for row in load_ranges(conn, version_no):
        if row["sku"] != current_sku:
            current_sku = row["sku"]
            current = {"sku": row["sku"], "intervals": []}
            grouped.append(current)
        current["intervals"].append(
            {
                "zone": row["zone"],
                "valid_from": display_utc(row["valid_from"]),
                "valid_to": display_utc(row["valid_to"]) if row["valid_to"] else None,
                "valid_from_raw": row["valid_from"],
                "valid_to_raw": row["valid_to"],
            }
        )
    return grouped
