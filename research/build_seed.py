"""Build data/seed/conferences.json from scratch.

This is the long way round; most people only ever need `research/refresh.py`,
which updates the existing dataset from the public feeds. Rebuilding from
scratch is for when the hand-verified overrides or the candidate list change.

Sources, in order of trust (later ones only fill blanks unless noted):
  1. research/verified.json           hand-verified facts (dates, policies, contacts)    [wins]
  2. a spreadsheet in the original "one sheet per year" layout (optional)
  3. research/new_conferences.json    candidates found by research (run new_conferences.py)
  4. bsides.org / makerfaire.com      authoritative for BSides and Maker Faire dates
  5. cfptime.org / cfp.hex.dance      CFP deadlines
  6. research/crawl/<slug>.json       contacts, socials, links found on each website (optional; run crawl.py)
Then: estimate a next edition for anything whose last known edition is in the past.

usage: python research/build_seed.py [--sheet FILE.xlsx] [--offline] [--no-hints]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app import feeds  # noqa: E402
from app.geo import geocode  # noqa: E402
from app.importer import parse_workbook  # noqa: E402

R = ROOT / "research"
SRC = R / "sources"
SEED = ROOT / "data" / "seed" / "conferences.json"

SOURCES = [
    {"name": "Security BSides calendar", "url": "https://bsides.org/events/", "notes": "Every BSides worldwide with dates; also an iCal feed"},
    {"name": "cfptime.org", "url": "https://www.cfptime.org/", "notes": "CFP deadlines with an open API"},
    {"name": "CFP Time (hex.dance)", "url": "https://cfp.hex.dance/", "notes": "Community-curated CFP list on GitHub"},
    {"name": "infosec-conferences.com", "url": "https://infosec-conferences.com/country/united-states/", "notes": "Big directory, lots of vendor events mixed in"},
    {"name": "cryptax/confsec", "url": "https://github.com/cryptax/confsec", "notes": "Europe-heavy list with CFP dates"},
    {"name": "sec-deadlines", "url": "https://sec-deadlines.github.io/", "notes": "Academic security conference deadlines"},
    {"name": "cfp.directory", "url": "https://cfp.directory/events", "notes": ""},
    {"name": "@cfp_time on infosec.exchange", "url": "https://infosec.exchange/@cfp_time", "notes": "Mastodon bot posting CFPs"},
    {"name": "infosecfp.com", "url": "https://www.infosecfp.com/", "notes": ""},
    {"name": "EasyChair CFPs", "url": "https://easychair.org/cfp2/", "notes": "Academic"},
    {"name": "Maker Faire map", "url": "https://makerfaire.com/map/", "notes": "All Maker Faires; the page loads a JSON feed the app re-reads"},
    {"name": "Hackaday events", "url": "https://hackaday.com/category/cons/", "notes": "Hardware-con coverage"},
    {"name": "Vintage Computer Federation", "url": "https://vcfed.org/events/", "notes": "VCF East / West / Midwest / Southwest / Southeast"},
    {"name": "DEF CON Groups", "url": "https://forum.defcon.org/social-groups", "notes": "Local groups often run mini-cons"},
]
TEMPLATES = [
    {"name": "First outreach — vendor table + workshop", "subject": "{{conference}} {{year}}: hardware vendor table + hands-on workshop from {{org}}",
     "body": "Hi {{conference}} team,\n\nI'm {{me}} with {{org}}. We build and sell hands-on hardware hacking kits and run workshops at community security conferences (BSides, DEF CON villages, and similar).\n\nFor {{conference}} {{year}} ({{dates}}) we'd love to:\n  • run a hands-on hardware workshop or mini-village, and\n  • have a small vendor table for kits and badges.\n\nCould you point me at the right person for vendor/sponsor tables and workshop proposals, or send the sponsor prospectus?\n\nThanks,\n{{me}}\n{{org}}"},
    {"name": "Follow-up (no reply)", "subject": "Re: {{conference}} {{year}}: vendor table + workshop from {{org}}",
     "body": "Hi again,\n\nJust floating this back to the top of your inbox — we'd still love to bring a hardware workshop and vendor table to {{conference}} {{year}}. Happy to work with whatever format fits your event.\n\nThanks,\n{{me}}\n{{org}}"},
    {"name": "Village / workshop proposal", "subject": "{{conference}} {{year}} village proposal: hands-on hardware hacking",
     "body": "Hi {{conference}} organizers,\n\n{{org}} would like to propose a hands-on hardware hacking village/workshop for {{conference}} {{year}}.\n\nWhat we bring:\n  • Guided hands-on exercises (soldering, firmware extraction, serial/JTAG, RF) sized for beginners through intermediate\n  • All equipment and kits; we only need tables, power, and a corner of a room\n  • Staff for the full event\n\nWe've run this format at [list a couple of cons]. Let me know what information you need for the village process.\n\nThanks,\n{{me}}\n{{org}}"},
    {"name": "Thank-you after the event", "subject": "Thank you from {{org}} — {{conference}} {{year}}",
     "body": "Hi {{conference}} team,\n\nThank you for having {{org}} at {{conference}} {{year}} — we had a great time and met a lot of people.\n\nWe'd love to come back next year. If you already have dates or a sponsor timeline for {{year}}+1, please keep us in the loop.\n\nThanks again,\n{{me}}\n{{org}}"},
]


def load(p, default=None):
    try:
        return json.loads(Path(p).read_text())
    except Exception:
        return default if default is not None else {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sheet", default=os.environ.get("CONTRACKER_SHEET"), help="spreadsheet in the one-sheet-per-year layout (optional)")
    ap.add_argument("--offline", action="store_true", help="use cached feeds in research/sources/")
    ap.add_argument("--no-hints", action="store_true", help="skip reading each website for date hints")
    args = ap.parse_args()
    today = date.fromisoformat(os.environ["CONTRACKER_TODAY"]) if os.environ.get("CONTRACKER_TODAY") else date.today()

    confs: dict[str, dict] = {}
    changes: list = []

    def register(c):
        c.setdefault("aliases", [])
        c.setdefault("editions", [])
        confs[c["slug"]] = c

    # 2. spreadsheet (optional)
    if args.sheet and Path(args.sheet).exists():
        for c in parse_workbook(args.sheet):
            register(c)
        print(f"sheet: {len(confs)} conferences from {args.sheet}")

    # 1b. names/aliases from verified.json, so feeds merge into the right entries
    verified = load(R / "verified.json", {})
    for slug, v in verified.items():
        if v.get("remove"):
            continue
        if slug not in confs:
            register({"slug": slug, "name": v.get("name", slug), "source": "research"})
        c = confs[slug]
        c["aliases"] = sorted(set(c["aliases"]) | set(v.get("aliases", [])) | ({v["name"]} if v.get("name") else set()))
        for k in ("name", "type", "city", "region", "country", "website"):   # so feeds match the right entry
            if v.get(k) and not c.get(k):
                c[k] = v[k]

    # 3. candidates
    for n in load(R / "new_conferences.json", []):
        if n["slug"] in confs:
            c = confs[n["slug"]]
            for k, v in n.items():
                if v in (None, "", []) or k == "editions":
                    continue
                if k == "aliases":
                    c["aliases"] = sorted(set(c["aliases"]) | set(v))
                elif c.get(k) in (None, "", []) or (k == "name" and c.get("name") == n["slug"]):
                    c[k] = v
            continue
        c = {k: v for k, v in n.items() if v not in (None, "", [])}
        register(c)

    # 4-5. feeds
    raw, errors = feeds.fetch_all(SRC, today, offline=args.offline, max_age_hours=0 if not args.offline else 1e9)
    for err in errors:
        print("feed problem:", err)
    blocked = {s for s, v in verified.items() if v.get("remove")}
    feeds.apply_records(confs, feeds.parse_all(raw, today), today, changes, blocked=blocked)

    # 6. crawl results: contacts and links (fill blanks only)
    crawl_dir = R / "crawl"
    for slug, c in confs.items():
        rec = load(crawl_dir / f"{slug}.json") if (crawl_dir / f"{slug}.json").exists() else None
        if not rec or not rec.get("home"):
            continue
        pages = [rec["home"]] + rec.get("sub", [])
        emails = {}
        for pg in pages:
            for em, src in pg.get("emails", {}).items():
                emails.setdefault(em, src)
        if rec.get("final_url") and not c.get("website"):
            c["website"] = rec["final_url"]

        def pick(pattern):
            for em in emails:
                if re.search(pattern, em.split("@")[0], re.I):
                    return em
            return None
        site_dom = re.sub(r"^www\.", "", re.sub(r"^https?://", "", c.get("website") or "").split("/")[0]).lower()
        own = [em for em in emails if site_dom and em.endswith("@" + site_dom)] or list(emails)
        if not c.get("sponsor_email"):
            c["sponsor_email"] = pick(r"sponsor|vendor|partner|exhibit")
        if not c.get("cfp_email"):
            c["cfp_email"] = pick(r"cfp|speak|papers|talks|program")
        if not c.get("contact_email"):
            c["contact_email"] = pick(r"^(info|hello|contact|team|organi[sz]ers?|hi|admin|staff|board|crew)$") or (own[0] if own else None)
        soc, pg_links = {}, {}
        for pg in pages:
            for k, us in pg.get("social", {}).items():
                soc.setdefault(k, [])
                soc[k] += [u for u in us if u not in soc[k]]
            for k, us in pg.get("pages", {}).items():
                pg_links.setdefault(k, [])
                pg_links[k] += [u for u in us if u not in pg_links[k]]
        for key, field in (("discord", "discord_url"), ("twitter", "twitter"), ("mastodon", "mastodon"), ("bluesky", "bluesky"),
                           ("linkedin", "linkedin"), ("instagram", "instagram"), ("youtube", "youtube")):
            if soc.get(key) and not c.get(field):
                c[field] = soc[key][0]
        for key, field in (("cfp", "cfp_url"), ("sponsor", "sponsor_url"), ("village", "cfv_url")):
            if pg_links.get(key) and not c.get(field):
                c[field] = pg_links[key][0]
        pdfs = [p for pg in pages for p in pg.get("pdfs", [])]
        spons_pdf = next((u for u, lab in pdfs if re.search(r"sponsor|prospectus|vendor|exhibit|partner", (u + lab).lower())), None)
        if spons_pdf and not c.get("sponsor_docs_url"):
            c["sponsor_docs_url"] = spons_pdf

    # 1. hand-verified facts win
    for slug, v in verified.items():
        if v.get("remove"):
            confs.pop(slug, None)
            continue
        c = confs[slug]
        for k, val in v.items():
            if k == "editions":
                for ve in val:
                    e = feeds.get_edition(c, int(ve["year"]), changes, "verified")
                    for kk, vv in ve.items():
                        if kk == "deadlines":
                            for d in vv:
                                ex = next((x for x in e["deadlines"] if x.get("kind") == d["kind"] and not x.get("label")), None)
                                if ex:
                                    ex.update({kk2: vv2 for kk2, vv2 in d.items() if vv2 not in (None, "")})
                                else:
                                    e["deadlines"].append({"kind": d["kind"], "label": None, "due_date": d.get("due_date"), "status": d.get("status") or "open", "url": d.get("url"), "notes": d.get("notes")})
                        else:
                            e[kk] = vv
                    e["source"] = "verified"
                    e["last_verified"] = today.isoformat()
            elif k == "contacts":
                c["contacts"] = val
            elif k == "aliases":
                c["aliases"] = sorted(set(c.get("aliases", [])) | set(val))
            else:
                c[k] = val
        c["last_verified"] = today.isoformat()

    # geocode, estimate next editions, optional website hints
    for c in confs.values():
        if not c.get("online") and c.get("city") and c.get("lat") is None:
            g = geocode(c["city"], c.get("region"), c.get("country"))
            if g:
                c["lat"], c["lon"] = g
    feeds.estimate_next_editions(confs, today, changes)
    if not args.no_hints:
        stats = feeds.scan_date_hints(confs, today, changes)
        print(f"website hints: {stats}")

    if SEED.exists():   # keep website date hints from the previous build unless we just rescanned
        for oc in load(SEED, {}).get("conferences", []):
            c = confs.get(oc.get("slug"))
            if c and oc.get("date_hints") and not c.get("hints_checked"):
                c["date_hints"], c["hints_checked"] = oc["date_hints"], oc.get("hints_checked")
    data = {"generated": today.isoformat(), "conferences": [], "sources": SOURCES, "templates": TEMPLATES}
    SEED.parent.mkdir(parents=True, exist_ok=True)
    feeds.write_seed(SEED, data, confs, today, removed=blocked)
    n_ed = sum(len(c["editions"]) for c in confs.values())
    print(f"{len(confs)} conferences, {n_ed} editions written to {SEED.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
