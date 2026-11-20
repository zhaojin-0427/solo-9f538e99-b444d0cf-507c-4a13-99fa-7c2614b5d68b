#!/usr/bin/env python3
"""Generate example CSVs: one month of outbound shipments, plus a few months."""

from __future__ import annotations

import csv
import sys
from pathlib import Path

ROWS = [
    ("OUT-202603-0001", "SKU-YOGURT", "2026-03-01T02:15:00Z", 12),
    ("OUT-202603-0002", "SKU-YOGURT", "2026-03-08T23:59:59Z", 5),
    ("OUT-202603-0003", "SKU-BEEF", "2026-03-03T08:00:00Z", 7),
    ("OUT-202603-0004", "SKU-BEEF", "2026-03-15T12:00:00Z", 9),
    ("OUT-202603-0005", "SKU-ICE", "2026-03-10T00:00:00Z", 3),
    ("OUT-202603-0006", "SKU-UNKNOWN", "2026-03-20T06:30:00Z", 4),
]


def main(target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["流水号", "SKU", "出库时刻", "箱数"])
        writer.writerows(ROWS)


if __name__ == "__main__":
    main(Path(sys.argv[1] if len(sys.argv) > 1 else "examples/2026-03.csv"))
