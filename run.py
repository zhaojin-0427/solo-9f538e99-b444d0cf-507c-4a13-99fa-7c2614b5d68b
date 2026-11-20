#!/usr/bin/env python3
"""Development/production entry point.

Usage:
    python run.py                 # serve on 0.0.0.0:8000
    COLDCHAIN_DB=/data/cc.sqlite3 python run.py
"""

from __future__ import annotations

import os

from coldchain.app import create_app

app = create_app()

if __name__ == "__main__":
    app.run(
        host=os.environ.get("COLDCHAIN_HOST", "0.0.0.0"),
        port=int(os.environ.get("COLDCHAIN_PORT", "8000")),
        debug=os.environ.get("COLDCHAIN_DEBUG") == "1",
    )
