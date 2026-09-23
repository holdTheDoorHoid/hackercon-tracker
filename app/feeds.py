"""Pull dates and deadlines from public conference feeds and merge them into the
dataset without touching anything a person typed.

Feeds
  bsides.org       Tribe Events REST API   authoritative for BSides dates
  makerfaire.com   map query feed          authoritative for Maker Faire dates
  cfptime.org      public API              CFP deadlines; dates only fill blanks
  cfp.hex.dance    README table on GitHub  CFP deadlines
  each conference website (optional)       "date hints": date-looking text for an
                                           edition we have not confirmed yet

The merge rules live here once and are used from three places:
  research/refresh.py          updates data/seed/conferences.json (weekly GitHub Action)
  Settings > "Refresh now"     updates the live SQLite database
  the app's background timer   same, every N days

Rules, in one breath: feeds may add editions, set or move dates, add or move CFP /
Call-for-Makers deadlines, and fill blank links. They never touch a person's own
status, notes, hotel/travel/PTO, priorities, or a deadline's submitted/accepted
state, and they never override an edition whose dates_status is "confirmed".
"""
from __future__ import annotations

import concurrent.futures
import html as htmllib
import json
import re
import threading
import time
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

from . import db as dbm
from .geo import geocode, norm_country
from .importer import CONF_COLS, DEADLINE_COLS, EDITION_COLS, slugify

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
BSIDES_API = "https://bsides.org/wp-json/tribe/events/v1/events"
CFPTIME_UPCOMING = "https://api.cfptime.org/api/upcoming/"
CFPTIME_ALL = "https://api.cfptime.org/api/cfps/"
HEXDANCE_README = "https://raw.githubusercontent.com/Astarte-Security/cfp-time/main/README.md"
MAKERFAIRE_MAP = "https://makerfaire.com/query/?type=map"

RANK = {"unknown": 0, "estimated": 1, "tentative": 2, "postponed": 2, "cancelled": 2, "announced": 3, "confirmed": 4}
SOFT = ("estimated", "tentative", "unknown")
PERSONAL_CONF = {"relationship_notes", "past_rating", "past_sales", "past_notes", "priority"}
PERSONAL_EDITION = {"our_status", "hotel", "travel", "pto", "budget_est", "sales", "leads", "worth_it", "aar_notes", "outreach"}
USER_DEADLINE_STATUS = {"submitted", "accepted", "rejected", "declined", "waitlist", "done", "na"}
INTERVALS = {"toorcamp": 2, "emf-camp": 2, "ccc-camp": 4}
JSON_FIELDS = {"aliases", "date_hints"}
MONTH = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?"

_lock = threading.Lock()


# ---------------------------------------------------------------- small helpers

def norm_full(s: str | None) -> str:
    """Like norm() but keeps parentheticals: 'BSides Birmingham (USA)' stays distinct from the UK one."""
    s = (s or "").lower()
    s = s.split(" – ")[0].split(" - ")[0]
    s = re.sub(r"\b20\d\d\b", "", s)
    s = s.replace("security bsides", "bsides")
    return re.sub(r"[^a-z0-9]", "", s)


def norm(s: str | None) -> str:
    s = (s or "").lower()
    s = re.sub(r"\(.*?\)", "", s)
    s = s.split(" – ")[0].split(" - ")[0]
    s = re.sub(r"\b20\d\d\b", "", s)
    s = s.replace("security bsides", "bsides")
    return re.sub(r"[^a-z0-9]", "", s)


def shift_year(start: str, end: str | None, years: int) -> tuple[str, str | None]:
    """Same Nth weekday of the same month, `years` later (the usual con pattern)."""
    s = date.fromisoformat(start)
    n = (s.day - 1) // 7 + 1
    tm = date(s.year + years, s.month, 1)
    delta = (s.weekday() - tm.weekday()) % 7
    day = 1 + delta + (n - 1) * 7
    try:
        ns = date(tm.year, tm.month, day)
    except ValueError:
        ns = date(tm.year, tm.month, day - 7)
    ne = (ns + (date.fromisoformat(end) - s)).isoformat() if end else None
    return ns.isoformat(), ne


def fmt_range(start: str | None, end: str | None) -> str:
    if not start:
        return "?"
    s = date.fromisoformat(start)
    if not end or end == start:
        return s.strftime("%b %-d, %Y")
    e = date.fromisoformat(end)
    if s.month == e.month:
        return f"{s.strftime('%b')} {s.day}–{e.day}, {s.year}"
    return f"{s.strftime('%b %-d')} – {e.strftime('%b %-d')}, {s.year}"


def _fix_url(u: str | None) -> str | None:
    u = (u or "").strip()
    if not u or u in ("0", "#"):
        return None
    return u if u.startswith("http") else "https://" + u


def _looks_like_cfp(u: str | None) -> bool:
    u = (u or "").lower()
    return any(k in u for k in ("cfp", "sessionize", "pretalx", "papercall", "call-for", "speak", "submit"))


def _chg(changes, kind, slug, obj, field=None, old=None, new=None, year=None, parent=None, source=None, msg=None):
    changes.append({"kind": kind, "slug": slug, "year": year, "field": field, "old": old, "new": new,
                    "obj": obj, "parent": parent, "source": source, "msg": msg})


def _setf(changes, kind, slug, obj, field, value, year=None, parent=None, source=None, msg=None) -> bool:
    old = obj.get(field)
    if old == value or (old in (None, "") and value in (None, "")):
        return False
    obj[field] = value
    _chg(changes, kind, slug, obj, field, old, value, year, parent, source, msg)
    return True


# ---------------------------------------------------------------- fetching

def _get(url: str, headers: dict | None = None, timeout: int = 60) -> bytes:
    h = {"User-Agent": UA, "Accept": "application/json, text/plain, */*"}
    h.update(headers or {})
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def fetch_bsides(today: date) -> list[dict]:
    start = (today - timedelta(days=420)).isoformat()
    end = (today + timedelta(days=900)).isoformat()
    events, page = [], 1
    while page <= 30:
        d = json.loads(_get(f"{BSIDES_API}?start_date={start}&end_date={end}&per_page=50&page={page}"))
        events += d.get("events", [])
        if page >= int(d.get("total_pages") or 1):
            break
        page += 1
    return events


