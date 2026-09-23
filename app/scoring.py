"""Attend-or-skip scoring.

Each factor is scored 0-100, then combined with the weights from Settings.
The result is a number plus a breakdown so the UI can explain *why*.
"""
from __future__ import annotations

from datetime import date, timedelta

from .geo import travel_estimate

BAND_TO_EST = {
    "<100": 60, "100 - 250": 175, "100 -  250": 175, "100-250": 175,
    "250 - 1000": 500, "250-1000": 500, "1000 - 3000": 1800, "1000-3000": 1800,
    "3000 - 5000": 4000, "5000 - 10k": 7000, "10k - 20k": 15000, "20k+": 25000,
}


def attendance_number(conf: dict) -> int | None:
    if conf.get("attendance_est"):
        try:
            return int(conf["attendance_est"])
        except (TypeError, ValueError):
            pass
    band = (conf.get("attendance_band") or "").strip()
    if band in BAND_TO_EST:
        return BAND_TO_EST[band]
    try:
        return int(float(band))
    except ValueError:
        return None


def size_score(conf: dict) -> tuple[int, str]:
    n = attendance_number(conf)
    if n is None:
        return 45, "Attendance unknown (neutral)"
    if n < 100:
        s = 20
    elif n < 250:
        s = 35
    elif n < 1000:
        s = 55
    elif n < 3000:
        s = 75
    elif n < 10000:
        s = 88
    else:
        s = 100
    return s, f"~{n:,} attendees"


def travel_score(conf: dict, settings: dict) -> tuple[int, str, dict]:
    t = travel_estimate(conf, settings)
    if t["mode"] == "online":
        return 100, "Online, no travel", t
    if t["mode"] == "unknown":
        return 40, t.get("label") or "Location unknown", t
    if t["mode"] == "drive":
        h = t["drive_hours"]
        if h <= 1:
            s = 100
        elif h <= 2:
            s = 92
        elif h <= 3:
            s = 84
        elif h <= 4:
            s = 75
        elif h <= 5:
            s = 66
        else:
            s = 58
        return s, t["label"], t
    # flying
    if t.get("far_international"):
        s = 15
    elif t["international"]:
        s = 30
    else:
        miles = t["miles"] or 0
        s = 42 if miles < 1500 else 34
    return s, t["label"], t


def vending_score(conf: dict) -> tuple[int, str]:
    p = (conf.get("vending_policy") or "unknown").lower()
    cost = (conf.get("table_cost") or "").lower()
    if p == "yes":
        s, why = 100, "Vending allowed"
        if any(w in cost for w in ("free", "$0", "no cost", "included")):
            why += ", free table"
        elif cost:
            why += f", table {conf.get('table_cost')}"
            digits = "".join(ch for ch in cost if ch.isdigit())
            if digits and int(digits) >= 1500:
                s = 70
    elif p == "sponsor-only":
        s, why = 55, "Vending only via sponsorship"
    elif p == "no":
        s, why = 0, "No vending"
    else:
        s, why = 50, "Vending policy unknown"
    return s, why


def workshop_score(conf: dict, deadlines: list[dict]) -> tuple[int, str]:
    ws = [d for d in deadlines if d["kind"] in ("training", "cfv")]
    statuses = {d["status"] for d in ws}
    if "accepted" in statuses:
        return 100, "Workshop / village accepted"
    if "submitted" in statuses or "waitlist" in statuses:
        return 72, "Workshop / village proposal in"
    if "rejected" in statuses or "declined" in statuses:
        return 25, "Workshop / village turned down"
    track = (conf.get("workshop_track") or "unknown").lower()
    village = (conf.get("village_hosting") or "unknown").lower()
    if track == "yes" or village == "yes":
        return 60, "Has a workshop or village track"
    if track == "no" and village == "no":
        return 20, "No workshop or village track"
    return 45, "Workshop track unknown"


