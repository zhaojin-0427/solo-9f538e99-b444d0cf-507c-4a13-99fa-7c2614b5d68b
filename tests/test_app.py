"""End-to-end and domain tests for the cold-chain ledger."""

from __future__ import annotations

import io
import sqlite3
import threading
import unittest
import unittest.mock
from pathlib import Path
from tempfile import TemporaryDirectory

from coldchain import facts, knowledge
from coldchain.app import create_app
from coldchain.db import connect, init_db
from coldchain.errors import ConflictError, ValidationError


def csv_bytes(rows, header="流水号,SKU,出库时刻,箱数"):
    lines = [header]
    lines.extend(",".join(str(cell) for cell in row) for row in rows)
    return ("\n".join(lines) + "\n").encode("utf-8")


def sample_rows():
    return [
        ("OUT-001", "SKU-A", "2026-03-01T01:00:00Z", 12),
        ("OUT-002", "SKU-B", "2026-03-05T23:30:00Z", 7),
        ("OUT-003", "SKU-A", "2026-03-10T00:00:00Z", 5),
        ("OUT-004", "SKU-C", "2026-03-31T23:59:59Z", 2),
    ]


class AppTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "test.sqlite3")
        self.app = create_app({"TESTING": True, "DB_PATH": self.db_path})
        self.client = self.app.test_client()

    def tearDown(self):
        self.tmp.cleanup()

    def upload(self, rows, filename="m.csv", month=None):
        data = {"file": (io.BytesIO(csv_bytes(rows)), filename)}
        if month:
            data["month"] = month
        return self.client.post(
            "/api/months", data=data, content_type="multipart/form-data"
        )

    def register(self, sku, intervals, reason="correction", base=None):
        return self.client.post(
            "/api/versions",
            json={"sku": sku, "intervals": intervals, "reason": reason, "base_version": base},
        )

    def report(self, month="2026-03", version=None):
        query = f"/api/report?month={month}"
        if version is not None:
            query += f"&version={version}"
        response = self.client.get(query)
        return response.status_code, response.get_json()

    def raw_report(self, month="2026-03", version=None):
        query = f"/api/report?month={month}"
        if version is not None:
            query += f"&version={version}"
        return self.client.get(query).get_json()


class MonthFactTests(AppTestCase):
    def test_upload_freezes_month(self):
        response = self.upload(sample_rows())
        self.assertEqual(response.status_code, 201)
        body = response.get_json()
        self.assertEqual(body["month"], "2026-03")
        self.assertEqual(body["row_count"], 4)
        self.assertEqual(body["total_boxes"], 26)

    def test_second_upload_of_same_month_rejected_without_change(self):
        self.assertEqual(self.upload(sample_rows()).status_code, 201)
        response = self.upload([("OUT-100", "SKU-Z", "2026-03-02T00:00:00Z", 1)])
        self.assertEqual(response.status_code, 409)
        _, report = self.report()
        self.assertEqual(report["total_boxes"], 26)
        self.assertEqual(report["total_shipments"], 4)

    def test_multi_month_file_rejected(self):
        rows = sample_rows() + [("OUT-009", "SKU-A", "2026-04-01T00:00:00Z", 1)]
        response = self.upload(rows)
        self.assertEqual(response.status_code, 400)
        self.assertIn("单月", response.get_json()["message"])

    def test_invalid_rows_leave_no_facts(self):
        rows = sample_rows() + [("", "SKU-A", "2026-03-11T00:00:00Z", 4)]
        response = self.upload(rows)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.client.get("/api/months").get_json()["months"], [])
        # The same month must still be uploadable cleanly afterwards.
        self.assertEqual(self.upload(sample_rows()).status_code, 201)

    def test_duplicate_sequence_inside_file_rejected(self):
        rows = sample_rows() + [("OUT-001", "SKU-Q", "2026-03-12T00:00:00Z", 3)]
        response = self.upload(rows)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.client.get("/api/months").get_json()["months"], [])

    def test_duplicate_sequence_across_months_rejected(self):
        self.assertEqual(self.upload(sample_rows()).status_code, 201)
        response = self.upload([("OUT-001", "SKU-Z", "2026-04-01T00:00:00Z", 1)])
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.upload([], filename="x.csv").status_code, 400)
        self.assertEqual(len(self.client.get("/api/months").get_json()["months"]), 1)

    def test_month_param_must_match_file(self):
        response = self.upload(sample_rows(), month="2026-04")
        self.assertEqual(response.status_code, 400)

    def test_zero_and_negative_boxes_rejected(self):
        for boxes in ("0", "-3", "1.5", "abc"):
            response = self.upload([("OUT-9", "SKU-A", "2026-03-02T00:00:00Z", boxes)])
            self.assertEqual(response.status_code, 400, boxes)

    def test_facts_are_immutable_in_database(self):
        self.upload(sample_rows())
        conn = connect(self.db_path)
        try:
            with self.assertRaises(sqlite3.DatabaseError):
                conn.execute("UPDATE shipments SET boxes = 1")
            with self.assertRaises(sqlite3.DatabaseError):
                conn.execute("DELETE FROM months")
        finally:
            conn.close()


