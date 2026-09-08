#!/usr/bin/env python3
"""Download/import SEC quarterly ownership data before serving web traffic."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.sec_bulk import BulkOwnershipStore, DEFAULT_DB, download_quarter, quarter_url


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("quarters", nargs="+", help="quarters such as 2025q4 2026q1")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--zip", type=Path, help="import this local ZIP (exactly one quarter)")
    args = parser.parse_args()
    if args.zip and len(args.quarters) != 1:
        parser.error("--zip requires exactly one quarter")

    store = BulkOwnershipStore(args.db)
    try:
        for quarter in args.quarters:
            if args.zip:
                path = args.zip
            else:
                download_dir = Path(tempfile.gettempdir()) / "spystocks-sec"
                path = download_quarter(quarter, download_dir / f"{quarter}_form345.zip")
            counts = store.import_zip(path, quarter, source_url=quarter_url(quarter))
            print(json.dumps({"quarter": quarter.lower(), "db": str(args.db), "rows": counts}, sort_keys=True))
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