def fetch_cfptime(today: date) -> list[dict]:
    seen, out = set(), []
    for u in (CFPTIME_UPCOMING, CFPTIME_ALL):
        for it in json.loads(_get(u)):
            if it.get("id") in seen:
                continue
            seen.add(it.get("id"))
            out.append(it)
    return out


def fetch_hexdance(today: date) -> str:
    return _get(HEXDANCE_README).decode("utf-8", "replace")


def fetch_makerfaire(today: date) -> dict:
    return json.loads(_get(MAKERFAIRE_MAP, headers={"Referer": "https://makerfaire.com/map/"}))


FETCHERS = {"bsides": (fetch_bsides, "json"), "cfptime": (fetch_cfptime, "json"),
            "hexdance": (fetch_hexdance, "md"), "makerfaire": (fetch_makerfaire, "json")}


def fetch_all(cache_dir: Path, today: date, offline: bool = False, max_age_hours: float = 6) -> tuple[dict, list[str]]:
    """Fetch every feed, keeping a copy in cache_dir. On failure fall back to the cached copy."""
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    raw, errors = {}, []
    for name, (fn, ext) in FETCHERS.items():
        p = cache_dir / f"{name}.{ext}"
        fresh = p.exists() and (time.time() - p.stat().st_mtime) < max_age_hours * 3600
        data = None
        if not (offline or fresh):
            try:
                data = fn(today)
                p.write_text(json.dumps(data) if ext == "json" else data)
            except Exception as ex:  # noqa: BLE001
                errors.append(f"{name}: {type(ex).__name__}: {str(ex)[:120]}")
        if data is None and p.exists():
            try:
                data = json.loads(p.read_text()) if ext == "json" else p.read_text()
            except Exception as ex:  # noqa: BLE001
                errors.append(f"{name} cache unreadable: {ex}")
        raw[name] = data if data is not None else ([] if ext == "json" else "")
    return raw, errors


# ---------------------------------------------------------------- parsing into records

def _rec(**kw) -> dict:
    r = {"source": None, "name": None, "key": None, "start": None, "end": None, "year": None, "status": "announced",
         "city": None, "region": None, "country": None, "venue": None, "website": None, "lat": None, "lon": None,
         "cfp_due": None, "cfp_url": None, "cfv_due": None, "cfv_url": None, "twitter": None, "mastodon": None,
         "type": None, "description": None, "authoritative": False, "create": False, "note": None}
    r.update(kw)
    return r


def parse_bsides(events: list[dict], today: date) -> list[dict]:
    out = []
    for ev in events or []:
        title = re.sub(r"\s*\+$", "", htmllib.unescape(ev.get("title") or "").strip())
        start = (ev.get("start_date") or "")[:10]
        if not title or not start:
            continue
        end = (ev.get("end_date") or "")[:10]
        v = ev.get("venue") or {}
        if isinstance(v, list):
            v = v[0] if v else {}
        up = title.upper()
        status = "postponed" if "POSTPONED" in up else ("cancelled" if "CANCEL" in up else "announced")
        name = re.sub(r"\s*(20\d\d)?\s*(POSTPONED|CANCELLED|CANCELED).*$", "", title, flags=re.I).strip()
        key = "bsideslondonon" if ("(canada)" in title.lower() and "london" in title.lower()) else None
        city = v.get("city")
        if city and "(multiple" in city.lower():
            city = None
        out.append(_rec(source="bsides.org", name=name, key=key, start=start, end=end if end and end != start else None,
                        status=status, city=city, region=v.get("state") or v.get("province") or v.get("stateprovince"),
                        country=norm_country(v.get("country")), venue=v.get("venue"),
                        website=_fix_url(ev.get("website") or v.get("website")), type="bsides", authoritative=True,
                        create=status == "announced" and start >= (today - timedelta(days=30)).isoformat(),
                        note=(f"bsides.org lists this edition as: {title}" if status != "announced" else None)))
    return out


def _mf_date(s: str | None):
    s = (s or "").strip()
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s[:10], fmt).date()
        except ValueError:
            pass
    return None


def parse_makerfaire(payload: dict, today: date) -> list[dict]:
    out = []
    for x in (payload or {}).get("Locations", []):
        sd = _mf_date(x.get("event_start_dt"))
        if not sd or sd.year < 2025:
            continue
        nm = (x.get("faire_name") or "").strip()
        if not nm or "school" in (x.get("category") or "").lower() or "school" in nm.lower():
            continue
        ed = _mf_date(x.get("event_end_dt"))
        cc = norm_country(x.get("venue_address_country") or "")
        try:
            lat, lon = float(x.get("lat")), float(x.get("lng"))
        except (TypeError, ValueError):
            lat = lon = None
        out.append(_rec(source="makerfaire.com", name=nm, start=sd.isoformat(), end=ed.isoformat() if ed and ed != sd else None,
                        city=x.get("venue_address_city") or None, region=x.get("venue_address_state") or None, country=cc,
                        lat=lat, lon=lon, website=_fix_url(x.get("faire_url")), type="maker",
                        description=f"{x.get('category') or ''} Maker Faire".strip(),
                        cfv_due=(_mf_date(x.get("cfm_end_dt")).isoformat() if _mf_date(x.get("cfm_end_dt")) else None),
                        cfv_url=_fix_url(x.get("cfm_url")), authoritative=True,
                        create=cc in ("US", "CA") and sd >= today - timedelta(days=30)))
    return out