class KnowledgeVersionTests(AppTestCase):
    def setUp(self):
        super().setUp()
        self.assertEqual(self.upload(sample_rows()).status_code, 201)

    def test_first_version_and_back_dated_correction_keep_old_report(self):
        response = self.register(
            "SKU-A",
            [{"zone": "冷藏", "valid_from": "2026-03-01T00:00:00Z", "valid_to": ""}],
            reason="initial",
            base=None,
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.get_json()["version_no"], 1)

        status, before = self.report()
        self.assertEqual(status, 200)
        self.assertEqual(before["zones"][0]["zone"], "冷藏")
        self.assertEqual(before["zones"][0]["boxes"], 17)
        self.assertEqual(before["unclassified"]["boxes"], 9)

        response = self.register(
            "SKU-A",
            [
                {"zone": "冷冻", "valid_from": "2026-03-01T00:00:00Z", "valid_to": "2026-03-08T00:00:00Z"},
                {"zone": "冷藏", "valid_from": "2026-03-08T00:00:00Z", "valid_to": ""},
            ],
            reason="back-dated reclassification",
            base=1,
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.get_json()["version_no"], 2)

        _, old = self.report(version=1)
        _, new = self.report(version=2)
        self.assertEqual(old["zones"], before["zones"])
        self.assertEqual({z["zone"]: z["boxes"] for z in old["zones"]}, {"冷藏": 17})
        self.assertEqual(
            {z["zone"]: z["boxes"] for z in new["zones"]}, {"冷藏": 5, "冷冻": 12}
        )
        _, latest = self.report()
        self.assertEqual(latest["version"]["version_no"], 2)
        self.assertEqual(latest["zones"], new["zones"])

    def test_half_open_boundaries(self):
        self.register(
            "SKU-A",
            [
                {"zone": "冷冻", "valid_from": "2026-03-01T01:00:00Z", "valid_to": "2026-03-10T00:00:00Z"},
                {"zone": "冷藏", "valid_from": "2026-03-10T00:00:00Z", "valid_to": ""},
            ],
            reason="boundary",
        )
        _, report = self.report()
        by_zone = {z["zone"]: z for z in report["zones"]}
        self.assertEqual(by_zone["冷冻"]["boxes"], 12)  # OUT-001 at exact start
        self.assertEqual(by_zone["冷藏"]["boxes"], 5)  # OUT-003 at exact end -> next
        self.assertEqual(
            by_zone["冷冻"]["shipments"][0]["sequence_no"], "OUT-001"
        )

    def test_gap_and_unknown_sku_are_unclassified(self):
        self.register(
            "SKU-A",
            [{"zone": "冷藏", "valid_from": "2026-03-02T00:00:00Z", "valid_to": "2026-03-05T00:00:00Z"}],
            reason="narrow",
        )
        _, report = self.report()
        self.assertEqual(report["unclassified"]["boxes"], 26)
        self.assertEqual(report["zones"], [])

    def test_overlapping_ranges_rejected(self):
        response = self.register(
            "SKU-A",
            [
                {"zone": "冷藏", "valid_from": "2026-03-01T00:00:00Z", "valid_to": "2026-03-10T00:00:00Z"},
                {"zone": "冷冻", "valid_from": "2026-03-09T00:00:00Z", "valid_to": "2026-03-20T00:00:00Z"},
            ],
            reason="overlap",
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.client.get("/api/versions").get_json()["versions"], [])

    def test_adjacent_ranges_allowed(self):
        response = self.register(
            "SKU-A",
            [
                {"zone": "冷冻", "valid_from": "2026-03-01T00:00:00Z", "valid_to": "2026-03-05T00:00:00Z"},
                {"zone": "冷藏", "valid_from": "2026-03-05T00:00:00Z", "valid_to": "2026-03-20T00:00:00Z"},
            ],
            reason="adjacent",
        )
        self.assertEqual(response.status_code, 201)

    def test_stale_base_version_rejected(self):
        self.register("SKU-A", [{"zone": "冷藏", "valid_from": "2026-03-01T00:00:00Z"}], reason="v1")
        self.register("SKU-B", [{"zone": "冷冻", "valid_from": "2026-03-01T00:00:00Z"}], reason="v2", base=1)
        response = self.register(
            "SKU-A",
            [{"zone": "冷藏", "valid_from": "2026-03-02T00:00:00Z"}],
            reason="stale",
            base=1,
        )
        self.assertEqual(response.status_code, 409)
        versions = self.client.get("/api/versions").get_json()["versions"]
        self.assertEqual([v["version_no"] for v in versions], [2, 1])

    def test_missing_base_on_non_empty_ledger_rejected(self):
        self.register("SKU-A", [{"zone": "冷藏", "valid_from": "2026-03-01T00:00:00Z"}], reason="v1")
        response = self.register(
            "SKU-B", [{"zone": "冷冻", "valid_from": "2026-03-01T00:00:00Z"}], reason="no base"
        )
        self.assertEqual(response.status_code, 409)

    def test_whole_group_correction_keeps_other_skus(self):
        self.register("SKU-A", [{"zone": "冷藏", "valid_from": "2026-03-01T00:00:00Z"}], reason="v1")
        self.register("SKU-B", [{"zone": "冷冻", "valid_from": "2026-03-01T00:00:00Z"}], reason="v2", base=1)
        self.register(
            "SKU-A",
            [
                {"zone": "冷藏", "valid_from": "2026-03-01T00:00:00Z", "valid_to": "2026-03-05T00:00:00Z"},
                {"zone": "冷冻", "valid_from": "2026-03-05T00:00:00Z"},
            ],
            reason="v3",
            base=2,
        )
        detail = self.client.get("/api/versions/3").get_json()
        skus = {group["sku"]: group["intervals"] for group in detail["skus"]}
        self.assertEqual(set(skus), {"SKU-A", "SKU-B"})
        self.assertEqual(len(skus["SKU-A"]), 2)
        self.assertEqual(skus["SKU-B"][0]["zone"], "冷冻")
        # Old version still shows the original single interval.
        old = self.client.get("/api/versions/1").get_json()
        self.assertEqual(len(old["skus"][0]["intervals"]), 1)

    def test_noop_correction_rejected(self):
        self.register("SKU-A", [{"zone": "冷藏", "valid_from": "2026-03-01T00:00:00Z"}], reason="v1")
        response = self.register(
            "SKU-A",
            [{"zone": "冷藏", "valid_from": "2026-03-01T00:00:00Z"}],
            reason="same",
            base=1,
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(len(self.client.get("/api/versions").get_json()["versions"]), 1)

    def test_missing_reason_rejected(self):
        response = self.register("SKU-A", [{"zone": "冷藏", "valid_from": "2026-03-01T00:00:00Z"}], reason="  ")
        self.assertEqual(response.status_code, 400)

    def test_end_before_start_rejected(self):
        response = self.register(
            "SKU-A",
            [{"zone": "冷藏", "valid_from": "2026-03-05T00:00:00Z", "valid_to": "2026-03-01T00:00:00Z"}],
            reason="bad",
        )
        self.assertEqual(response.status_code, 400)

    def test_unknown_version_404(self):
        self.register("SKU-A", [{"zone": "冷藏", "valid_from": "2026-03-01T00:00:00Z"}], reason="v1")
        status, body = self.report(version=99)
        self.assertEqual(status, 404)
        self.assertEqual(self.client.get("/api/versions/99").status_code, 404)

    def test_unknown_month_404(self):
        status, _ = self.report(month="2026-02")
        self.assertEqual(status, 404)


class DurabilityTests(AppTestCase):
    def test_restart_keeps_old_versions(self):
        self.upload(sample_rows())
        self.register("SKU-A", [{"zone": "冷藏", "valid_from": "2026-03-01T00:00:00Z"}], reason="v1")
        self.register(
            "SKU-A",
            [{"zone": "冷冻", "valid_from": "2026-03-01T00:00:00Z", "valid_to": "2026-03-09T00:00:00Z"},
             {"zone": "冷藏", "valid_from": "2026-03-09T00:00:00Z"}],
            reason="v2",
            base=1,
        )
        _, before = self.report(version=1)

        restarted = create_app({"TESTING": True, "DB_PATH": self.db_path})
        client = restarted.test_client()
        after = client.get("/api/report?month=2026-03&version=1").get_json()
        latest = client.get("/api/report?month=2026-03").get_json()
        self.assertEqual(before, after)
        self.assertEqual(latest["version"]["version_no"], 2)
        self.assertEqual(
            {z["zone"]: z["boxes"] for z in latest["zones"]}, {"冷藏": 5, "冷冻": 12}
        )


class ConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "concurrent.sqlite3")
        init_db(self.db_path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_parallel_corrections_serialize_without_mixing_versions(self):
        app = create_app({"TESTING": True, "DB_PATH": self.db_path})
        client = app.test_client()
        csv = csv_bytes(
            [(f"OUT-{i:03d}", f"SKU-{i % 3}", f"2026-03-{i % 28 + 1:02d}T00:00:00Z", 1) for i in range(1, 29)]
        )
        response = client.post(
            "/api/months",
            data={"file": (io.BytesIO(csv), "m.csv")},
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 201)
        client.post(
            "/api/versions",
            json={"sku": "SKU-0", "reason": "init", "base_version": None,
                  "intervals": [{"zone": "冷藏", "valid_from": "2026-03-01T00:00:00Z"}]},
        )

        results = []
        barrier = threading.Barrier(6)

        def correct(index):
            conn = connect(self.db_path)
            barrier.wait()
            try:
                outcome = knowledge.register_ranges(
                    conn,
                    sku=f"SKU-{index % 3}",
                    intervals=[
                        {
                            "zone": "冷冻" if index % 2 else "冷藏",
                            "valid_from": f"2026-03-{index + 1:02d}T00:00:00Z",
                            "valid_to": f"2026-03-{index + 5:02d}T00:00:00Z",
                        }
                    ],
                    reason=f"c{index}",
                    base_version=1,
                )
                results.append(("ok", outcome["version_no"]))
            except (ConflictError, ValidationError) as exc:
                results.append(("rejected", str(exc)))
            finally:
                conn.close()

        threads = [threading.Thread(target=correct, args=(i,)) for i in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        versions = knowledge.list_versions(connect(self.db_path))
        committed = sorted(v["version_no"] for v in versions)
        accepted = [value for status, value in results if status == "ok"]
        # Exactly one request could win the v1 base; the rest must be rejected
        # rather than silently overwriting it.
        self.assertEqual(len(accepted), 1)
        self.assertEqual(committed, [1, 2])
        self.assertEqual(len([r for r in results if r[0] == "rejected"]), 5)

        # Each committed version is a complete, non-overlapping group.
        for version in versions:
            conn = connect(self.db_path)
            try:
                rows = knowledge.load_ranges(conn, version["version_no"])
            finally:
                conn.close()
            by_sku = {}
            for row in rows:
                by_sku.setdefault(row["sku"], []).append(row)
            for sku, items in by_sku.items():
                items.sort(key=lambda r: r["valid_from_us"])
                for previous, current in zip(items, items[1:]):
                    self.assertIsNotNone(previous["valid_to_us"])
                    self.assertLessEqual(previous["valid_to_us"], current["valid_from_us"])

    def test_report_during_correction_uses_one_snapshot(self):
        app = create_app({"TESTING": True, "DB_PATH": self.db_path})
        client = app.test_client()
        csv = csv_bytes(
            [("OUT-1", "SKU-A", "2026-03-01T00:00:00Z", 10), ("OUT-2", "SKU-A", "2026-03-20T00:00:00Z", 20)]
        )
        client.post(
            "/api/months",
            data={"file": (io.BytesIO(csv), "m.csv")},
            content_type="multipart/form-data",
        )
        client.post(
            "/api/versions",
            json={"sku": "SKU-A", "reason": "v1", "base_version": None,
                  "intervals": [{"zone": "冷藏", "valid_from": "2026-03-01T00:00:00Z"}]},
        )
        client.post(
            "/api/versions",
            json={"sku": "SKU-A", "reason": "v2", "base_version": 1,
                  "intervals": [
                      {"zone": "冷冻", "valid_from": "2026-03-10T00:00:00Z"},
                      {"zone": "冷藏", "valid_from": "2026-03-01T00:00:00Z", "valid_to": "2026-03-10T00:00:00Z"},
                  ]},
        )
        v1 = client.get("/api/report?month=2026-03&version=1").get_json()
        v2 = client.get("/api/report?month=2026-03&version=2").get_json()
        self.assertEqual({z["zone"]: z["boxes"] for z in v1["zones"]}, {"冷藏": 30})
        self.assertEqual({z["zone"]: z["boxes"] for z in v2["zones"]}, {"冷藏": 10, "冷冻": 20})
        # Totals are always conserved.
        for report in (v1, v2):
            self.assertEqual(
                report["classified_boxes"] + report["unclassified"]["boxes"],
                report["total_boxes"],
            )

    def test_query_is_not_mixed_when_correction_commits_mid_read(self):
        app = create_app({"TESTING": True, "DB_PATH": self.db_path})
        client = app.test_client()
        csv = csv_bytes(
            [
                ("OUT-1", "SKU-A", "2026-03-01T00:00:00Z", 10),
                ("OUT-2", "SKU-A", "2026-03-20T00:00:00Z", 20),
            ]
        )
        client.post(
            "/api/months",
            data={"file": (io.BytesIO(csv), "m.csv")},
            content_type="multipart/form-data",
        )
        client.post(
            "/api/versions",
            json={"sku": "SKU-A", "reason": "v1", "base_version": None,
                  "intervals": [{"zone": "冷藏", "valid_from": "2026-03-01T00:00:00Z"}]},
        )

        original_load_ranges = knowledge.load_ranges
        writer_done = []

        def racing_load_ranges(conn, version_no):
            if not writer_done:
                writer_done.append(True)
                other = connect(self.db_path)
                try:
                    knowledge.register_ranges(
                        other,
                        sku="SKU-A",
                        intervals=[{"zone": "冷冻", "valid_from": "2026-03-10T00:00:00Z"}],
                        reason="committed during read",
                        base_version=1,
                    )
                finally:
                    other.close()
            return original_load_ranges(conn, version_no)

        conn = connect(self.db_path)
        try:
            with unittest.mock.patch.object(
                knowledge, "load_ranges", side_effect=racing_load_ranges
            ):
                report = knowledge.build_report(conn, "2026-03", None)
        finally:
            conn.close()

        # The report pinned v1 before the concurrent commit and must not mix
        # any v2 interval into it: everything stays in 冷藏.
        self.assertTrue(writer_done)
        self.assertEqual(report["version"]["version_no"], 1)
        self.assertEqual(
            {bucket["zone"]: bucket["boxes"] for bucket in report["zones"]},
            {"冷藏": 30},
        )
        # The later query sees the new version.
        conn = connect(self.db_path)
        try:
            after = knowledge.build_report(conn, "2026-03", None)
        finally:
            conn.close()
        self.assertEqual(after["version"]["version_no"], 2)
        # v2 classified SKU-A only from 03-10 onward, so the 03-01 shipment
        # falls into a gap and is reported as unclassified.
        self.assertEqual(
            {bucket["zone"]: bucket["boxes"] for bucket in after["zones"]},
            {"冷冻": 20},
        )
        self.assertEqual(after["unclassified"]["boxes"], 10)

    def test_failed_write_rolls_back_whole_version(self):
        app = create_app({"TESTING": True, "DB_PATH": self.db_path})
        client = app.test_client()
        client.post(
            "/api/months",
            data={"file": (io.BytesIO(csv_bytes([("OUT-1", "SKU-A", "2026-03-01T00:00:00Z", 1)])), "m.csv")},
            content_type="multipart/form-data",
        )
        client.post(
            "/api/versions",
            json={"sku": "SKU-B", "reason": "v1", "base_version": None,
                  "intervals": [{"zone": "冷藏", "valid_from": "2026-03-01T00:00:00Z"}]},
        )

        conn = connect(self.db_path)
        try:
            conn.execute(
                "CREATE TRIGGER boom BEFORE INSERT ON version_ranges"
                " WHEN NEW.zone = 'BOOM' BEGIN SELECT RAISE(ABORT, 'injected failure'); END"
            )
        finally:
            conn.close()

        response = client.post(
            "/api/versions",
            json={"sku": "SKU-A", "reason": "v2", "base_version": 1,
                  "intervals": [{"zone": "BOOM", "valid_from": "2026-03-01T00:00:00Z"}]},
        )
        self.assertGreaterEqual(response.status_code, 400)
        conn = connect(self.db_path)
        try:
            versions = conn.execute("SELECT version_no FROM knowledge_versions").fetchall()
            ranges = conn.execute(
                "SELECT COUNT(*) AS c FROM version_ranges WHERE version_no = 2"
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual([row["version_no"] for row in versions], [1])
        self.assertEqual(ranges["c"], 0)
        # The ledger is still usable after the failed write.
        response = client.post(
            "/api/versions",
            json={"sku": "SKU-A", "reason": "retry", "base_version": 1,
                  "intervals": [{"zone": "冷冻", "valid_from": "2026-03-01T00:00:00Z"}]},
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.get_json()["version_no"], 2)


class CsvParsingTests(unittest.TestCase):
    def test_gb18030_and_alias_headers(self):
        raw = (
            "出库流水号,商品编码,出库时间,件数\n"
            "A-1,SKU-X,2026-03-01 08:00:00,4\n"
        ).encode("gb18030")
        month, rows, _ = facts.parse_csv(raw)
        self.assertEqual(month, "2026-03")
        self.assertEqual(rows[0]["boxes"], 4)
        self.assertEqual(rows[0]["occurred_at"], "2026-03-01T08:00:00.000000Z")

    def test_offsets_normalised_to_utc_month(self):
        raw = csv_bytes([("A-1", "SKU-X", "2026-03-01T01:00:00+08:00", 4)])
        month, rows, _ = facts.parse_csv(raw)
        self.assertEqual(month, "2026-02")
        self.assertEqual(rows[0]["occurred_at"], "2026-02-28T17:00:00.000000Z")

    def test_missing_column_rejected(self):
        raw = "流水号,SKU,箱数\nA-1,SKU-X,4\n".encode()
        with self.assertRaises(ValidationError):
            facts.parse_csv(raw)

    def test_blank_lines_ignored(self):
        raw = "流水号,SKU,出库时刻,箱数\nA-1,SKU-X,2026-03-01T00:00:00Z,4\n\n"
        month, rows, _ = facts.parse_csv(raw)
        self.assertEqual(len(rows), 1)

    def test_csv_injection_stays_data(self):
        raw = csv_bytes([("=cmd|' /C calc'!A0", "SKU-X", "2026-03-01T00:00:00Z", 4)])
        _, rows, _ = facts.parse_csv(raw)
        self.assertEqual(rows[0]["sequence_no"], "=cmd|' /C calc'!A0")


if __name__ == "__main__":
    unittest.main()
