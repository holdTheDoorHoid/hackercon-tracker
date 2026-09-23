"""Hackercon Tracker - web app entry point."""
from __future__ import annotations

import json
import os
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Request, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import db as dbm
from . import gmail
from .exporter import export_ics, export_xlsx
from .geo import geocode, travel_estimate
import re
import threading
import time

from . import feeds
from .importer import CONF_COLS, EDITION_COLS, DEADLINE_COLS, import_seed, import_xlsx, slugify
from .scoring import compute_score, grade, attendance_number

ROOT = Path(__file__).resolve().parent
app = FastAPI(title="Hackercon Tracker")
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
templates = Jinja2Templates(directory=ROOT / "templates")

STATUS_CHOICES = ["considering", "applied", "accepted", "attending", "attended", "declined", "rejected", "skipped", "not-going"]
DEADLINE_KINDS = ["cfp", "training", "cfv", "vendor", "sponsor", "hotel", "travel", "early_bird", "custom"]
DEADLINE_STATUS = ["open", "submitted", "accepted", "rejected", "declined", "waitlist", "closed", "done", "na"]
DATES_STATUS = ["confirmed", "announced", "tentative", "estimated", "unknown", "cancelled", "postponed"]
TYPES = ["bsides", "hacker", "maker", "hardware", "industry", "academic", "camp", "online"]
KIND_LABELS = {"cfp": "CFP (talks)", "training": "Training / workshop", "cfv": "Village / vendor call", "vendor": "Vendor table",
               "sponsor": "Sponsorship", "hotel": "Hotel block", "travel": "Book travel", "early_bird": "Early-bird tickets", "custom": "Custom"}


# ---------------------------------------------------------------- helpers

def today() -> date:
    return date.today()


def fmt_date(iso: str | None, with_year: bool = True) -> str:
    if not iso:
        return ""
    try:
        d = date.fromisoformat(iso)
    except ValueError:
        return iso
    return d.strftime("%a %b %-d, %Y" if with_year else "%a %b %-d")


def fmt_range(start: str | None, end: str | None) -> str:
    if not start:
        return "dates TBD"
    s = date.fromisoformat(start)
    if not end or end == start:
        return s.strftime("%a %b %-d, %Y")
    e = date.fromisoformat(end)
    if s.month == e.month:
        return f"{s.strftime('%b %-d')}–{e.strftime('%-d, %Y')}"
    return f"{s.strftime('%b %-d')} – {e.strftime('%b %-d, %Y')}"


def days_until(iso: str | None) -> int | None:
    if not iso:
        return None
    try:
        return (date.fromisoformat(iso) - today()).days
    except ValueError:
        return None


templates.env.filters["fmt_date"] = fmt_date
templates.env.filters["fmt_range"] = fmt_range
templates.env.filters["days_until"] = days_until
templates.env.globals["grade"] = grade
templates.env.globals["KIND_LABELS"] = KIND_LABELS
templates.env.globals["STATUS_CHOICES"] = STATUS_CHOICES
templates.env.globals["DEADLINE_KINDS"] = DEADLINE_KINDS
templates.env.globals["DEADLINE_STATUS"] = DEADLINE_STATUS
templates.env.globals["DATES_STATUS"] = DATES_STATUS
templates.env.globals["TYPES"] = TYPES
templates.env.globals["today"] = today


def render(request: Request, name: str, **ctx):
    with dbm.db() as con:
        gm = gmail.status()
        ctx.setdefault("gmail", gm)
        ctx.setdefault("settings", dbm.get_settings(con))
    ctx["request"] = request
    ctx["msg"] = request.query_params.get("msg")
    ctx["err"] = request.query_params.get("err")
    return templates.TemplateResponse(request, name, ctx)


def back(url: str, msg: str | None = None, err: str | None = None):
    sep = "&" if "?" in url else "?"
    if msg:
        url = f"{url}{sep}msg={quote(msg)}"
    elif err:
        url = f"{url}{sep}err={quote(err)}"
    return RedirectResponse(url, status_code=303)


def pick_edition(editions: list[dict]) -> dict | None:
    """The edition that matters now: the next one with dates, else the latest year."""
    if not editions:
        return None
    t = today().isoformat()
    upcoming = [e for e in editions if e.get("end_date") and e["end_date"] >= t] + \
               [e for e in editions if not e.get("end_date") and e.get("start_date") and e["start_date"] >= t]
    if upcoming:
        return sorted(upcoming, key=lambda e: e["start_date"])[0]
    dated = [e for e in editions if e.get("start_date")]
    if dated:
        latest = sorted(dated, key=lambda e: e["start_date"])[-1]
        undated_later = [e for e in editions if not e.get("start_date") and e["year"] > latest["year"]]
        if undated_later:
            return sorted(undated_later, key=lambda e: e["year"])[0]
        return latest
    return sorted(editions, key=lambda e: e["year"])[-1]


def enrich(con, conf: dict, settings: dict) -> dict:
    """Attach editions, next edition, deadlines, score."""
    eds = dbm.rows(con, "SELECT * FROM editions WHERE conference_id=? ORDER BY year", (conf["id"],))
    nxt = pick_edition(eds)
    dls = dbm.rows(con, "SELECT * FROM deadlines WHERE edition_id=? ORDER BY due_date", (nxt["id"],)) if nxt else []
    sc = compute_score(conf, nxt, dls, eds, settings)
    conf = dict(conf)
    conf["editions"] = eds
    conf["next"] = nxt
    conf["deadlines"] = dls
    conf["score"] = sc
    conf["attendance_n"] = attendance_number(conf)
    return conf


def all_enriched(con, settings, include_archived=False) -> list[dict]:
    q = "SELECT * FROM conferences" + ("" if include_archived else " WHERE archived=0") + " ORDER BY name"
    return [enrich(con, c, settings) for c in dbm.rows(con, q)]


def overlaps(a: dict, b: dict, pad: int = 1) -> bool:
    if not (a.get("start_date") and b.get("start_date")):
        return False
    a_s = date.fromisoformat(a["start_date"]) - timedelta(days=pad)
    a_e = date.fromisoformat(a.get("end_date") or a["start_date"]) + timedelta(days=pad)
    b_s = date.fromisoformat(b["start_date"])
    b_e = date.fromisoformat(b.get("end_date") or b["start_date"])
    return a_s <= b_e and b_s <= a_e