def parse_cfptime(items: list[dict], today: date) -> list[dict]:
    out = []
    for it in items or []:
        nm = re.sub(r"^Call for Papers\s+", "", it.get("name") or "").split(" | ")[0].split(" - ")[0].strip()
        start = (it.get("conf_start_date") or "")[:10]
        if not nm or not start:
            continue
        end = None
        try:
            nd = int(it.get("number_of_days") or 1)
            if nd > 1:
                end = (date.fromisoformat(start) + timedelta(days=nd - 1)).isoformat()
        except (TypeError, ValueError):
            pass
        tw = (it.get("twitter") or "").strip()
        twitter = mastodon = None
        if tw and tw.upper() != "NA":
            if tw.startswith("@") or "x.com" in tw or "twitter.com" in tw:
                twitter = tw
            elif "/@" in tw:
                mastodon = tw
        site = _fix_url(it.get("website"))
        out.append(_rec(source="cfptime.org", name=nm, start=start, end=end, city=it.get("city") if (it.get("city") or "").upper() != "NA" else None,
                        country=norm_country(it.get("country")) if (it.get("country") or "").upper() != "NA" else None,
                        cfp_due=(it.get("cfp_deadline") or "")[:10] or None, cfp_url=site, twitter=twitter, mastodon=mastodon))
    return out


def parse_hexdance(md: str, today: date) -> list[dict]:
    out = []
    for line in (md or "").splitlines():
        m = re.match(r"\|\s*\[(.+?)\]\((.+?)\)\s*\|\s*(.+?)\s*\|\s*(.+?)\s*\|\s*(.+?)\s*\|\s*(?:\[.*?\]\((.+?)\))?", line)
        if not m:
            continue
        name, site, cfp_end, dates, loc, cfp_link = m.groups()
        ym = re.search(r"20\d\d", dates or "")
        if not ym:
            continue
        try:
            due = datetime.strptime(re.sub(r"\s+", " ", cfp_end.replace(",", "")).strip(), "%B %d %Y").date().isoformat()
        except ValueError:
            due = None
        if not due:
            continue
        out.append(_rec(source="hex.dance", name=name.strip(), year=int(ym.group()), website=_fix_url(site), cfp_due=due, cfp_url=cfp_link))
    return out


def parse_all(raw: dict, today: date) -> list[dict]:
    return (parse_bsides(raw.get("bsides"), today) + parse_makerfaire(raw.get("makerfaire"), today)
            + parse_hexdance(raw.get("hexdance"), today) + parse_cfptime(raw.get("cfptime"), today))


# ---------------------------------------------------------------- matching + merging

class Index:
    def __init__(self, confs: dict[str, dict]):
        self.map: dict[str, str] = {}
        for c in confs.values():
            self.add(c)

    def add(self, c: dict):
        for n in [c.get("name"), c.get("slug")] + list(c.get("aliases") or []):
            if n:
                self.map.setdefault("=" + norm_full(n), c["slug"])
                self.map.setdefault(norm(n), c["slug"])

    def find(self, name: str | None, key: str | None = None) -> str | None:
        for k in (key, "=" + norm_full(name), norm(name)):
            if k and k in self.map:
                return self.map[k]
        k = norm(name)
        for a, b in (("asheville", "ashville"), ("nwa", "nwarkansas"), ("saint", "st"), ("mount", "mt"), ("fort", "ft")):
            if a in k and k.replace(a, b) in self.map:
                return self.map[k.replace(a, b)]
            if b in k and k.replace(b, a) in self.map:
                return self.map[k.replace(b, a)]
        return None


def get_edition(c: dict, year: int, changes: list, source: str | None = None) -> dict:
    for e in c["editions"]:
        if int(e["year"]) == int(year):
            e.setdefault("deadlines", [])
            return e
    e = {"year": int(year), "deadlines": [], "source": source or "research"}
    c["editions"].append(e)
    c["editions"].sort(key=lambda x: int(x["year"]))
    _chg(changes, "edition_new", c["slug"], e, year=year, parent=c, source=source)
    return e


def merge_dates(c: dict, e: dict, start: str, end: str | None, status: str, source: str | None,
                authoritative: bool, today: date, changes: list) -> bool:
    """Apply incoming dates to an edition if they outrank what we have. Returns True if changed."""
    slug, year = c["slug"], e["year"]
    cur = e.get("dates_status") or ("announced" if e.get("start_date") else "unknown")
    cur_rank, new_rank = RANK.get(cur, 0), RANK.get(status, 3)
    differs = (e.get("start_date") != start) or ((e.get("end_date") or None) != (end or None))
    if cur == "confirmed" and status != "confirmed":
        return False
    takes = (cur_rank < new_rank) or (differs and authoritative and cur_rank == new_rank) \
        or (cur in ("postponed", "cancelled") and authoritative and status == "announced" and differs)
    if not takes:
        return False
    old_start, old_end = e.get("start_date"), e.get("end_date")
    changed = False
    if differs:
        if old_start and cur in ("announced", "confirmed"):
            msg = f"{year}: dates changed to {fmt_range(start, end)}, was {fmt_range(old_start, old_end)} ({source})"
        elif status in SOFT:
            msg = f"{year}: {status} {fmt_range(start, end)} ({source})"
        else:
            msg = f"{year}: dates announced {fmt_range(start, end)} ({source})"
        _setf(changes, "edition_field", slug, e, "start_date", start, year, c, source, msg)
        _setf(changes, "edition_field", slug, e, "end_date", end, year, c, source)
        changed = True
    if _setf(changes, "edition_field", slug, e, "dates_status", status, year, c, source,
             None if differs else f"{year}: dates now {status} ({source})"):
        changed = True
    if changed:
        if source:
            _setf(changes, "edition_field", slug, e, "source", source, year, c)
        _setf(changes, "edition_field", slug, e, "last_verified", today.isoformat(), year, c)
        if (e.get("notes") or "").startswith("Estimated") and status not in SOFT:
            _setf(changes, "edition_field", slug, e, "notes", None, year, c)
    return changed


