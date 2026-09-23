"""Turn data/seed/conferences.json into the static site in site/ (data.json + cities.json).

usage: python scripts/build_site.py
The GitHub Pages workflow runs this on every push; run it yourself to preview:
    python scripts/build_site.py && python -m http.server 8790 -d site
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app.geo import CITY_COORDS  # noqa: E402

SEED = ROOT / "data" / "seed" / "conferences.json"
SITE = ROOT / "site"

LINKS = ("website", "cfp_url", "cfv_url", "sponsor_url", "sponsor_docs_url", "discord_url", "twitter", "mastodon", "bluesky",
         "linkedin", "instagram", "youtube")
FIELDS = ("slug", "name", "type", "city", "region", "country", "lat", "lon", "online", "description", "attendance_est",
          "attendance_band", "vending_policy", "vending_notes", "table_cost", "workshop_track", "village_hosting",
          "iot_village_partner", "ticket_price", "archived", "typical_month", "contact_email", "cfp_email", "sponsor_email",
          "verification_notes", "hints_checked")


def clean(d: dict) -> dict:
    return {k: v for k, v in d.items() if v not in (None, "", [], {}, 0, False) or k in ("archived",)}


def main() -> None:
    data = json.loads(SEED.read_text())
    out = []
    for c in data["conferences"]:
        row = clean({k: c.get(k) for k in FIELDS})
        row["links"] = clean({k.replace("_url", ""): c.get(k) for k in LINKS})
        row["editions"] = []
        for e in sorted(c.get("editions", []), key=lambda e: int(e["year"])):
            ed = clean({"year": e["year"], "start": e.get("start_date"), "end": e.get("end_date"), "status": e.get("dates_status"),
                        "venue": e.get("venue"), "city": e.get("city"), "url": e.get("url"), "label": e.get("label"),
                        "training_start": e.get("training_start"), "training_end": e.get("training_end"), "notes": e.get("notes")})
            ed["deadlines"] = [clean({"kind": d.get("kind"), "due": d.get("due_date"), "url": d.get("url"), "notes": d.get("notes")})
                               for d in e.get("deadlines", []) if d.get("due_date")]
            row["editions"].append(ed)
        row["hints"] = [h.get("date") for h in (c.get("date_hints") or [])][:6]
        out.append(row)
    SITE.mkdir(exist_ok=True)
    (SITE / "data.json").write_text(json.dumps({"generated": data.get("generated"), "conferences": out}, ensure_ascii=False, separators=(",", ":")))
    cities = []
    for key, (lat, lon) in CITY_COORDS.items():
        city, region, country = key.split("|")
        cities.append({"c": city, "r": region, "k": country, "lat": lat, "lon": lon})
    (SITE / "cities.json").write_text(json.dumps(cities, ensure_ascii=False, separators=(",", ":")))
    print(f"site/data.json: {len(out)} conferences · site/cities.json: {len(cities)} cities · dataset {data.get('generated')}")


if __name__ == "__main__":
    main()