def upcoming_editions(con, settings, days: int = 400, min_score: int = 0) -> list[dict]:
    t = today().isoformat()
    horizon = (today() + timedelta(days=days)).isoformat()
    rows = dbm.rows(con, """SELECT e.*, c.name AS cname, c.slug, c.type AS ctype, c.city AS ccity, c.region AS cregion, c.country AS ccountry,
                                   c.online AS conline, c.priority AS cpriority, c.date_hints AS date_hints
                            FROM editions e JOIN conferences c ON c.id=e.conference_id
                            WHERE c.archived=0 AND e.start_date IS NOT NULL AND COALESCE(e.end_date, e.start_date) >= ? AND e.start_date <= ?
                            ORDER BY e.start_date""", (t, horizon))
    out = []
    for e in rows:
        e["city"] = e.get("city") or e.get("ccity")
        e["region"], e["country"], e["online"], e["type"], e["priority"] = e["cregion"], e["ccountry"], e["conline"], e["ctype"], e["cpriority"]
        conf = dbm.row(con, "SELECT * FROM conferences WHERE id=?", (e["conference_id"],))
        eds = dbm.rows(con, "SELECT * FROM editions WHERE conference_id=?", (conf["id"],))
        dls = dbm.rows(con, "SELECT * FROM deadlines WHERE edition_id=?", (e["id"],))
        sc = compute_score(conf, e, dls, eds, settings)
        if sc["score"] < min_score:
            continue
        e = dict(e)
        e["score"] = sc
        e["deadlines"] = dls
        out.append(e)
    return out