def merge_deadline(c: dict, e: dict, kind: str, due: str | None, url: str | None, source: str | None,
                   today: date, changes: list, notes: str | None = None) -> None:
    slug, year = c["slug"], e["year"]
    e.setdefault("deadlines", [])
    d = next((d for d in e["deadlines"] if d.get("kind") == kind and not d.get("label")), None)
    t = today.isoformat()
    if not d:
        if not due:
            return
        d = {"kind": kind, "label": None, "due_date": due, "status": "open" if due >= t else "closed", "url": url, "notes": notes}
        e["deadlines"].append(d)
        _chg(changes, "deadline_new", slug, d, year=year, parent=e, source=source,
             msg=f"{year}: {kind.upper()} deadline {due} ({source})")
        return
    moved = False
    was = d.get("due_date")
    stale = (today - timedelta(days=14)).isoformat()
    estimated = (d.get("notes") or "").startswith("Estimated")
    may_move = False
    if due and due != was:
        if not was or estimated:
            may_move = True                      # nothing real there yet
        elif was < stale and due < stale:
            may_move = False                     # both long past: that is history, leave it
        elif due > was:
            may_move = True                      # deadline extended (the common case)
    if may_move:
        _setf(changes, "deadline_field", slug, d, "due_date", due, year, e, source,
              f"{year}: {kind.upper()} deadline {due}" + (f", was {was}" if was else "") + f" ({source})")
        if (d.get("notes") or "").startswith("Estimated"):
            _setf(changes, "deadline_field", slug, d, "notes", notes, year, e)
        moved = True
    if url and not d.get("url"):
        _setf(changes, "deadline_field", slug, d, "url", url, year, e)
    st = d.get("status") or "open"
    if st in ("open", "closed") and d.get("due_date"):
        want = "open" if d["due_date"] >= t else "closed"
        if want != st and (want == "closed" or moved):
            _setf(changes, "deadline_field", slug, d, "status", want, year, e)


def _fill(changes, c, field, value, source=None):
    if value in (None, "", [], "unknown") or c.get(field) not in (None, "", "unknown", 0):
        return False
    return _setf(changes, "conference_field", c["slug"], c, field, value, source=source)


def apply_records(confs: dict[str, dict], records: list[dict], today: date, changes: list,
                  allow_create: bool = True, blocked: set[str] | None = None, idx: Index | None = None) -> None:
    idx = idx or Index(confs)
    blocked = blocked or set()
    seen_cfp = set()
    for r in records:
        slug = idx.find(r["name"], r.get("key"))
        if slug and r.get("country") and confs[slug].get("country") and r["country"] != confs[slug]["country"] \
                and r["source"] in ("bsides.org", "makerfaire.com"):
            slug = None  # same name, different country (the two BSides Birminghams): not the same event
        if not slug:
            if not (allow_create and r.get("create") and r.get("country") in ("US", "CA")):
                continue
            slug = slugify(r["name"])
            if slug in blocked or not slug:
                continue
            if slug in confs and (confs[slug].get("country") or r["country"]) != r["country"]:
                slug = f"{slug}-{r['country'].lower()}"
            if slug in blocked:
                continue
            if slug in confs:
                idx.map[norm(r["name"])] = slug
            else:
                c = {"slug": slug, "name": r["name"], "type": r.get("type") or "hacker", "aliases": [], "editions": [],
                     "source": r["source"], "city": r.get("city"), "region": r.get("region"), "country": r.get("country"),
                     "website": r.get("website"), "lat": r.get("lat"), "lon": r.get("lon"), "description": r.get("description")}
                if c["lat"] is None and c.get("city"):
                    g = geocode(c["city"], c.get("region"), c.get("country"))
                    if g:
                        c["lat"], c["lon"] = g
                confs[slug] = c
                idx.add(c)
                _chg(changes, "conference_new", slug, c, source=r["source"], msg=f"New conference from {r['source']}")
        c = confs[slug]
        c.setdefault("editions", [])
        src = r["source"]
        if not c.get("type") and r.get("type"):
            _fill(changes, c, "type", r["type"], src)
        if r.get("city"):
            if _fill(changes, c, "city", r["city"], src):
                _fill(changes, c, "region", r.get("region"), src)
        _fill(changes, c, "country", r.get("country"), src)
        _fill(changes, c, "website", r.get("website"), src)
        if c.get("lat") is None and r.get("lat") is not None:
            _fill(changes, c, "lat", r["lat"], src)
            _fill(changes, c, "lon", r["lon"], src)
        _fill(changes, c, "twitter", r.get("twitter"), src)
        _fill(changes, c, "mastodon", r.get("mastodon"), src)
        if _looks_like_cfp(r.get("cfp_url")):
            _fill(changes, c, "cfp_url", r["cfp_url"], src)
        _fill(changes, c, "cfv_url", r.get("cfv_url"), src)
        if not c.get("description") and r.get("description"):
            _fill(changes, c, "description", r["description"], src)

        year = r.get("year") or (int(r["start"][:4]) if r.get("start") else None)
        if not year:
            continue
        if src == "cfptime.org":
            if (slug, r.get("start")) in seen_cfp:
                continue
            seen_cfp.add((slug, r.get("start")))
        e = get_edition(c, year, changes, src)
        if r["status"] in ("postponed", "cancelled"):
            if e.get("dates_status") != "confirmed":
                _setf(changes, "edition_field", slug, e, "dates_status", r["status"], year, c, src, f"{year}: {r['status']} ({src})")
                if not e.get("notes") or e["notes"].startswith("Estimated") or e["notes"].startswith("bsides.org lists"):
                    _setf(changes, "edition_field", slug, e, "notes", r.get("note"), year, c)
            continue
        stale = (today - timedelta(days=14)).isoformat()
        if r.get("start") and not (e.get("start_date") and r["start"] < stale and e["start_date"] < stale):
            merge_dates(c, e, r["start"], r.get("end"), "announced", src, bool(r.get("authoritative")), today, changes)
        if r.get("venue") and not e.get("venue"):
            _setf(changes, "edition_field", slug, e, "venue", r["venue"], year, c, src)
        if r.get("cfp_due"):
            merge_deadline(c, e, "cfp", r["cfp_due"], r.get("cfp_url"), src, today, changes)
        if r.get("cfv_due"):
            merge_deadline(c, e, "cfv", r["cfv_due"], r.get("cfv_url"), src, today, changes, notes="Call for Makers closes")


