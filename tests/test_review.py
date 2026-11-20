"""Draft review / explicit whole-group replacement tests.

Scenario: two operators correct the same SKU. The first form fails because
its own intervals overlap. Meanwhile the second operator's correction lands.
The first operator fixes only the overlap and re-submits — that must still
be checked against the *original* base, not silently applied on top of the
second operator's version. If they really want their draft, they must open
the review page, see the original base, the current version and the draft
side by side, and explicitly confirm the whole-group replacement.
"""

from __future__ import annotations

import io
import unittest
from html import unescape
from pathlib import Path
from tempfile import TemporaryDirectory

from werkzeug.datastructures import MultiDict

from coldchain.app import create_app
from coldchain.db import connect


def csv_bytes(rows, header="流水号,SKU,出库时刻,箱数"):
    lines = [header]
    lines.extend(",".join(str(cell) for cell in row) for row in rows)
    return ("\n".join(lines) + "\n").encode("utf-8")


ZONE_A_V1 = [{"zone": "冷藏", "valid_from": "2026-03-01T00:00:00Z", "valid_to": ""}]
ZONE_B_V2 = [{"zone": "冷冻", "valid_from": "2026-03-01T00:00:00Z", "valid_to": ""}]
ZONE_A_DRAFT = [{"zone": "冷藏", "valid_from": "2026-03-01T00:00:00Z", "valid_to": "2026-03-10T00:00:00Z"},
                {"zone": "冷冻", "valid_from": "2026-03-10T00:00:00Z", "valid_to": ""}]


class ReviewFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "review.sqlite3")
        self.app = create_app({"TESTING": True, "DB_PATH": self.db_path,
                               "SECRET_KEY": "test-review-secret"})
        self.client = self.app.test_client()
        self.client.post(
            "/api/months",
            data={"file": (io.BytesIO(csv_bytes([
                ("OUT-1", "SKU-A", "2026-03-01T00:00:00Z", 12),
                ("OUT-2", "SKU-A", "2026-03-20T00:00:00Z", 5),
                ("OUT-3", "SKU-B", "2026-03-02T00:00:00Z", 7),
            ])), "m.csv")},
            content_type="multipart/form-data",
        )
        # Operator A creates v1 for SKU-A.
        self.assertEqual(self.client.post("/api/versions", json={
            "sku": "SKU-A", "reason": "初始", "base_version": None, "intervals": ZONE_A_V1,
        }).status_code, 201)
        # Operator B creates v2 (SKU-B) on top of v1.
        self.assertEqual(self.client.post("/api/versions", json={
            "sku": "SKU-B", "reason": "乙的更正", "base_version": 1, "intervals": ZONE_B_V2,
        }).status_code, 201)

    def tearDown(self):
        self.tmp.cleanup()

    # ---------- helpers ----------

    def submit_form(self, form):
        return self.client.post("/knowledge", data=MultiDict(form))

    def base_form(self, base="1", intervals=None, reason="甲的更正"):
        pairs = [("sku", "SKU-A"), ("reason", reason), ("base_version", base)]
        for interval in intervals or ZONE_A_DRAFT:
            pairs.append(("zone", interval["zone"]))
            pairs.append(("valid_from", interval["valid_from"]))
            pairs.append(("valid_to", interval["valid_to"]))
        return MultiDict(pairs)

    def review_from_html(self, html):
        marker = 'name="review_token" value="'
        start = html.index(marker) + len(marker)
        return unescape(html[start:html.index('"', start)])

    def confirm(self, html, overrides=None):
        token = self.review_from_html(html)
        state = self.review_state
        pairs = [
            ("review_token", token),
            ("reviewed_version", str(state["reviewed_version"])),
            ("sku", state["sku"]),
            ("reason", state["reason"]),
            ("base_version",
             "" if state["draft_base_version"] is None else str(state["draft_base_version"])),
        ]
        for interval in (overrides if overrides is not None else state["draft_ranges"]):
            pairs.append(("zone", interval["zone"]))
            pairs.append(("valid_from", interval["valid_from"]))
            pairs.append(("valid_to", interval["valid_to"] or ""))
        return self.client.post("/knowledge/review/confirm", data=MultiDict(pairs))

    def open_review(self, form):
        response = self.client.post("/knowledge/review", data=form)
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        from coldchain import knowledge

        conn = connect(self.db_path)
        try:
            state = knowledge.review_draft(
                conn, self.review_from_html(response.get_data(as_text=True)),
                secret="test-review-secret",
            )
        finally:
            conn.close()
        self.review_state = state
        return response

    def versions(self):
        return [v["version_no"] for v in
                self.client.get("/api/versions").get_json()["versions"]]

    # ---------- tests ----------

    def test_conflict_keeps_draft_and_original_base(self):
        response = self.submit_form(self.base_form(base="1"))
        self.assertEqual(response.status_code, 409)
        page = response.get_data(as_text=True)
        # Draft values survive.
        self.assertIn('value="甲的更正"', page)
        self.assertIn('value="2026-03-10T00:00:00Z"', page)
        # The form still carries the original base, not the latest version.
        self.assertIn('name="base_version" value="1"', page)
        self.assertIn("打开核对页", page)
        # Nothing was written.
        self.assertEqual(self.versions(), [2, 1])

    def test_fixing_only_the_overlap_still_needs_review(self):
        # First attempt: overlapping intervals -> validation error.
        overlapping = [
            {"zone": "冷藏", "valid_from": "2026-03-01T00:00:00Z", "valid_to": "2026-03-15T00:00:00Z"},
            {"zone": "冷冻", "valid_from": "2026-03-10T00:00:00Z", "valid_to": ""},
        ]
        response = self.submit_form(self.base_form(base="1", intervals=overlapping))
        self.assertEqual(response.status_code, 400)
        self.assertIn("重叠", response.get_data(as_text=True))

        # Fix only the overlap, resubmit against the same original base v1.
        response = self.submit_form(self.base_form(base="1"))
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.versions(), [2, 1])
        # v2 (operator B) is untouched, and the ledger has no v3.
        detail = self.client.get("/api/versions/2").get_json()
        skus = {group["sku"]: group["intervals"] for group in detail["skus"]}
        self.assertEqual(skus["SKU-B"][0]["zone"], "冷冻")

    def test_review_page_shows_all_three_groups(self):
        self.submit_form(self.base_form(base="1"))
        page = self.open_review(self.base_form(base="1"))
        html = page.get_data(as_text=True)
        self.assertIn("原基准 v1", html)
        self.assertIn("当前版 v2", html)
        self.assertIn("草稿（待确认）", html)
        state = self.review_state
        self.assertEqual(state["draft_base_version"], 1)
        self.assertEqual(state["reviewed_version"], 2)
        # Base shows the original single 冷藏 interval.
        self.assertEqual([r["zone"] for r in state["base_ranges"]], ["冷藏"])
        # Current still carries SKU-A's original interval; SKU-B's own
        # correction lives in the same version but under another SKU.
        self.assertEqual([r["zone"] for r in state["current_ranges"]], ["冷藏"])
        self.assertEqual([r["zone"] for r in state["draft_ranges"]], ["冷藏", "冷冻"])

    def test_confirmation_applies_draft_on_top_of_current_ledger(self):
        self.submit_form(self.base_form(base="1"))
        page = self.open_review(self.base_form(base="1"))
        response = self.confirm(page.get_data(as_text=True))
        self.assertEqual(response.status_code, 302, response.get_data(as_text=True))
        self.assertEqual(self.versions(), [3, 2, 1])

        version = self.client.get("/api/versions/3").get_json()
        self.assertEqual(version["version"]["based_on_version"], 1)
        self.assertEqual(version["review"]["draft_base_version"], 1)
        self.assertEqual(version["review"]["reviewed_version"], 2)
        skus = {group["sku"]: group["intervals"] for group in version["skus"]}
        # Whole group replaced for SKU-A; SKU-B's correction survives.
        self.assertEqual(len(skus["SKU-A"]), 2)
        self.assertEqual(skus["SKU-B"][0]["zone"], "冷冻")

        old = self.client.get("/api/report?month=2026-03&version=2").get_json()
        self.assertEqual({z["zone"]: z["boxes"] for z in old["zones"]},
                         {"冷冻": 7, "冷藏": 17})

    def test_edited_draft_must_be_reviewed_again(self):
        self.submit_form(self.base_form(base="1"))
        page = self.open_review(self.base_form(base="1"))
        html = page.get_data(as_text=True)
        edited = [{"zone": "冷藏", "valid_from": "2026-03-01T00:00:00Z", "valid_to": ""}]
        response = self.confirm(html, overrides=edited)
        self.assertEqual(response.status_code, 400)
        self.assertIn("与核对页展示的草稿不一致", response.get_data(as_text=True))
        self.assertEqual(self.versions(), [2, 1])

    def test_new_version_after_review_forces_review_again(self):
        self.submit_form(self.base_form(base="1"))
        page = self.open_review(self.base_form(base="1"))
        html = page.get_data(as_text=True)
        # Operator C lands another correction after the page was rendered.
        self.assertEqual(self.client.post("/api/versions", json={
            "sku": "SKU-B", "reason": "丙的更正", "base_version": 2,
            "intervals": [{"zone": "恒温", "valid_from": "2026-03-01T00:00:00Z", "valid_to": ""}],
        }).status_code, 201)
        response = self.confirm(html)
        self.assertEqual(response.status_code, 409)
        self.assertIn("核对后又生成了新版", response.get_data(as_text=True))
        self.assertEqual(self.versions(), [3, 2, 1])

        # Re-opening the review page picks up the new ledger state.
        state = self.review_state
        self.assertEqual(state["reviewed_version"], 2)
        response = self.client.get(f"/knowledge/review?token={state['token']}")
        self.assertEqual(response.status_code, 200)
        self.assertIn("当前版 v3", response.get_data(as_text=True))

    def test_duplicate_confirmation_creates_at_most_one_version(self):
        self.submit_form(self.base_form(base="1"))
        page = self.open_review(self.base_form(base="1"))
        html = page.get_data(as_text=True)
        first = self.confirm(html)
        self.assertEqual(first.status_code, 302)
        second = self.confirm(html)
        self.assertEqual(second.status_code, 409)
        self.assertIn("重复确认不会再生成新版", second.get_data(as_text=True))
        self.assertEqual(self.versions(), [3, 2, 1])

    def test_failed_confirmation_leaves_no_version_or_review(self):
        self.submit_form(self.base_form(base="1"))
        page = self.open_review(self.base_form(base="1"))
        html = page.get_data(as_text=True)
        conn = connect(self.db_path)
        try:
            conn.execute(
                "CREATE TRIGGER boom BEFORE INSERT ON version_ranges"
                " WHEN NEW.zone = 'BOOM' BEGIN SELECT RAISE(ABORT, 'injected'); END"
            )
        finally:
            conn.close()
        failing = [{"zone": "BOOM", "valid_from": "2026-03-01T00:00:00Z", "valid_to": ""}]
        # The draft itself must be produced by a review page, so sign one with
        # the same receipt shape but a failing zone through the API path.
        from coldchain import knowledge

        conn = connect(self.db_path)
        try:
            state = knowledge.prepare_review(
                conn,
                sku="SKU-A",
                intervals=failing,
                reason="甲的更正",
                base_version="1",
                secret="test-review-secret",
            )
            with self.assertRaises(Exception):
                knowledge.register_ranges(
                    conn, sku="SKU-A", intervals=failing, reason="甲的更正",
                    base_version="1",
                    review={"token": state["token"],
                            "reviewed_version": str(state["reviewed_version"])},
                    secret="test-review-secret",
                )
        finally:
            conn.close()
        conn = connect(self.db_path)
        try:
            versions = conn.execute("SELECT version_no FROM knowledge_versions").fetchall()
            reviews = conn.execute("SELECT COUNT(*) AS c FROM version_reviews").fetchone()
        finally:
            conn.close()
        self.assertEqual([row["version_no"] for row in versions], [1, 2])
        self.assertEqual(reviews["c"], 0)

    def test_old_month_reports_still_query_after_review(self):
        self.submit_form(self.base_form(base="1"))
        page = self.open_review(self.base_form(base="1"))
        self.assertEqual(self.confirm(page.get_data(as_text=True)).status_code, 302)
        for version in (1, 2, 3):
            response = self.client.get(f"/api/report?month=2026-03&version={version}")
            self.assertEqual(response.status_code, 200)
        report = self.client.get("/api/report?month=2026-03&version=3").get_json()
        self.assertEqual({z["zone"]: z["boxes"] for z in report["zones"]},
                         {"冷藏": 12, "冷冻": 12})

    def test_tampered_receipt_rejected(self):
        self.submit_form(self.base_form(base="1"))
        page = self.open_review(self.base_form(base="1"))
        html = page.get_data(as_text=True)
        token = self.review_from_html(html)
        body, _, mac = token.rpartition(".")
        tampered = body[:-1] + ("A" if body[-1] != "A" else "B") + "." + mac
        response = self.client.post("/knowledge/review/confirm", data=MultiDict([
            ("review_token", tampered),
            ("reviewed_version", "2"),
            ("sku", "SKU-A"),
            ("reason", "甲的更正"),
            ("base_version", "1"),
            ("zone", "冷藏"), ("valid_from", "2026-03-01T00:00:00Z"), ("valid_to", ""),
        ]))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.versions(), [2, 1])

    def test_review_page_renders_version_detail(self):
        self.submit_form(self.base_form(base="1"))
        page = self.open_review(self.base_form(base="1"))
        self.assertEqual(self.confirm(page.get_data(as_text=True)).status_code, 302)
        response = self.client.get("/knowledge?version=3")
        page = response.get_data(as_text=True)
        self.assertIn("该版由核对确认生成", page)
        self.assertIn("原基准", page)
        self.assertIn("v2", page)


if __name__ == "__main__":
    unittest.main()