def find_conflicts(eds: list[dict]) -> list[list[dict]]:
    """Cluster overlapping editions (union-find)."""
    live = [e for e in eds if e.get("our_status") not in ("rejected", "declined", "skipped", "not-going") and e.get("priority") != "ignore"
            and e.get("dates_status") not in ("cancelled", "postponed")]
    parent = list(range(len(live)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(live)):
        for j in range(i + 1, len(live)):
            if overlaps(live[i], live[j]):
                parent[find(i)] = find(j)
    groups: dict[int, list[dict]] = {}
    for i, e in enumerate(live):
        groups.setdefault(find(i), []).append(e)
    clusters = [sorted(g, key=lambda e: -e["score"]["score"]) for g in groups.values() if len(g) > 1]
    return sorted(clusters, key=lambda g: g[0]["start_date"])


def form_dict(form, allowed: set[str]) -> dict:
    out = {}
    for k, v in form.items():
        if k in allowed:
            v = v.strip() if isinstance(v, str) else v
            out[k] = None if v == "" else v
    return out


# ---------------------------------------------------------------- startup

SEED_PATH = dbm.DATA_DIR / "seed" / "conferences.json"


def seed_stamp() -> str:
    """The "generated" date of the bundled dataset, without parsing the whole file."""
    try:
        m = re.search(r'"generated":\s*"(\d{4}-\d\d-\d\d)"', SEED_PATH.read_text()[:400])
        return m.group(1) if m else ""
    except OSError:
        return ""


def _auto_refresh_loop():
    """Background timer: pull the public feeds every `auto_refresh_days` days."""
    time.sleep(45)
    while True:
        try:
            with dbm.db() as con:
                s = dbm.get_settings(con)
            days = int(s.get("auto_refresh_days") or 0)
            last = s.get("last_refresh") or ""
            due = days > 0 and (not last or datetime.fromisoformat(last) < datetime.now() - timedelta(days=days))
            if due:
                feeds.refresh_db()
        except Exception as ex:  # noqa: BLE001
            with dbm.db() as con:
                dbm.log_activity(con, "refresh", f"Automatic refresh failed: {ex}")
        time.sleep(6 * 3600)


@app.on_event("startup")
def _startup():
    dbm.init_db()
    with dbm.db() as con:
        n = dbm.row(con, "SELECT COUNT(*) AS n FROM conferences")["n"]
        applied = dbm.get_settings(con).get("seed_applied") or ""
    stamp = seed_stamp()
    if SEED_PATH.exists():
        if n == 0:
            import_seed(SEED_PATH)
            with dbm.db() as con:
                dbm.set_setting(con, "seed_applied", stamp)
        elif stamp and applied != stamp:
            try:
                feeds.apply_seed_to_db(SEED_PATH)   # a newer bundled dataset (after git pull): merge, keep edits
            except Exception as ex:  # noqa: BLE001
                with dbm.db() as con:
                    dbm.log_activity(con, "dataset", f"Could not apply the bundled dataset: {ex}")
    with dbm.db() as con:
        if not dbm.row(con, "SELECT 1 AS x FROM emails LIMIT 1") and not gmail.status()["connected"]:
            gmail.load_sample(con)
    threading.Thread(target=_auto_refresh_loop, daemon=True, name="auto-refresh").start()


# ---------------------------------------------------------------- dashboard

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    with dbm.db() as con:
        settings = dbm.get_settings(con)
        t = today()
        soon = (t + timedelta(days=45)).isoformat()
        deadlines = dbm.rows(con, """SELECT d.*, e.year, e.start_date, c.name AS cname, c.slug FROM deadlines d
                                     JOIN editions e ON e.id=d.edition_id JOIN conferences c ON c.id=e.conference_id
                                     WHERE c.archived=0 AND d.due_date IS NOT NULL AND d.due_date >= ? AND d.due_date <= ?
                                       AND d.status IN ('open','submitted','waitlist')
                                     ORDER BY d.due_date""", ((t - timedelta(days=3)).isoformat(), soon))
        ups = upcoming_editions(con, settings, days=120)
        unconfirmed = [e for e in ups if e.get("dates_status") in ("tentative", "estimated", "unknown")]
        for e in unconfirmed:
            e["hints"] = (dbm.jloads(e.get("date_hints")) or [])[:4]
        home_missing = not settings.get("home_lat")
        followups = dbm.rows(con, """SELECT o.*, c.name AS cname, c.slug FROM outreach o JOIN conferences c ON c.id=o.conference_id
                                     LEFT JOIN editions e ON e.id=o.edition_id
                                     WHERE o.direction='out' AND o.status IN ('sent','no-reply')
                                       AND (o.follow_up_due <= ? OR (o.follow_up_due IS NULL AND o.date <= ?))
                                       AND (e.id IS NULL OR e.start_date IS NULL OR e.start_date >= ?)
                                     ORDER BY o.date""", (t.isoformat(), (t - timedelta(days=int(settings["followup_days"]))).isoformat(), (t - timedelta(days=7)).isoformat()))
        going = [e for e in ups if e.get("our_status") in ("accepted", "attending", "applied")]
        logistics = []
        for e in going:
            d = days_until(e["start_date"])
            need = []
            if d is not None and d <= int(settings["hotel_lead_days"]) and not e.get("hotel"):
                need.append("hotel")
            tr = e["score"]["travel"]
            if d is not None and tr.get("mode") == "fly" and d <= int(settings["flight_lead_days"]) and not e.get("travel"):
                need.append("flights")
            if need:
                logistics.append((e, need))
        conflicts = find_conflicts(upcoming_editions(con, settings, days=400, min_score=35))
        activity = dbm.rows(con, "SELECT a.*, c.slug FROM activity a LEFT JOIN conferences c ON c.id=a.conference_id ORDER BY a.id DESC LIMIT 12")
        unread = dbm.row(con, "SELECT COUNT(*) AS n FROM emails WHERE is_unread=1")["n"]
        counts = {
            "total": dbm.row(con, "SELECT COUNT(*) AS n FROM conferences WHERE archived=0")["n"],
            "upcoming": len(ups),
            "going": len(going),
            "deadlines": len(deadlines),
        }
    return render(request, "dashboard.html", deadlines=deadlines, upcoming=ups[:40], unconfirmed=unconfirmed[:20], home_missing=home_missing,
                  followups=followups, logistics=logistics, conflicts=conflicts[:6], activity=activity, unread=unread, counts=counts)


# ---------------------------------------------------------------- conferences

@app.get("/conferences", response_class=HTMLResponse)
def conferences(request: Request, q: str = "", type: str = "", country: str = "", status: str = "", month: str = "",
                sort: str = "date", archived: str = "", region: str = ""):
    with dbm.db() as con:
        settings = dbm.get_settings(con)
        confs = all_enriched(con, settings, include_archived=bool(archived))
        countries = sorted({c["country"] for c in confs if c.get("country")})
    ql = q.lower()
    out = []
    for c in confs:
        if ql and ql not in (c["name"] + " " + (c.get("city") or "") + " " + " ".join(dbm.jloads(c.get("aliases")))).lower():
            continue
        if type and c.get("type") != type:
            continue
        if country and c.get("country") != country:
            continue
        if region == "us-ca" and c.get("country") not in ("US", "CA"):
            continue
        if region == "drive" and c["score"]["travel"].get("mode") not in ("drive", "online"):
            continue
        nxt = c.get("next") or {}
        if status and (nxt.get("our_status") or "considering") != status:
            continue
        if month:
            sd = nxt.get("start_date") or ""
            if not sd or int(sd[5:7]) != int(month):
                if not (c.get("typical_month") and int(c["typical_month"]) == int(month)):
                    continue
        out.append(c)
    if sort == "score":
        out.sort(key=lambda c: -c["score"]["score"])
    elif sort == "name":
        out.sort(key=lambda c: c["name"].lower())
    elif sort == "size":
        out.sort(key=lambda c: -(c.get("attendance_n") or 0))
    else:  # date: upcoming first, then undated, then past
        t = today().isoformat()

        def key(c):
            sd = (c.get("next") or {}).get("start_date")
            if sd and sd >= t:
                return (0, sd)
            if not sd:
                return (1, f"{(c.get('typical_month') or 13):02d}")
            return (2, sd)
        out.sort(key=key)
    return render(request, "conferences.html", confs=out, countries=countries, q=q, type=type, country=country,
                  status=status, month=month, sort=sort, archived=archived, region=region, total=len(confs))


@app.get("/conferences/new", response_class=HTMLResponse)
def conference_new(request: Request):
    return render(request, "conference_form.html", conf={}, is_new=True)


@app.post("/conferences/new")
async def conference_create(request: Request):
    form = await request.form()
    data = form_dict(form, CONF_COLS)
    if not data.get("name"):
        return back("/conferences/new", err="Name is required")
    data["slug"] = data.get("slug") or slugify(data["name"])
    data["aliases"] = "[]"
    if data.get("city") and not data.get("online"):
        g = geocode(data["city"], data.get("region"), data.get("country"))
        if g:
            data["lat"], data["lon"] = g
    with dbm.db() as con:
        if dbm.row(con, "SELECT id FROM conferences WHERE slug=?", (data["slug"],)):
            return back("/conferences/new", err=f"A conference with the short name '{data['slug']}' already exists")
        cid = dbm.insert_row(con, "conferences", data)
        year = form.get("year") or str(today().year)
        ed = {"conference_id": cid, "year": int(year), "start_date": form.get("start_date") or None, "end_date": form.get("end_date") or None,
              "dates_status": form.get("dates_status") or "unknown", "our_status": "considering", "source": "manual"}
        dbm.insert_row(con, "editions", ed)
        dbm.log_activity(con, "add", f"Added {data['name']}", cid)
    return back(f"/conferences/{data['slug']}", msg="Conference added")


@app.get("/conferences/{slug}", response_class=HTMLResponse)
def conference_detail(request: Request, slug: str, year: int | None = None):
    with dbm.db() as con:
        settings = dbm.get_settings(con)
        conf = dbm.row(con, "SELECT * FROM conferences WHERE slug=?", (slug,))
        if not conf:
            return HTMLResponse("Not found", status_code=404)
        c = enrich(con, conf, settings)
        eds = c["editions"]
        sel = next((e for e in eds if year and e["year"] == year), None) or c["next"]
        ed_dls = dbm.rows(con, "SELECT * FROM deadlines WHERE edition_id=? ORDER BY COALESCE(due_date,'9999'), kind", (sel["id"],)) if sel else []
        if sel and sel is not c["next"]:
            c["score"] = compute_score(conf, sel, ed_dls, eds, settings)
        contacts = dbm.rows(con, "SELECT * FROM contacts WHERE conference_id=? ORDER BY id", (conf["id"],))
        outreach = dbm.rows(con, "SELECT o.*, e.year FROM outreach o LEFT JOIN editions e ON e.id=o.edition_id WHERE o.conference_id=? ORDER BY o.date DESC", (conf["id"],))
        emails = dbm.rows(con, "SELECT * FROM emails WHERE conference_id=? ORDER BY date DESC LIMIT 60", (conf["id"],))
        threads: dict[str, dict] = {}
        for e in emails:
            t = threads.setdefault(e["thread_id"] or e["gmail_id"], {"subject": e["subject"], "count": 0, "last": e["date"], "unread": 0, "snippet": e["snippet"], "from": e["from_addr"], "id": e["thread_id"] or e["gmail_id"]})
            t["count"] += 1
            t["unread"] += e["is_unread"]
        tmpls = dbm.rows(con, "SELECT id, name FROM templates ORDER BY name")
        all_dls = dbm.rows(con, "SELECT d.*, e.year FROM deadlines d JOIN editions e ON e.id=d.edition_id WHERE e.conference_id=? ORDER BY e.year DESC, d.due_date", (conf["id"],))
        # conflicts for the selected edition
        conflicts = []
        if sel and sel.get("start_date") and sel["start_date"] >= (today() - timedelta(days=1)).isoformat():
            for e in upcoming_editions(con, settings, days=400):
                if e["conference_id"] != conf["id"] and overlaps(sel, e):
                    conflicts.append(e)
        activity = dbm.rows(con, "SELECT * FROM activity WHERE conference_id=? ORDER BY id DESC LIMIT 10", (conf["id"],))
    c["hints"] = (dbm.jloads(c.get("date_hints")) or [])[:6]
    return render(request, "conference_detail.html", c=c, sel=sel, ed_dls=ed_dls, contacts=contacts, outreach=outreach,
                  threads=list(threads.values()), tmpls=tmpls, all_dls=all_dls, conflicts=conflicts, activity=activity,
                  travel=c["score"]["travel"], aliases=dbm.jloads(c.get("aliases")))


@app.post("/conferences/{slug}/edit")
async def conference_edit(request: Request, slug: str):
    form = await request.form()
    data = form_dict(form, CONF_COLS - {"slug", "aliases"})
    for k in ("iot_village_partner", "online", "archived"):
        if k in form or k + "_present" in form:
            data[k] = 1 if form.get(k) in ("1", "on", "true") else 0
    with dbm.db() as con:
        conf = dbm.row(con, "SELECT * FROM conferences WHERE slug=?", (slug,))
        if not conf:
            return HTMLResponse("Not found", status_code=404)
        loc_changed = any(data.get(k) != conf.get(k) for k in ("city", "region", "country") if k in data)
        if loc_changed and data.get("city") and not data.get("online"):
            g = geocode(data["city"], data.get("region", conf.get("region")), data.get("country", conf.get("country")))
            if g:
                data["lat"], data["lon"] = g
        if "lat" in form and form.get("lat"):
            data["lat"], data["lon"] = float(form["lat"]), float(form["lon"])
        dbm.update_row(con, "conferences", conf["id"], data)
        dbm.log_activity(con, "edit", "Updated details", conf["id"])
    return back(f"/conferences/{slug}", msg="Saved")


@app.post("/conferences/{slug}/delete")
def conference_delete(slug: str):
    with dbm.db() as con:
        conf = dbm.row(con, "SELECT * FROM conferences WHERE slug=?", (slug,))
        if conf:
            con.execute("DELETE FROM conferences WHERE id=?", (conf["id"],))
            sup = set(dbm.jloads(dbm.get_settings(con).get("suppressed_slugs")) or [])
            sup.add(slug)
            dbm.set_setting(con, "suppressed_slugs", json.dumps(sorted(sup)))
            dbm.log_activity(con, "delete", f"Deleted {conf['name']} (updates will not bring it back; see Settings)")
    return back("/conferences", msg="Deleted")


@app.post("/conferences/{slug}/editions")
async def edition_add(request: Request, slug: str):
    form = await request.form()
    with dbm.db() as con:
        conf = dbm.row(con, "SELECT * FROM conferences WHERE slug=?", (slug,))
        year = int(form.get("year") or today().year)
        if dbm.row(con, "SELECT id FROM editions WHERE conference_id=? AND year=?", (conf["id"], year)):
            return back(f"/conferences/{slug}?year={year}", err=f"{year} already exists")
        # copy last edition's structure as a starting point (+1 year, tentative)
        prev = dbm.row(con, "SELECT * FROM editions WHERE conference_id=? AND year<? ORDER BY year DESC LIMIT 1", (conf["id"], year))
        ed = {"conference_id": conf["id"], "year": year, "our_status": "considering", "source": "manual", "dates_status": "unknown"}
        if prev and prev.get("start_date") and form.get("estimate") == "1":
            s, e = shift_year(prev["start_date"], prev.get("end_date"), year - prev["year"])
            ed.update({"start_date": s, "end_date": e, "dates_status": "estimated",
                       "notes": f"Dates estimated from {prev['year']} (same weekend)"})
        eid = dbm.insert_row(con, "editions", ed)
        if prev and form.get("estimate") == "1":
            for d in dbm.rows(con, "SELECT * FROM deadlines WHERE edition_id=? AND due_date IS NOT NULL", (prev["id"],)):
                nd, _ = shift_year(d["due_date"], None, year - prev["year"])
                dbm.insert_row(con, "deadlines", {"edition_id": eid, "kind": d["kind"], "label": d.get("label"), "due_date": nd,
                                                   "status": "open", "url": d.get("url"), "notes": "Estimated from last year"})
        dbm.log_activity(con, "edition", f"Added {year} edition", conf["id"])
    return back(f"/conferences/{slug}?year={year}", msg=f"{year} added")


def shift_year(start: str, end: str | None, years: int) -> tuple[str, str | None]:
    """Same weekday-of-month next year: keep the Nth weekday of the month."""
    s = date.fromisoformat(start)
    n = (s.day - 1) // 7 + 1
    target_month = date(s.year + years, s.month, 1)
    first_wd = target_month.weekday()
    delta = (s.weekday() - first_wd) % 7
    day = 1 + delta + (n - 1) * 7
    try:
        ns = date(target_month.year, target_month.month, day)
    except ValueError:
        ns = date(target_month.year, target_month.month, day - 7)
    ne = None
    if end:
        ne = (ns + (date.fromisoformat(end) - s)).isoformat()
    return ns.isoformat(), ne


@app.post("/editions/{eid}/edit")
async def edition_edit(request: Request, eid: int):
    form = await request.form()
    data = form_dict(form, EDITION_COLS - {"year"})
    for k in ("worth_it", "leads", "attendance_actual"):
        if data.get(k) is not None:
            try:
                data[k] = int(data[k])
            except ValueError:
                data[k] = None
    with dbm.db() as con:
        ed = dbm.row(con, "SELECT e.*, c.slug FROM editions e JOIN conferences c ON c.id=e.conference_id WHERE e.id=?", (eid,))
        if not ed:
            return HTMLResponse("Not found", status_code=404)
        if data.get("start_date") and data.get("end_date") and data["end_date"] < data["start_date"]:
            data["end_date"] = data["start_date"]
        dbm.update_row(con, "editions", eid, data)
        if "our_status" in data and data["our_status"] != ed["our_status"]:
            dbm.log_activity(con, "status", f"{ed['year']}: status → {data['our_status']}", ed["conference_id"])
        elif "start_date" in data and data["start_date"] != ed["start_date"]:
            dbm.log_activity(con, "dates", f"{ed['year']}: dates set to {data.get('start_date')}", ed["conference_id"])
    return back(f"/conferences/{ed['slug']}?year={ed['year']}", msg="Saved")


@app.post("/editions/{eid}/delete")
def edition_delete(eid: int):
    with dbm.db() as con:
        ed = dbm.row(con, "SELECT e.*, c.slug FROM editions e JOIN conferences c ON c.id=e.conference_id WHERE e.id=?", (eid,))
        if ed:
            con.execute("DELETE FROM editions WHERE id=?", (eid,))
    return back(f"/conferences/{ed['slug']}" if ed else "/conferences", msg="Edition removed")


@app.post("/editions/{eid}/deadlines")
async def deadline_add(request: Request, eid: int):
    form = await request.form()
    data = form_dict(form, DEADLINE_COLS)
    data.setdefault("kind", "custom")
    data.setdefault("status", "open")
    with dbm.db() as con:
        ed = dbm.row(con, "SELECT e.*, c.slug FROM editions e JOIN conferences c ON c.id=e.conference_id WHERE e.id=?", (eid,))
        dbm.insert_row(con, "deadlines", {"edition_id": eid, **data})
        dbm.log_activity(con, "deadline", f"{ed['year']}: added {KIND_LABELS.get(data['kind'], data['kind'])} deadline {data.get('due_date') or ''}", ed["conference_id"])
    return back(f"/conferences/{ed['slug']}?year={ed['year']}", msg="Deadline added")


@app.post("/deadlines/{did}/edit")
async def deadline_edit(request: Request, did: int):
    form = await request.form()
    data = form_dict(form, DEADLINE_COLS)
    with dbm.db() as con:
        d = dbm.row(con, "SELECT d.*, e.year, c.slug, c.id AS cid FROM deadlines d JOIN editions e ON e.id=d.edition_id JOIN conferences c ON c.id=e.conference_id WHERE d.id=?", (did,))
        if not d:
            return HTMLResponse("Not found", status_code=404)
        dbm.update_row(con, "deadlines", did, data)
        if data.get("status") and data["status"] != d["status"]:
            dbm.log_activity(con, "deadline", f"{d['year']}: {KIND_LABELS.get(d['kind'], d['kind'])} → {data['status']}", d["cid"])
            # keep the edition's overall status in step with big milestones
            if data["status"] == "accepted" and d["kind"] in ("cfp", "training", "cfv", "vendor", "sponsor"):
                con.execute("UPDATE editions SET our_status='accepted' WHERE id=? AND our_status IN ('considering','applied')", (d["edition_id"],))
            elif data["status"] == "submitted":
                con.execute("UPDATE editions SET our_status='applied' WHERE id=? AND our_status='considering'", (d["edition_id"],))
    if request.headers.get("accept", "").startswith("application/json"):
        return JSONResponse({"ok": True})
    return back(f"/conferences/{d['slug']}?year={d['year']}", msg="Saved")


@app.post("/deadlines/{did}/delete")
def deadline_delete(did: int):
    with dbm.db() as con:
        d = dbm.row(con, "SELECT d.*, e.year, c.slug FROM deadlines d JOIN editions e ON e.id=d.edition_id JOIN conferences c ON c.id=e.conference_id WHERE d.id=?", (did,))
        con.execute("DELETE FROM deadlines WHERE id=?", (did,))
    return back(f"/conferences/{d['slug']}?year={d['year']}" if d else "/", msg="Deadline removed")


@app.post("/conferences/{slug}/contacts")
async def contact_add(request: Request, slug: str):
    form = await request.form()
    with dbm.db() as con:
        conf = dbm.row(con, "SELECT id FROM conferences WHERE slug=?", (slug,))
        dbm.insert_row(con, "contacts", {"conference_id": conf["id"], "name": form.get("name") or None, "role": form.get("role") or None,
                                         "email": form.get("email") or None, "phone": form.get("phone") or None, "notes": form.get("notes") or None, "source": "manual"})
        gmail.rematch(con)
    return back(f"/conferences/{slug}", msg="Contact added")


@app.post("/contacts/{cid}/delete")
def contact_delete(cid: int):
    with dbm.db() as con:
        ct = dbm.row(con, "SELECT ct.*, c.slug FROM contacts ct JOIN conferences c ON c.id=ct.conference_id WHERE ct.id=?", (cid,))
        con.execute("DELETE FROM contacts WHERE id=?", (cid,))
    return back(f"/conferences/{ct['slug']}" if ct else "/", msg="Contact removed")


@app.post("/conferences/{slug}/outreach")
async def outreach_add(request: Request, slug: str):
    form = await request.form()
    with dbm.db() as con:
        settings = dbm.get_settings(con)
        conf = dbm.row(con, "SELECT id FROM conferences WHERE slug=?", (slug,))
        d = form.get("date") or today().isoformat()
        fu = form.get("follow_up_due") or None
        if not fu and form.get("direction", "out") == "out":
            fu = (date.fromisoformat(d) + timedelta(days=int(settings["followup_days"]))).isoformat()
        eid = form.get("edition_id") or None
        dbm.insert_row(con, "outreach", {"conference_id": conf["id"], "edition_id": int(eid) if eid else None, "date": d,
                                         "channel": form.get("channel") or "gmail", "direction": form.get("direction") or "out",
                                         "subject": form.get("subject") or None, "summary": form.get("summary") or None,
                                         "status": form.get("status") or "sent", "follow_up_due": fu})
        dbm.log_activity(con, "outreach", f"Logged {form.get('channel') or 'gmail'} {form.get('direction') or 'out'}: {form.get('summary') or form.get('subject') or ''}", conf["id"])
    return back(f"/conferences/{slug}", msg="Logged")


@app.post("/outreach/{oid}/edit")
async def outreach_edit(request: Request, oid: int):
    form = await request.form()
    data = form_dict(form, {"date", "channel", "direction", "subject", "summary", "status", "follow_up_due"})
    with dbm.db() as con:
        o = dbm.row(con, "SELECT o.*, c.slug FROM outreach o JOIN conferences c ON c.id=o.conference_id WHERE o.id=?", (oid,))
        dbm.update_row(con, "outreach", oid, data)
    return back(f"/conferences/{o['slug']}" if o else "/", msg="Saved")


@app.post("/outreach/{oid}/delete")
def outreach_delete(oid: int):
    with dbm.db() as con:
        o = dbm.row(con, "SELECT o.*, c.slug FROM outreach o JOIN conferences c ON c.id=o.conference_id WHERE o.id=?", (oid,))
        con.execute("DELETE FROM outreach WHERE id=?", (oid,))
    return back(f"/conferences/{o['slug']}" if o else "/", msg="Removed")


# ---------------------------------------------------------------- calendar & conflicts

@app.get("/calendar", response_class=HTMLResponse)
def calendar_page(request: Request):
    return render(request, "calendar.html")


COLORS = {"accepted": "#2e7d32", "attending": "#2e7d32", "attended": "#5d6d5e", "applied": "#1565c0", "considering": "#6d5aa8",
          "declined": "#9e9e9e", "rejected": "#9e9e9e", "skipped": "#9e9e9e", "not-going": "#9e9e9e"}


@app.get("/api/calendar.json")
def calendar_json(start: str = "", end: str = "", deadlines: str = "1", min_score: int = 0, mine: str = ""):
    s = start[:10] if start else (today() - timedelta(days=60)).isoformat()
    e = end[:10] if end else (today() + timedelta(days=400)).isoformat()
    out = []
    with dbm.db() as con:
        settings = dbm.get_settings(con)
        rows = dbm.rows(con, """SELECT e.*, c.name AS cname, c.slug, c.city AS ccity, c.region AS cregion, c.country AS ccountry, c.type AS ctype FROM editions e JOIN conferences c ON c.id=e.conference_id
                                WHERE c.archived=0 AND e.start_date IS NOT NULL AND COALESCE(e.end_date, e.start_date) >= ? AND e.start_date <= ?""", (s, e))
        for r in rows:
            r["city"] = r.get("city") or r.get("ccity")
            r["region"], r["country"], r["type"] = r["cregion"], r["ccountry"], r["ctype"]
            if mine and r.get("our_status") not in ("applied", "accepted", "attending", "attended"):
                continue
            conf = dbm.row(con, "SELECT * FROM conferences WHERE id=?", (r["conference_id"],))
            dls = dbm.rows(con, "SELECT * FROM deadlines WHERE edition_id=?", (r["id"],))
            eds = dbm.rows(con, "SELECT * FROM editions WHERE conference_id=?", (conf["id"],))
            sc = compute_score(conf, r, dls, eds, settings)
            if sc["score"] < min_score and r.get("our_status") not in ("applied", "accepted", "attending"):
                continue
            end_excl = (date.fromisoformat(r.get("end_date") or r["start_date"]) + timedelta(days=1)).isoformat()
            title = r["cname"]
            if r.get("dates_status") in ("tentative", "estimated"):
                title += " (tent.)"
            color = COLORS.get(r.get("our_status") or "considering", "#6d5aa8")
            if r.get("dates_status") in ("cancelled", "postponed"):
                color = "#b71c1c"
            out.append({"id": f"e{r['id']}", "title": f"{title} · {sc['score']}", "start": r["start_date"], "end": end_excl, "allDay": True,
                        "url": f"/conferences/{r['slug']}?year={r['year']}", "color": color,
                        "extendedProps": {"kind": "event", "score": sc["score"], "status": r.get("our_status"), "dates_status": r.get("dates_status"),
                                          "place": ", ".join(p for p in (r.get("city"), r.get("region")) if p), "type": r.get("type")}})
            if deadlines == "1":
                for d in dls:
                    if d.get("due_date") and s <= d["due_date"] <= e and d.get("status") in ("open", "submitted", "waitlist"):
                        out.append({"id": f"d{d['id']}", "title": f"⏰ {KIND_LABELS.get(d['kind'], d['kind']).split(' ')[0]}: {r['cname']}", "start": d["due_date"], "allDay": True,
                                    "url": f"/conferences/{r['slug']}?year={r['year']}", "color": "#e65100", "textColor": "#fff",
                                    "extendedProps": {"kind": "deadline", "status": d.get("status")}})
    return JSONResponse(out)


@app.get("/conflicts", response_class=HTMLResponse)
def conflicts_page(request: Request, min_score: int = 35):
    with dbm.db() as con:
        settings = dbm.get_settings(con)
        clusters = find_conflicts(upcoming_editions(con, settings, days=420, min_score=min_score))
    return render(request, "conflicts.html", clusters=clusters, min_score=min_score)


@app.get("/compare", response_class=HTMLResponse)
def compare(request: Request, date_: str = "", slugs: str = ""):
    d = request.query_params.get("date") or date_
    with dbm.db() as con:
        settings = dbm.get_settings(con)
        eds = upcoming_editions(con, settings, days=420)
        if d:
            centre = date.fromisoformat(d)
            probe = {"start_date": (centre - timedelta(days=2)).isoformat(), "end_date": (centre + timedelta(days=2)).isoformat()}
            eds = [e for e in eds if overlaps(probe, e, pad=0)]
        elif slugs:
            want = set(slugs.split(","))
            eds = [e for e in eds if e["slug"] in want]
        eds.sort(key=lambda e: -e["score"]["score"])
    return render(request, "compare.html", eds=eds, d=d)


# ---------------------------------------------------------------- email

@app.get("/email", response_class=HTMLResponse)
def email_page(request: Request, view: str = "all", q: str = ""):
    with dbm.db() as con:
        where = "1=1"
        params: list = []
        if view == "unassigned":
            where = "e.conference_id IS NULL"
        elif view == "unread":
            where = "e.is_unread=1"
        elif view == "sent":
            where = "e.is_sent=1"
        if q:
            where += " AND (e.subject LIKE ? OR e.from_addr LIKE ? OR e.snippet LIKE ?)"
            params += [f"%{q}%"] * 3
        emails = dbm.rows(con, f"""SELECT e.*, c.name AS cname, c.slug FROM emails e LEFT JOIN conferences c ON c.id=e.conference_id
                                   WHERE {where} ORDER BY e.date DESC LIMIT 400""", params)
        threads: dict[str, dict] = {}
        for e in emails:
            k = e["thread_id"] or e["gmail_id"]
            t = threads.setdefault(k, {"id": k, "subject": e["subject"], "count": 0, "last": e["date"], "unread": 0, "snippet": e["snippet"],
                                       "from": e["from_addr"], "cname": e["cname"], "slug": e["slug"], "conference_id": e["conference_id"], "email_id": e["id"]})
            t["count"] += 1
            t["unread"] += e["is_unread"]
        confs = dbm.rows(con, "SELECT id, name, slug FROM conferences WHERE archived=0 ORDER BY name")
        counts = {
            "all": dbm.row(con, "SELECT COUNT(DISTINCT COALESCE(thread_id, gmail_id)) AS n FROM emails")["n"],
            "unassigned": dbm.row(con, "SELECT COUNT(DISTINCT COALESCE(thread_id, gmail_id)) AS n FROM emails WHERE conference_id IS NULL")["n"],
            "unread": dbm.row(con, "SELECT COUNT(*) AS n FROM emails WHERE is_unread=1")["n"],
        }
    return render(request, "email.html", threads=list(threads.values()), confs=confs, view=view, q=q, counts=counts)


@app.get("/email/thread/{thread_id}", response_class=HTMLResponse)
def email_thread(request: Request, thread_id: str):
    with dbm.db() as con:
        msgs = dbm.rows(con, "SELECT e.*, c.name AS cname, c.slug FROM emails e LEFT JOIN conferences c ON c.id=e.conference_id WHERE e.thread_id=? OR e.gmail_id=? ORDER BY e.date", (thread_id, thread_id))
        confs = dbm.rows(con, "SELECT id, name, slug FROM conferences WHERE archived=0 ORDER BY name")
        live = []
        if gmail.status()["connected"]:
            try:
                live = gmail.get_thread(thread_id)
                con.execute("UPDATE emails SET is_unread=0 WHERE thread_id=?", (thread_id,))
            except Exception as ex:  # fall back to the cache
                live = []
                request.state.err = str(ex)
        tmpls = dbm.rows(con, "SELECT id, name FROM templates ORDER BY name")
    return render(request, "email_thread.html", msgs=msgs, live=live, thread_id=thread_id, confs=confs, tmpls=tmpls,
                  conf=(msgs[0] if msgs else None))


@app.post("/email/assign")
async def email_assign(request: Request):
    form = await request.form()
    thread_id = form.get("thread_id")
    cid = form.get("conference_id") or None
    with dbm.db() as con:
        con.execute("UPDATE emails SET conference_id=?, match_reason='manual' WHERE thread_id=? OR gmail_id=?", (int(cid) if cid else None, thread_id, thread_id))
        if cid and gmail.status()["connected"] and form.get("label") == "1":
            conf = dbm.row(con, "SELECT name FROM conferences WHERE id=?", (int(cid),))
            try:
                gmail.label_thread(gmail._build(), thread_id, f"{dbm.get_settings(con)['gmail_label_prefix']}/{conf['name']}")
            except Exception:
                pass
    return back(form.get("next") or "/email", msg="Assigned")


@app.post("/email/sync")
def email_sync(request: Request):
    with dbm.db() as con:
        try:
            r = gmail.sync(con)
        except Exception as ex:
            return back("/email", err=f"Sync failed: {ex}")
    if r.get("mode") == "sample":
        return back("/email", msg="Gmail isn't connected yet, so this is sample data. See Settings → Gmail.")
    return back("/email", msg=f"Synced: {r['new']} new messages, {r['matched']} matched to conferences")


@app.get("/email/compose", response_class=HTMLResponse)
def compose(request: Request, slug: str = "", template: str = "", to: str = "", thread: str = "", subject: str = "", reply_to: str = ""):
    with dbm.db() as con:
        settings = dbm.get_settings(con)
        conf = dbm.row(con, "SELECT * FROM conferences WHERE slug=?", (slug,)) if slug else None
        contacts = dbm.rows(con, "SELECT * FROM contacts WHERE conference_id=?", (conf["id"],)) if conf else []
        tmpls = dbm.rows(con, "SELECT * FROM templates ORDER BY name")
        t = next((x for x in tmpls if str(x["id"]) == template or x["name"] == template), None)
        nxt = None
        if conf:
            nxt = pick_edition(dbm.rows(con, "SELECT * FROM editions WHERE conference_id=? ORDER BY year", (conf["id"],)))
        body = t["body"] if t else ""
        subj = subject or (t["subject"] if t else "")
        ctx = {"conference": conf["name"] if conf else "", "city": (conf or {}).get("city") or "", "year": str((nxt or {}).get("year") or today().year),
               "dates": fmt_range((nxt or {}).get("start_date"), (nxt or {}).get("end_date")) if nxt else "", "org": settings.get("org_name", ""),
               "me": settings.get("owner_name", "")}
        for k, v in ctx.items():
            body = body.replace("{{" + k + "}}", v)
            subj = subj.replace("{{" + k + "}}", v)
        if not to and conf:
            to = conf.get("sponsor_email") or conf.get("contact_email") or (contacts[0]["email"] if contacts else "")
        confs = dbm.rows(con, "SELECT slug, name FROM conferences WHERE archived=0 ORDER BY name")
    return render(request, "compose.html", conf=conf, contacts=contacts, tmpls=tmpls, body=body, subject=subj, to=to, thread=thread,
                  reply_to=reply_to, confs=confs, slug=slug)


@app.post("/email/send")
async def email_send(request: Request):
    form = await request.form()
    to, subject, body = form.get("to", "").strip(), form.get("subject", "").strip(), form.get("body", "")
    slug = form.get("slug") or ""
    thread = form.get("thread") or None
    reply_to = form.get("reply_to") or None
    action = form.get("action", "send")
    if not to or not subject:
        return back(f"/email/compose?slug={slug}", err="Recipient and subject are required")
    with dbm.db() as con:
        settings = dbm.get_settings(con)
        conf = dbm.row(con, "SELECT * FROM conferences WHERE slug=?", (slug,)) if slug else None
        if not gmail.status()["connected"]:
            return back(f"/email/compose?slug={slug}", err="Gmail is not connected. Go to Settings → Gmail to connect, or copy the text into Gmail by hand.")
        try:
            label = f"{settings['gmail_label_prefix']}/{conf['name']}" if conf else None
            if action == "draft":
                gmail.create_draft(to, subject, body, thread_id=thread)
                dbm.log_activity(con, "email", f"Saved Gmail draft to {to}: {subject}", conf["id"] if conf else None)
                return back(f"/conferences/{slug}" if slug else "/email", msg="Draft saved in Gmail (Drafts folder)")
            sent = gmail.send(to, subject, body, thread_id=thread, in_reply_to=reply_to, label=label, cc=form.get("cc") or None)
        except Exception as ex:
            return back(f"/email/compose?slug={slug}", err=f"Send failed: {ex}")
        if conf:
            fu = (today() + timedelta(days=int(settings["followup_days"]))).isoformat()
            nxt = pick_edition(dbm.rows(con, "SELECT * FROM editions WHERE conference_id=? ORDER BY year", (conf["id"],)))
            dbm.insert_row(con, "outreach", {"conference_id": conf["id"], "edition_id": nxt["id"] if nxt else None, "date": today().isoformat(),
                                             "channel": "gmail", "direction": "out", "subject": subject, "summary": f"Sent to {to}",
                                             "gmail_thread_id": sent.get("threadId"), "gmail_message_id": sent.get("id"), "status": "sent", "follow_up_due": fu})
            dbm.log_activity(con, "email", f"Sent email to {to}: {subject}", conf["id"])
        dbm.insert_row(con, "emails", {"gmail_id": sent.get("id"), "thread_id": sent.get("threadId"), "conference_id": conf["id"] if conf else None,
                                       "date": datetime.now().isoformat(), "from_addr": gmail.status().get("email") or "me", "to_addr": to,
                                       "subject": subject, "snippet": body[:120], "body_text": body, "labels": json.dumps(["SENT"]), "is_sent": 1,
                                       "match_reason": "sent from app"})
    return back(f"/conferences/{slug}" if slug else "/email", msg="Sent")


# ---------------------------------------------------------------- gmail oauth

@app.get("/gmail/connect")
def gmail_connect(request: Request):
    if not gmail.CREDENTIALS_PATH.exists():
        return back("/settings", err="No credentials.json yet — follow GMAIL-SETUP.md first")
    redirect_uri = str(request.base_url).rstrip("/") + "/gmail/callback"
    try:
        url = gmail.auth_url(redirect_uri)
    except Exception as ex:
        return back("/settings", err=f"Could not start Google sign-in: {ex}")
    return RedirectResponse(url)


@app.get("/gmail/callback")
def gmail_callback(request: Request):
    try:
        email = gmail.finish_auth(str(request.url))
    except Exception as ex:
        return back("/settings", err=f"Google sign-in failed: {ex}")
    with dbm.db() as con:
        dbm.log_activity(con, "gmail", f"Connected Gmail as {email}")
        con.execute("DELETE FROM emails WHERE match_reason='sample data'")
    return back("/settings", msg=f"Gmail connected as {email}. Click 'Sync now' on the Email page.")


@app.post("/gmail/disconnect")
def gmail_disconnect():
    gmail.disconnect()
    return back("/settings", msg="Gmail disconnected")


# ---------------------------------------------------------------- settings, templates, sources, import/export

@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    with dbm.db() as con:
        tmpls = dbm.rows(con, "SELECT * FROM templates ORDER BY name")
        sources = dbm.rows(con, "SELECT * FROM sources ORDER BY id")
        stats = {"conferences": dbm.row(con, "SELECT COUNT(*) AS n FROM conferences")["n"], "emails": dbm.row(con, "SELECT COUNT(*) AS n FROM emails")["n"]}
        suppressed = dbm.jloads(dbm.get_settings(con).get("suppressed_slugs")) or []
        last = dbm.row(con, "SELECT message FROM activity WHERE kind IN ('refresh','dataset') AND conference_id IS NULL ORDER BY id DESC LIMIT 1")
    return render(request, "settings.html", tmpls=tmpls, sources=sources, stats=stats, db_path=str(dbm.DB_PATH),
                  suppressed=suppressed, last_refresh_msg=(last or {}).get("message"),
                  cred_path=str(gmail.CREDENTIALS_PATH), base_url=str(request.base_url).rstrip("/"))


@app.post("/settings")
async def settings_save(request: Request):
    form = await request.form()
    with dbm.db() as con:
        for k in dbm.DEFAULT_SETTINGS:
            if k in form and k != "refresh_hints":
                dbm.set_setting(con, k, form[k].strip())
        if form.get("refresh_hints_form"):
            dbm.set_setting(con, "refresh_hints", "1" if form.get("refresh_hints") else "0")
        if form.get("home_city"):
            g = geocode(form["home_city"], form.get("home_region"), form.get("home_country") or "US")
            if g:
                dbm.set_setting(con, "home_lat", str(g[0]))
                dbm.set_setting(con, "home_lon", str(g[1]))
                dbm.set_setting(con, "home_label", ", ".join(p for p in (form["home_city"], form.get("home_region")) if p))
    return back("/settings", msg="Settings saved")


@app.post("/templates")
async def template_save(request: Request):
    form = await request.form()
    with dbm.db() as con:
        if form.get("id"):
            dbm.update_row(con, "templates", int(form["id"]), {"name": form.get("name"), "subject": form.get("subject"), "body": form.get("body")})
        else:
            dbm.insert_row(con, "templates", {"name": form.get("name") or "Untitled", "subject": form.get("subject"), "body": form.get("body")})
    return back("/settings#templates", msg="Template saved")


@app.post("/templates/{tid}/delete")
def template_delete(tid: int):
    with dbm.db() as con:
        con.execute("DELETE FROM templates WHERE id=?", (tid,))
    return back("/settings#templates", msg="Template deleted")


@app.post("/sources")
async def source_save(request: Request):
    form = await request.form()
    with dbm.db() as con:
        dbm.insert_row(con, "sources", {"name": form.get("name"), "url": form.get("url"), "notes": form.get("notes")})
    return back("/settings#sources", msg="Source added")


@app.post("/sources/{sid}/delete")
def source_delete(sid: int):
    with dbm.db() as con:
        con.execute("DELETE FROM sources WHERE id=?", (sid,))
    return back("/settings#sources", msg="Source removed")


@app.post("/settings/import")
async def settings_import(request: Request, file: UploadFile = File(...), overwrite: str = ""):
    tmp = dbm.DATA_DIR / "uploads"
    tmp.mkdir(parents=True, exist_ok=True)
    p = tmp / (file.filename or "upload.xlsx")
    p.write_bytes(await file.read())
    try:
        r = import_xlsx(p, overwrite=bool(overwrite))
    except Exception as ex:
        return back("/settings", err=f"Import failed: {ex}")
    return back("/settings", msg=f"Imported {r['total']} conferences ({r['new']} new, {r['updated']} updated)")


@app.post("/settings/reseed")
def settings_reseed():
    if not SEED_PATH.exists():
        return back("/settings", err="No bundled dataset found")
    try:
        r = feeds.apply_seed_to_db(SEED_PATH)
    except Exception as ex:  # noqa: BLE001
        return back("/settings", err=f"Could not apply the dataset: {ex}")
    return back("/settings", msg=r["text"])


@app.post("/settings/refresh")
def settings_refresh():
    def run():
        try:
            feeds.refresh_db()
        except Exception as ex:  # noqa: BLE001
            with dbm.db() as con:
                dbm.log_activity(con, "refresh", f"Refresh failed: {ex}")
    threading.Thread(target=run, daemon=True, name="manual-refresh").start()
    return back("/settings#refresh", msg="Refresh started. It takes a minute or two (longer with website scanning); results appear in the dashboard activity log.")


@app.post("/settings/unsuppress")
async def settings_unsuppress(request: Request):
    form = await request.form()
    with dbm.db() as con:
        sup = set(dbm.jloads(dbm.get_settings(con).get("suppressed_slugs")) or [])
        sup.discard(form.get("slug", ""))
        dbm.set_setting(con, "suppressed_slugs", json.dumps(sorted(sup)))
    return back("/settings#data", msg="Forgotten. The next dataset update or refresh may add it back.")


@app.get("/export/xlsx")
def export_xlsx_route():
    data = export_xlsx()
    name = f"Conferences {today().isoformat()}.xlsx"
    return Response(data, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.get("/export/ics")
@app.get("/calendar.ics")
def export_ics_route(mine: str = "", deadlines: str = "1"):
    ics = export_ics(include_deadlines=deadlines == "1", only_mine=bool(mine))
    return Response(ics, media_type="text/calendar", headers={"Content-Disposition": 'attachment; filename="conferences.ics"'})


@app.get("/api/score/{slug}")
def api_score(slug: str):
    with dbm.db() as con:
        settings = dbm.get_settings(con)
        conf = dbm.row(con, "SELECT * FROM conferences WHERE slug=?", (slug,))
        if not conf:
            return JSONResponse({"error": "not found"}, status_code=404)
        return JSONResponse(enrich(con, conf, settings)["score"])


@app.get("/api/conferences.json")
def api_conferences():
    with dbm.db() as con:
        settings = dbm.get_settings(con)
        out = []
        for c in all_enriched(con, settings):
            c = dict(c)
            out.append({k: c.get(k) for k in ("slug", "name", "type", "city", "region", "country", "website", "contact_email", "discord_url")} |
                       {"score": c["score"]["score"], "next": {k: (c["next"] or {}).get(k) for k in ("year", "start_date", "end_date", "dates_status", "our_status")}})
    return JSONResponse(out)


@app.post("/quick/edition-status")
async def quick_edition_status(request: Request):
    form = await request.form()
    with dbm.db() as con:
        ed = dbm.row(con, "SELECT e.*, c.slug FROM editions e JOIN conferences c ON c.id=e.conference_id WHERE e.id=?", (int(form["edition_id"]),))
        dbm.update_row(con, "editions", ed["id"], {"our_status": form.get("our_status")})
        dbm.log_activity(con, "status", f"{ed['year']}: status → {form.get('our_status')}", ed["conference_id"])
    return back(form.get("next") or f"/conferences/{ed['slug']}", msg="Status updated")