def estimate_next_editions(confs: dict[str, dict], today: date, changes: list) -> None:
    """Anything whose last known edition is in the past gets a guessed next edition."""
    t = today.isoformat()
    for slug, c in confs.items():
        if c.get("archived") or c.get("priority") == "ignore":
            continue
        eds = sorted(c.get("editions", []), key=lambda e: int(e["year"]))
        dated = [e for e in eds if e.get("start_date")]
        if not dated or any(e["start_date"] >= t for e in dated):
            continue
        last = dated[-1]
        if last.get("dates_status") == "cancelled":
            continue
        last_start = date.fromisoformat(last["start_date"])
        interval = int(c.get("interval_years") or INTERVALS.get(slug, 1))
        nxt_year = last_start.year + interval
        if any(int(e["year"]) == nxt_year and e.get("dates_status") in ("cancelled", "postponed") for e in eds):
            continue
        s, e_ = shift_year(last["start_date"], last.get("end_date"), interval)
        hops = 0
        while s < t and hops < 3:
            s, e_ = shift_year(s, e_, interval)
            nxt_year += interval
            hops += 1
        if any(int(e["year"]) == nxt_year and e.get("start_date") for e in eds):
            continue
        e = get_edition(c, nxt_year, changes, "estimated from previous year")
        note = f"Estimated: same weekend as {last_start.year} ({last['start_date']}). Confirm on the website."
        if last.get("dates_status") in SOFT:
            note = f"Estimated from an unconfirmed {last_start.year} estimate ({last['start_date']}); the event may have paused. Check the website."
        for k, v in (("start_date", s), ("end_date", e_), ("dates_status", "estimated"),
                     ("source", "estimated from previous year"), ("notes", note)):
            _setf(changes, "edition_field", slug, e, k, v, nxt_year, c, "estimate",
                  f"{nxt_year}: estimated {fmt_range(s, e_)} (same weekend as {last_start.year})" if k == "start_date" else None)
        for d in last.get("deadlines", []):
            if d.get("due_date") and d.get("kind") in ("cfp", "cfv", "training", "vendor", "sponsor"):
                nd, _ = shift_year(d["due_date"], None, interval)
                merge_deadline(c, e, d["kind"], nd, d.get("url"), "estimate", today, changes, notes="Estimated from last year's deadline")
        if not c.get("typical_month"):
            _setf(changes, "conference_field", slug, c, "typical_month", last_start.month)


# ---------------------------------------------------------------- website date hints

def _html_text(raw: bytes) -> str:
    h = raw.decode("utf-8", "replace")
    h = re.sub(r"(?is)<(script|style|noscript|svg|template).*?</\1>", " ", h)
    h = re.sub(r"(?s)<[^>]+>", " ", h)
    return re.sub(r"\s+", " ", htmllib.unescape(h)).strip()


def _date_regexes(years: list[int]):
    yy = "|".join(str(y)[2:] for y in years)
    return [
        re.compile(rf"\b({MONTH}\s+\d{{1,2}}(?:st|nd|rd|th)?(?:\s*[-–—&]\s*(?:{MONTH}\s+)?\d{{1,2}}(?:st|nd|rd|th)?)?,?\s+20(?:{yy}))", re.I),
        re.compile(rf"\b(\d{{1,2}}(?:st|nd|rd|th)?(?:\s*[-–—&]\s*\d{{1,2}}(?:st|nd|rd|th)?)?\s+{MONTH},?\s+20(?:{yy}))", re.I),
        re.compile(rf"\b(20(?:{yy})-\d{{2}}-\d{{2}})\b"),
        re.compile(rf"\b(\d{{1,2}}/\d{{1,2}}/20(?:{yy}))\b"),
    ]


def _needs_hint(c: dict, t: str) -> bool:
    if c.get("archived") or not c.get("website"):
        return False
    future = [e for e in c.get("editions", []) if e.get("start_date") and e["start_date"] >= t]
    if not future:
        return True
    return any((e.get("dates_status") or "announced") in SOFT for e in future)


_MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}


def hint_date(text: str) -> date | None:
    """First day of a date-looking string ('Oct 7-9, 2026', '17-18 September 2026', '2026-10-07', '10/7/2026')."""
    t = text.lower().replace("sept ", "sep ")
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})$", t)
    if m:
        y, mo, d = map(int, m.groups())
    else:
        m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})$", t)
        if m:
            mo, d, y = map(int, m.groups())
        else:
            m = re.match(r"([a-z]{3})[a-z]*\.?\s+(\d{1,2})", t) or None
            m2 = re.match(r"(\d{1,2})(?:st|nd|rd|th)?(?:\s*[-–—&]\s*\d{1,2}(?:st|nd|rd|th)?)?\s+([a-z]{3})", t)
            ym = re.search(r"(20\d\d)", t)
            if m and ym:
                mo, d, y = _MONTHS.get(m.group(1), 0), int(m.group(2)), int(ym.group(1))
            elif m2 and ym:
                d, mo, y = int(m2.group(1)), _MONTHS.get(m2.group(2), 0), int(ym.group(1))
            else:
                return None
    try:
        return date(y, mo, d)
    except ValueError:
        return None


def _hint_one(c: dict, years: list[int], timeout: int, today: date | None = None) -> list[dict]:
    text = _html_text(_get(c["website"], headers={"Accept": "text/html,*/*"}, timeout=timeout))
    hints, seen = [], set()
    floor = (today or date.today()) - timedelta(days=3)
    for rx in _date_regexes(years):
        for m in rx.finditer(text):
            d = re.sub(r"\s+", " ", m.group(1)).strip()
            k = d.lower()
            if k in seen:
                continue
            hd = hint_date(d)
            if hd and hd < floor:
                continue
            seen.add(k)
            a, b = max(0, m.start() - 60), min(len(text), m.end() + 60)
            hints.append({"date": d, "ctx": text[a:b].strip()})
            if len(hints) >= 8:
                return hints
    return hints