def pto_score(edition: dict, travel: dict) -> tuple[int, str]:
    start = edition.get("start_date")
    end = edition.get("end_date") or start
    if not start:
        return 60, "Dates unknown"
    try:
        s = date.fromisoformat(start)
        e = date.fromisoformat(end)
    except ValueError:
        return 60, "Dates unknown"
    if e < s:
        e = s
    weekdays = sum(1 for i in range((e - s).days + 1) if (s + timedelta(days=i)).weekday() < 5)
    # travel days: a flight usually costs a weekday on each side unless the con starts Saturday
    extra = 0
    if travel.get("mode") == "fly":
        if s.weekday() < 5:
            extra += 1
        if e.weekday() < 4:
            extra += 1
    elif travel.get("mode") == "drive" and (travel.get("drive_hours") or 0) > 4:
        if s.weekday() in (0, 1, 2, 3):
            extra += 1
    total = weekdays + extra
    table = {0: 100, 1: 78, 2: 58, 3: 40, 4: 28}
    sc = table.get(total, 18)
    why = "Weekend only" if total == 0 else f"~{total} weekday{'s' if total != 1 else ''} of PTO"
    if extra:
        why += f" (incl. {extra} travel)"
    return sc, why


def past_score(conf: dict, editions: list[dict]) -> tuple[int, str]:
    rated = [e for e in editions if e.get("worth_it")]
    if rated:
        latest = sorted(rated, key=lambda e: e["year"])[-1]
        r = int(latest["worth_it"])
        return {1: 15, 2: 35, 3: 55, 4: 80, 5: 100}.get(r, 55), f"Rated {r}/5 in {latest['year']}"
    if conf.get("past_rating"):
        r = int(conf["past_rating"])
        return {1: 15, 2: 35, 3: 55, 4: 80, 5: 100}.get(r, 55), f"Past rating {r}/5"
    attended = [e for e in editions if e.get("our_status") == "attended"]
    if attended:
        return 62, f"Attended {len(attended)}x, not rated"
    return 50, "No history yet"


def compute_score(conf: dict, edition: dict | None, deadlines: list[dict], editions: list[dict], settings: dict) -> dict:
    w = {k: float(settings.get(f"weight_{k}", 0)) for k in ("size", "travel", "vending", "workshop", "pto", "past")}
    total_w = sum(w.values()) or 1
    size_s, size_why = size_score(conf)
    trav_s, trav_why, trav = travel_score(conf, settings)
    vend_s, vend_why = vending_score(conf)
    work_s, work_why = workshop_score(conf, deadlines)
    pto_s, pto_why = pto_score(edition or {}, trav)
    past_s, past_why = past_score(conf, editions)
    parts = {
        "size": (size_s, size_why), "travel": (trav_s, trav_why), "vending": (vend_s, vend_why),
        "workshop": (work_s, work_why), "pto": (pto_s, pto_why), "past": (past_s, past_why),
    }
    raw = sum(parts[k][0] * w[k] for k in parts) / total_w
    flags = []
    # hard rules
    if (conf.get("vending_policy") or "").lower() == "no":
        raw = min(raw, 35)
        flags.append("Capped: no vending")
    if conf.get("iot_village_partner"):
        raw = min(100, raw + 6)
        flags.append("+6 IoT Village partner")
    if (conf.get("priority") or "normal") == "high":
        raw = min(100, raw + 5)
        flags.append("+5 priority")
    if (conf.get("priority") or "normal") == "ignore":
        raw = 0
        flags.append("Ignored")
    if edition and edition.get("our_status") in ("rejected", "declined", "not-going", "skipped"):
        flags.append(f"Status: {edition['our_status']}")
    if edition and edition.get("dates_status") in ("cancelled", "postponed"):
        raw = 0
        flags.append(edition["dates_status"].capitalize())
    return {
        "score": round(raw),
        "parts": {k: {"score": v[0], "why": v[1], "weight": w[k]} for k, v in parts.items()},
        "flags": flags,
        "travel": trav,
    }


def grade(score: int) -> str:
    if score >= 70:
        return "A"
    if score >= 60:
        return "B"
    if score >= 50:
        return "C"
    if score >= 40:
        return "D"
    return "F"
