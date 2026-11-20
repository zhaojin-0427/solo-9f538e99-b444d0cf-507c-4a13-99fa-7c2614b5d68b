"""撤销某 SKU 的全部区间（提交空组）时的行为。"""

from __future__ import annotations

import io
import unittest

from coldchain.app import create_app


class EmptyGroupTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path

        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "e.sqlite3")
        app = create_app({"TESTING": True, "DB_PATH": self.db_path})
        self.client = app.test_client()

    def tearDown(self):
        self.tmp.cleanup()

    def test_retract_sku_with_empty_group(self):
        csv = "流水号,SKU,出库时刻,箱数\nOUT-1,SKU-A,2026-03-01T00:00:00Z,4\n"
        self.client.post(
            "/api/months",
            data={"file": (io.BytesIO(csv.encode()), "m.csv")},
            content_type="multipart/form-data",
        )
        self.client.post(
            "/api/versions",
            json={"sku": "SKU-A", "reason": "v1", "base_version": None,
                  "intervals": [{"zone": "冷藏", "valid_from": "2026-03-01T00:00:00Z"}]},
        )
        self.client.post(
            "/api/versions",
            json={"sku": "SKU-B", "reason": "v2", "base_version": 1,
                  "intervals": [{"zone": "冷冻", "valid_from": "2026-03-01T00:00:00Z"}]},
        )
        response = self.client.post(
            "/api/versions",
            json={"sku": "SKU-A", "reason": "retract", "base_version": 2, "intervals": []},
        )
        self.assertEqual(response.status_code, 201)
        detail = self.client.get("/api/versions/3").get_json()
        self.assertEqual([group["sku"] for group in detail["skus"]], ["SKU-B"])
        report = self.client.get("/api/report?month=2026-03&version=3").get_json()
        self.assertEqual(report["unclassified"]["boxes"], 4)
        # Old version untouched.
        report1 = self.client.get("/api/report?month=2026-03&version=1").get_json()
        self.assertEqual({z["zone"]: z["boxes"] for z in report1["zones"]}, {"冷藏": 4})


if __name__ == "__main__":
    unittest.main()