def scan_date_hints(confs: dict[str, dict], today: date, changes: list, workers: int = 8, timeout: int = 20,
                    only: set[str] | None = None) -> dict:
    """Read each unconfirmed conference's homepage and keep the date-looking strings it mentions."""
    t = today.isoformat()
    years = [today.year, today.year + 1, today.year + 2]
    targets = [c for s, c in confs.items() if (only is None or s in only) and _needs_hint(c, t)]
    n_ok = n_fail = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_hint_one, c, years, timeout, today): c for c in targets}
        for fut in concurrent.futures.as_completed(futs):
            c = futs[fut]
            try:
                hints = fut.result()
                n_ok += 1
            except Exception:  # noqa: BLE001
                n_fail += 1
                continue
            old = c.get("date_hints") or []
            if [h["date"] for h in old] != [h["date"] for h in hints]:
                _setf(changes, "conference_field", c["slug"], c, "date_hints", hints, source="website")
            c["hints_checked"] = t
    return {"scanned": n_ok, "failed": n_fail, "targets": len(targets)}


# ---------------------------------------------------------------- seed <-> dicts

def strip_personal(confs: dict[str, dict]) -> None:
    for c in confs.values():
        for k in list(c):
            if k in PERSONAL_CONF or k.startswith("_"):
                del c[k]
        for e in c.get("editions", []):
            for k in list(e):
                if k in PERSONAL_EDITION or k.startswith("_"):
                    del e[k]
            for d in e.get("deadlines", []):
                for k in list(d):
                    if k.startswith("_"):
                        del d[k]
                if d.get("status") in USER_DEADLINE_STATUS:
                    d["status"] = "open"


def normalize(confs: dict[str, dict]) -> None:
    """Tidy a seed-style dict: statuses, years, duplicate editions, empty deadlines."""
    for c in confs.values():
        eds = sorted(c.get("editions", []), key=lambda e: int(e["year"]))
        for e in eds:
            e.setdefault("deadlines", [])
            e.setdefault("dates_status", "announced" if e.get("start_date") else "unknown")
            if e.get("start_date") and int(e["year"]) != int(e["start_date"][:4]):
                e["year"] = int(e["start_date"][:4])
        by_year: dict[int, dict] = {}
        for e in eds:
            y = int(e["year"])
            if y in by_year:
                old = by_year[y]
                for k, v in e.items():
                    if k in ("deadlines", "outreach"):
                        old[k] = old.get(k, []) + v
                    elif v not in (None, "", []) and old.get(k) in (None, "", []):
                        old[k] = v
            else:
                by_year[y] = e
        c["editions"] = [by_year[y] for y in sorted(by_year)]
        for e in c["editions"]:
            e["deadlines"] = [d for d in e.get("deadlines", []) if d.get("due_date") or d.get("status") not in (None, "open")]
        c["aliases"] = sorted(set(a for a in c.get("aliases", []) if a and a != c.get("name")))
        c.setdefault("type", "hacker")
        if c["slug"].startswith("bsides"):
            c["type"] = "bsides"
        if c["slug"].startswith("maker-faire"):
            c["type"] = "maker"


def load_seed(path: Path) -> tuple[dict, dict[str, dict]]:
    data = json.loads(Path(path).read_text())
    confs = {c["slug"]: c for c in data["conferences"]}
    for c in confs.values():
        c.setdefault("editions", [])
        c.setdefault("aliases", [])
        for e in c["editions"]:
            e.setdefault("deadlines", [])
    return data, confs


def write_seed(path: Path, data: dict, confs: dict[str, dict], today: date, removed: set[str] | None = None) -> None:
    normalize(confs)
    strip_personal(confs)
    data["generated"] = today.isoformat()
    if removed is not None:
        data["removed"] = sorted(removed)
    data["conferences"] = sorted(confs.values(), key=lambda c: (c.get("name") or "").lower())
    Path(path).write_text(json.dumps(data, indent=1, default=str, ensure_ascii=False))


# ---------------------------------------------------------------- database bridge

def load_from_db(con) -> dict[str, dict]:
    confs, by_cid, by_eid = {}, {}, {}
    for c in dbm.rows(con, "SELECT * FROM conferences"):
        c["_id"] = c.pop("id")
        c["aliases"] = dbm.jloads(c.get("aliases")) or []
        c["date_hints"] = dbm.jloads(c.get("date_hints")) or []
        c["editions"] = []
        confs[c["slug"]] = c
        by_cid[c["_id"]] = c
    for e in dbm.rows(con, "SELECT * FROM editions ORDER BY year"):
        e["_id"] = e.pop("id")
        e["deadlines"] = []
        if e["conference_id"] in by_cid:
            by_cid[e["conference_id"]]["editions"].append(e)
            by_eid[e["_id"]] = e
    for d in dbm.rows(con, "SELECT * FROM deadlines ORDER BY id"):
        d["_id"] = d.pop("id")
        if d["edition_id"] in by_eid:
            by_eid[d["edition_id"]]["deadlines"].append(d)
    return confs


def _ser(field, v):
    if field in JSON_FIELDS:
        return json.dumps(v if v is not None else [])
    return v


