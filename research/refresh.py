"""Update data/seed/conferences.json from the public feeds.

This is what the weekly GitHub Action runs. It is safe to run any time:
  python research/refresh.py                # fetch feeds, scan websites, update the dataset
  python research/refresh.py --no-hints     # skip reading each conference website
  python research/refresh.py --offline      # reuse research/sources/ from the last run
  python research/refresh.py --summary-file out.json   # also write a machine-readable summary

Rules are in app/feeds.py: feeds may add editions, set or move dates and CFP
deadlines, and fill blank links. Hand-verified edits ("confirmed") are never
overridden. Conferences listed with "remove": true in research/verified.json
are never re-added.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app import feeds  # noqa: E402

SEED = ROOT / "data" / "seed" / "conferences.json"
SRC = ROOT / "research" / "sources"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--no-hints", action="store_true")
    ap.add_argument("--summary-file")
    ap.add_argument("--seed", default=str(SEED))
    args = ap.parse_args()
    today = date.fromisoformat(os.environ["CONTRACKER_TODAY"]) if os.environ.get("CONTRACKER_TODAY") else date.today()
    blocked = set()
    vp = ROOT / "research" / "verified.json"
    if vp.exists():
        blocked = {s for s, v in json.loads(vp.read_text()).items() if v.get("remove")}
    summary = feeds.refresh_seed(Path(args.seed), SRC, fetch=not args.offline, hints=not args.no_hints, today=today, blocked=blocked)
    print(summary["text"])
    for slug, msg in summary["lines"]:
        print(f"  {slug}: {msg}")
    if args.summary_file:
        Path(args.summary_file).write_text(json.dumps({k: v for k, v in summary.items() if k != "lines"} | {"lines": summary["lines"]}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