def write_changes_to_db(con, changes: list) -> None:
    for ch in changes:
        k, obj = ch["kind"], ch["obj"]
        if k == "conference_new":
            fields = {kk: _ser(kk, vv) for kk, vv in obj.items() if kk in CONF_COLS}
            fields["aliases"] = json.dumps(sorted(set(obj.get("aliases") or [])))
            obj["_id"] = dbm.insert_row(con, "conferences", fields)
        elif k == "conference_field":
            if ch["field"] in CONF_COLS:
                dbm.update_row(con, "conferences", obj["_id"], {ch["field"]: _ser(ch["field"], ch["new"])})
        elif k == "edition_new":
            fields = {kk: vv for kk, vv in obj.items() if kk in EDITION_COLS}
            obj["_id"] = dbm.insert_row(con, "editions", {"conference_id": ch["parent"]["_id"], **fields})
        elif k == "edition_field":
            if ch["field"] in EDITION_COLS:
                dbm.update_row(con, "editions", obj["_id"], {ch["field"]: ch["new"]})
        elif k == "deadline_new":
            fields = {kk: vv for kk, vv in obj.items() if kk in DEADLINE_COLS}
            obj["_id"] = dbm.insert_row(con, "deadlines", {"edition_id": ch["parent"]["_id"], **fields})
        elif k == "deadline_field":
            if ch["field"] in DEADLINE_COLS:
                dbm.update_row(con, "deadlines", obj["_id"], {ch["field"]: ch["new"]})


def _conf_id(ch) -> int | None:
    obj, parent = ch["obj"], ch["parent"]
    if ch["kind"].startswith("conference"):
        return obj.get("_id")
    if ch["kind"].startswith("edition"):
        return (parent or {}).get("_id")
    return None


def summarize(changes: list, errors: list[str], today: date, hints: dict | None = None) -> dict:
    n = {"new_conferences": 0, "announced": 0, "changed": 0, "postponed": 0, "estimated": 0, "deadlines": 0, "links": 0, "hints": 0}
    lines = []
    for ch in changes:
        m = ch.get("msg") or ""
        if ch["kind"] == "conference_new":
            n["new_conferences"] += 1
        elif ch["kind"] == "edition_field" and ch["field"] == "start_date" and "estimated" in m and ch["source"] == "estimate":
            n["estimated"] += 1
        elif ch["kind"] == "edition_field" and ch["field"] == "start_date" and "changed" in m:
            n["changed"] += 1
        elif ch["kind"] == "edition_field" and ch["field"] == "start_date":
            n["announced"] += 1
        elif ch["kind"] == "edition_field" and ch["field"] == "dates_status" and ch["new"] in ("postponed", "cancelled"):
            n["postponed"] += 1
        elif ch["kind"] in ("deadline_new", "deadline_field") and ch["field"] in (None, "due_date"):
            n["deadlines"] += 1
        elif ch["kind"] == "conference_field" and ch["field"] == "date_hints":
            n["hints"] += 1
        elif ch["kind"] == "conference_field":
            n["links"] += 1
        if m:
            lines.append((ch["slug"], m))
    parts = []
    for key, label in (("announced", "dates announced"), ("changed", "dates changed"), ("postponed", "postponed/cancelled"),
                       ("new_conferences", "new conferences"), ("deadlines", "deadlines"), ("estimated", "next-year estimates"),
                       ("links", "details filled in"), ("hints", "website date hints")):
        if n[key]:
            parts.append(f"{n[key]} {label}")
    text = f"Refresh {today.isoformat()}: " + (", ".join(parts) if parts else "no changes")
    if hints:
        text += f" · scanned {hints['scanned']} websites"
    if errors:
        text += " · problems: " + "; ".join(errors)
    return {"text": text, "counts": n, "errors": errors, "lines": lines, "n_changes": len(changes)}


# ---------------------------------------------------------------- entry points

def refresh_db(fetch: bool = True, cache_dir: Path | None = None, hints: bool | None = None,
               today: date | None = None) -> dict:
    """Update the live database from the feeds. Safe to call from a background thread."""
    if not _lock.acquire(blocking=False):
        return {"text": "A refresh is already running", "counts": {}, "errors": [], "lines": [], "n_changes": 0, "busy": True}
    try:
        today = today or date.today()
        cache_dir = Path(cache_dir or dbm.DATA_DIR / "cache")
        raw, errors = fetch_all(cache_dir, today, offline=not fetch, max_age_hours=0 if fetch else 1e9)
        records = parse_all(raw, today)
        with dbm.db() as con:
            settings = dbm.get_settings(con)
            blocked = set(dbm.jloads(settings.get("suppressed_slugs")) or [])
            seed_file = dbm.DATA_DIR / "seed" / "conferences.json"
            if seed_file.exists():
                m = re.search(r'"removed":\s*(\[[^\]]*\])', seed_file.read_text())
                if m:
                    blocked |= set(json.loads(m.group(1)))
            confs = load_from_db(con)
            changes: list = []
            apply_records(confs, records, today, changes, blocked=blocked)
            estimate_next_editions(confs, today, changes)
            write_changes_to_db(con, changes)
            do_hints = hints if hints is not None else (settings.get("refresh_hints", "1") == "1")
        hint_stats = None
        if do_hints:
            hint_changes: list = []
            hint_stats = scan_date_hints(confs, today, hint_changes)
            with dbm.db() as con:
                write_changes_to_db(con, hint_changes)
                for c in confs.values():
                    if c.get("hints_checked") == today.isoformat() and c.get("_id"):
                        con.execute("UPDATE conferences SET hints_checked=? WHERE id=?", (today.isoformat(), c["_id"]))
            changes += hint_changes
        summary = summarize(changes, errors, today, hint_stats)
        with dbm.db() as con:
            by_slug = {s: c.get("_id") for s, c in confs.items()}
            for slug, msg in summary["lines"][:200]:
                dbm.log_activity(con, "refresh", msg, by_slug.get(slug))
            dbm.log_activity(con, "refresh", summary["text"])
            dbm.set_setting(con, "last_refresh", datetime.now().isoformat(timespec="minutes"))
        return summary
    finally:
        _lock.release()


def apply_seed_to_db(seed_path: Path, today: date | None = None) -> dict:
    """Merge a newer bundled dataset into the live database, keeping every personal field."""
    today = today or date.today()
    data, seed_confs = load_seed(seed_path)
    with dbm.db() as con:
        settings = dbm.get_settings(con)
        blocked = set(dbm.jloads(settings.get("suppressed_slugs")) or [])
        confs = load_from_db(con)
        idx = Index(confs)
        changes: list = []
        for slug, sc in seed_confs.items():
            if slug in blocked:
                continue
            tgt = confs.get(slug)
            if not tgt:
                alt = confs.get(idx.find(sc.get("name")) or "")
                if alt and (not alt.get("country") or not sc.get("country") or alt["country"] == sc["country"]):
                    tgt = alt
            if not tgt:
                c = {k: v for k, v in sc.items() if k in CONF_COLS and k not in PERSONAL_CONF and k != "aliases"}
                c.update({"slug": slug, "aliases": list(sc.get("aliases") or []), "editions": []})
                confs[slug] = c
                idx.add(c)
                _chg(changes, "conference_new", slug, c, source="dataset", msg="Added from the bundled dataset")
                tgt = c
            else:
                for k, v in sc.items():
                    if k in CONF_COLS and k not in PERSONAL_CONF and k not in ("slug", "aliases", "source", "last_verified", "date_hints", "hints_checked"):
                        _fill(changes, tgt, k, v, "dataset")
                merged = sorted(set(tgt.get("aliases") or []) | set(sc.get("aliases") or []))
                if merged != sorted(set(tgt.get("aliases") or [])):
                    _setf(changes, "conference_field", slug, tgt, "aliases", merged)
                if sc.get("verification_notes") and sc["verification_notes"] != tgt.get("verification_notes"):
                    _setf(changes, "conference_field", slug, tgt, "verification_notes", sc["verification_notes"])
                if sc.get("hints_checked") and sc["hints_checked"] > (tgt.get("hints_checked") or ""):
                    if [h.get("date") for h in (sc.get("date_hints") or [])] != [h.get("date") for h in (tgt.get("date_hints") or [])]:
                        _setf(changes, "conference_field", slug, tgt, "date_hints", sc.get("date_hints") or [])
                    _setf(changes, "conference_field", slug, tgt, "hints_checked", sc["hints_checked"])
            for se in sc.get("editions", []):
                e = get_edition(tgt, int(se["year"]), changes, se.get("source") or "dataset")
                st = se.get("dates_status") or ("announced" if se.get("start_date") else "unknown")
                if st in ("postponed", "cancelled"):
                    if se.get("start_date") and not e.get("start_date"):
                        _setf(changes, "edition_field", slug, e, "start_date", se["start_date"], e["year"], tgt)
                        _setf(changes, "edition_field", slug, e, "end_date", se.get("end_date"), e["year"], tgt)
                    if e.get("dates_status") not in ("confirmed", st):
                        _setf(changes, "edition_field", slug, e, "dates_status", st, e["year"], tgt, "dataset", f"{e['year']}: {st} (dataset)")
                elif se.get("start_date"):
                    # the dataset fills blanks and upgrades estimates; only a "confirmed" fact moves an announced date
                    # (feed-driven moves reach the database through the app's own refresh)
                    merge_dates(tgt, e, se["start_date"], se.get("end_date"), st, se.get("source") or "dataset",
                                st == "confirmed", today, changes)
                for k in ("label", "venue", "city", "url", "training_start", "training_end", "attendance_actual"):
                    if se.get(k) and not e.get(k):
                        _setf(changes, "edition_field", slug, e, k, se[k], e["year"], tgt)
                if se.get("notes") and not e.get("notes"):
                    _setf(changes, "edition_field", slug, e, "notes", se["notes"], e["year"], tgt)
                for d in se.get("deadlines", []):
                    if d.get("due_date"):
                        merge_deadline(tgt, e, d["kind"], d["due_date"], d.get("url"), "dataset", today, changes, notes=d.get("notes"))
            for ct in sc.get("contacts", []) or []:
                if ct.get("email") and tgt.get("_id") and not dbm.row(con, "SELECT id FROM contacts WHERE conference_id=? AND email=?", (tgt["_id"], ct["email"])):
                    dbm.insert_row(con, "contacts", {"conference_id": tgt["_id"], **{k: ct.get(k) for k in ("name", "role", "email", "phone", "notes", "source")}})
        write_changes_to_db(con, changes)
        for s in data.get("sources", []):
            if not dbm.row(con, "SELECT id FROM sources WHERE url=?", (s["url"],)):
                dbm.insert_row(con, "sources", {k: s.get(k) for k in ("name", "url", "notes")})
        for tpl in data.get("templates", []):
            if not dbm.row(con, "SELECT id FROM templates WHERE name=?", (tpl["name"],)):
                dbm.insert_row(con, "templates", {k: tpl.get(k) for k in ("name", "subject", "body")})
        summary = summarize(changes, [], today)
        summary["text"] = summary["text"].replace("Refresh", "Dataset update", 1)
        by_slug = {s: c.get("_id") for s, c in confs.items()}
        for slug, msg in summary["lines"][:200]:
            dbm.log_activity(con, "dataset", msg, by_slug.get(slug))
        dbm.log_activity(con, "dataset", summary["text"] + f" (dataset {data.get('generated')})")
        dbm.set_setting(con, "seed_applied", str(data.get("generated") or today.isoformat()))
    return summary


def refresh_seed(seed_path: Path, cache_dir: Path, fetch: bool = True, hints: bool = True, today: date | None = None,
                 blocked: set[str] | None = None, hint_workers: int = 8) -> dict:
    """Update the bundled dataset file from the feeds (what the weekly GitHub Action runs)."""
    today = today or date.today()
    data, confs = load_seed(seed_path)
    raw, errors = fetch_all(Path(cache_dir), today, offline=not fetch, max_age_hours=0 if fetch else 1e9)
    records = parse_all(raw, today)
    changes: list = []
    apply_records(confs, records, today, changes, blocked=blocked or set())
    estimate_next_editions(confs, today, changes)
    hint_stats = scan_date_hints(confs, today, changes, workers=hint_workers) if hints else None
    write_seed(seed_path, data, confs, today, removed=set(blocked or []) | set(data.get("removed") or []))
    return summarize(changes, errors, today, hint_stats)
