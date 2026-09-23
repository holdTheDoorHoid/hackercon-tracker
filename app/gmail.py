"""Gmail integration (OAuth, sync, send). Works in a 'sample' mode until you
drop a Google OAuth client file at data/credentials.json — see GMAIL-SETUP.md.
"""
from __future__ import annotations

import base64
import json
import re
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import parseaddr, getaddresses
from pathlib import Path

from . import db as dbm

DATA = dbm.DATA_DIR
CREDENTIALS_PATH = DATA / "credentials.json"
TOKEN_PATH = DATA / "token.json"
SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]

_flow_state: dict = {}


def status() -> dict:
    configured = CREDENTIALS_PATH.exists()
    connected = False
    email = None
    if configured and TOKEN_PATH.exists():
        try:
            creds = _creds()
            connected = bool(creds and (creds.valid or creds.refresh_token))
            email = json.loads(TOKEN_PATH.read_text()).get("_email")
        except Exception:
            connected = False
    return {"configured": configured, "connected": connected, "email": email, "mode": "live" if connected else "sample"}


def _creds():
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request

    if not TOKEN_PATH.exists():
        return None
    creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        _save_token(creds)
    return creds


def _save_token(creds, email=None):
    data = json.loads(creds.to_json())
    if TOKEN_PATH.exists():
        try:
            old = json.loads(TOKEN_PATH.read_text())
            data["_email"] = old.get("_email")
        except Exception:
            pass
    if email:
        data["_email"] = email
    TOKEN_PATH.write_text(json.dumps(data, indent=1))


def auth_url(redirect_uri: str) -> str:
    from google_auth_oauthlib.flow import Flow

    flow = Flow.from_client_secrets_file(str(CREDENTIALS_PATH), scopes=SCOPES, redirect_uri=redirect_uri)
    url, state = flow.authorization_url(access_type="offline", prompt="consent", include_granted_scopes="true")
    _flow_state["state"] = state
    _flow_state["redirect_uri"] = redirect_uri
    return url


def finish_auth(full_callback_url: str) -> str:
    from google_auth_oauthlib.flow import Flow

    flow = Flow.from_client_secrets_file(str(CREDENTIALS_PATH), scopes=SCOPES, state=_flow_state.get("state"),
                                         redirect_uri=_flow_state.get("redirect_uri"))
    # Google redirects to http://localhost; oauthlib insists on https unless told otherwise
    import os
    os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")
    os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")
    flow.fetch_token(authorization_response=full_callback_url)
    creds = flow.credentials
    svc = _build(creds)
    profile = svc.users().getProfile(userId="me").execute()
    _save_token(creds, profile.get("emailAddress"))
    return profile.get("emailAddress")


def disconnect():
    if TOKEN_PATH.exists():
        TOKEN_PATH.unlink()


def _build(creds=None):
    from googleapiclient.discovery import build
    creds = creds or _creds()
    if not creds:
        raise RuntimeError("Gmail is not connected")
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


# ---------------- labels ----------------

def ensure_label(svc, name: str) -> str:
    labels = svc.users().labels().list(userId="me").execute().get("labels", [])
    for l in labels:
        if l["name"].lower() == name.lower():
            return l["id"]
    # nested labels need parents to exist
    if "/" in name:
        ensure_label(svc, name.rsplit("/", 1)[0])
    created = svc.users().labels().create(userId="me", body={"name": name, "labelListVisibility": "labelShow", "messageListVisibility": "show"}).execute()
    return created["id"]


def label_thread(svc, thread_id: str, label_name: str):
    lid = ensure_label(svc, label_name)
    svc.users().threads().modify(userId="me", id=thread_id, body={"addLabelIds": [lid]}).execute()


# ---------------- matching ----------------

def _domain(url_or_email: str | None) -> str | None:
    if not url_or_email:
        return None
    s = url_or_email.strip().lower()
    if "@" in s:
        return s.split("@")[-1]
    s = re.sub(r"^https?://", "", s).split("/")[0]
    return s[4:] if s.startswith("www.") else s


GENERIC_DOMAINS = {"gmail.com", "googlemail.com", "yahoo.com", "outlook.com", "hotmail.com", "proton.me", "protonmail.com",
                   "icloud.com", "me.com", "sessionize.com", "google.com", "docs.google.com", "notion.site", "eventbrite.com",
                   "pretalx.com", "linktr.ee", "eventbrite.co.uk", "hopin.com", "bit.ly", "drive.google.com"}


def build_matchers(con) -> list[dict]:
    """Per conference: domains, addresses and name keywords used to match emails."""
    out = []
    for c in dbm.rows(con, "SELECT id, name, slug, aliases, website, contact_email, cfp_email, sponsor_email, discord_url FROM conferences WHERE archived=0"):
        domains = set()
        for u in (c.get("website"), c.get("contact_email"), c.get("cfp_email"), c.get("sponsor_email")):
            d = _domain(u)
            if d and d not in GENERIC_DOMAINS:
                domains.add(d)
        addrs = {a.lower() for a in (c.get("contact_email"), c.get("cfp_email"), c.get("sponsor_email")) if a}
        for ct in dbm.rows(con, "SELECT email FROM contacts WHERE conference_id=? AND email IS NOT NULL", (c["id"],)):
            addrs.add(ct["email"].lower())
            d = _domain(ct["email"])
            if d and d not in GENERIC_DOMAINS:
                domains.add(d)
        names = {c["name"]} | set(dbm.jloads(c.get("aliases")))
        kws = set()
        for n in names:
            n2 = re.sub(r"\b20\d\d\b", "", n).strip()
            if len(n2) >= 6:
                kws.add(n2.lower())
                kws.add(re.sub(r"\s+", "", n2.lower()))  # "bsides philly" -> "bsidesphilly"
        out.append({"id": c["id"], "name": c["name"], "slug": c["slug"], "domains": domains, "addrs": addrs, "kws": kws})
    return out


def match_email(matchers: list[dict], from_addr: str, to_addr: str, subject: str, snippet: str, labels: list[str], prefix: str) -> tuple[int | None, str | None]:
    all_addrs = [a.lower() for _, a in getaddresses([from_addr or "", to_addr or ""]) if a]
    doms = {a.split("@")[-1] for a in all_addrs}
    text = f"{subject or ''} {snippet or ''}".lower()
    # 1. our own label wins
    for l in labels or []:
        if l.lower().startswith(prefix.lower() + "/"):
            want = l.split("/", 1)[1].strip().lower()
            for m in matchers:
                if m["name"].lower() == want or m["slug"] == want:
                    return m["id"], f"label {l}"
    # 2. exact address / domain
    for m in matchers:
        if m["addrs"] & set(all_addrs):
            return m["id"], "contact address"
    for m in matchers:
        if m["domains"] & doms:
            return m["id"], "domain " + ", ".join(m["domains"] & doms)
    # 3. name keyword in subject/snippet (longest keyword wins)
    best = None
    for m in matchers:
        for kw in m["kws"]:
            if kw in text and (best is None or len(kw) > len(best[1])):
                best = (m["id"], kw)
    if best:
        return best[0], f"name '{best[1]}'"
    return None, None


# ---------------- sync ----------------

def _header(msg, name):
    for h in msg.get("payload", {}).get("headers", []):
        if h["name"].lower() == name.lower():
            return h["value"]
    return ""


def _body_text(payload) -> str:
    """Best-effort plain text from a Gmail payload."""
    if not payload:
        return ""
    mime = payload.get("mimeType", "")
    data = payload.get("body", {}).get("data")
    if data and mime.startswith("text/plain"):
        return base64.urlsafe_b64decode(data + "==").decode("utf-8", "replace")
    if data and mime.startswith("text/html"):
        html = base64.urlsafe_b64decode(data + "==").decode("utf-8", "replace")
        return re.sub(r"\s+\n", "\n", re.sub(r"<[^>]+>", " ", re.sub(r"(?is)<(script|style).*?</\1>", "", html)))
    parts = payload.get("parts") or []
    texts = [_body_text(p) for p in parts]
    plain = [t for p, t in zip(parts, texts) if p.get("mimeType", "").startswith("text/plain") and t.strip()]
    if plain:
        return plain[0]
    return next((t for t in texts if t.strip()), "")


def sync(con, max_messages: int = 400) -> dict:
    """Pull conference-related mail into the local cache and match it to conferences."""
    st = status()
    if not st["connected"]:
        return load_sample(con)
    settings = dbm.get_settings(con)
    prefix = settings.get("gmail_label_prefix", "LSOH")
    svc = _build()
    matchers = build_matchers(con)
    # Build one big query: our label, any known domain/address, and the general conference sweep.
    terms = set()
    for m in matchers:
        for d in m["domains"]:
            terms.add(f"from:{d}")
            terms.add(f"to:{d}")
        for a in m["addrs"]:
            terms.add(f"from:{a}")
            terms.add(f"to:{a}")
    base = settings.get("gmail_sync_query", "newer_than:2y")
    queries = [f"label:{prefix}-* OR label:{prefix}/*"]
    chunk = []
    for t in sorted(terms):
        chunk.append(t)
        if len(chunk) >= 40:
            queries.append(f"{base} (" + " OR ".join(chunk) + ")")
            chunk = []
    if chunk:
        queries.append(f"{base} (" + " OR ".join(chunk) + ")")
    queries.append(f"{base} (bsides OR sponsor OR sponsorship OR vendor OR village OR workshop OR CFP OR \"call for\" OR conference OR con)")
    seen = set()
    ids = []
    for q in queries:
        token = None
        while True:
            resp = svc.users().messages().list(userId="me", q=q, maxResults=100, pageToken=token).execute()
            for m in resp.get("messages", []):
                if m["id"] not in seen:
                    seen.add(m["id"])
                    ids.append(m["id"])
            token = resp.get("nextPageToken")
            if not token or len(ids) >= max_messages:
                break
        if len(ids) >= max_messages:
            break
    known = {r["gmail_id"] for r in dbm.rows(con, "SELECT gmail_id FROM emails")}
    label_names = {l["id"]: l["name"] for l in svc.users().labels().list(userId="me").execute().get("labels", [])}
    n_new = n_matched = 0
    for mid in ids:
        if mid in known:
            continue
        msg = svc.users().messages().get(userId="me", id=mid, format="full").execute()
        labels = [label_names.get(l, l) for l in msg.get("labelIds", [])]
        frm, to, subj = _header(msg, "From"), _header(msg, "To"), _header(msg, "Subject")
        ts = int(msg.get("internalDate", 0)) / 1000
        when = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
        conf_id, reason = match_email(matchers, frm, to, subj, msg.get("snippet", ""), labels, prefix)
        if conf_id is None:
            # only keep unmatched mail if it came from the broad sweep AND looks conference-ish
            if not any(k in (subj or "").lower() for k in ("bsides", "sponsor", "vendor", "village", "workshop", "cfp", "call for", "conference")):
                continue
        else:
            n_matched += 1
        dbm.insert_row(con, "emails", {
            "gmail_id": mid, "thread_id": msg.get("threadId"), "conference_id": conf_id, "date": when,
            "from_addr": frm, "to_addr": to, "subject": subj, "snippet": msg.get("snippet", ""),
            "body_text": _body_text(msg.get("payload"))[:20000], "labels": json.dumps(labels),
            "is_sent": 1 if "SENT" in labels else 0, "is_unread": 1 if "UNREAD" in labels else 0, "match_reason": reason,
        })
        n_new += 1
    # mark outreach as replied when an inbound message exists in the same thread
    for o in dbm.rows(con, "SELECT * FROM outreach WHERE gmail_thread_id IS NOT NULL AND status IN ('sent','no-reply')"):
        inbound = dbm.row(con, "SELECT id FROM emails WHERE thread_id=? AND is_sent=0", (o["gmail_thread_id"],))
        if inbound:
            dbm.update_row(con, "outreach", o["id"], {"status": "replied"})
    dbm.set_setting(con, "gmail_last_sync", datetime.now().isoformat(timespec="minutes"))
    dbm.log_activity(con, "gmail", f"Gmail sync: {n_new} new messages cached, {n_matched} matched to conferences")
    return {"new": n_new, "matched": n_matched, "scanned": len(ids), "mode": "live"}


def rematch(con) -> int:
    """Re-run matching over cached mail (after adding contacts, for example)."""
    settings = dbm.get_settings(con)
    matchers = build_matchers(con)
    n = 0
    for e in dbm.rows(con, "SELECT * FROM emails WHERE conference_id IS NULL OR match_reason NOT LIKE 'manual%'"):
        cid, reason = match_email(matchers, e["from_addr"], e["to_addr"], e["subject"], e["snippet"], dbm.jloads(e["labels"]), settings.get("gmail_label_prefix", "LSOH"))
        if cid and cid != e["conference_id"]:
            dbm.update_row(con, "emails", e["id"], {"conference_id": cid, "match_reason": reason})
            n += 1
    return n


def get_thread(thread_id: str) -> list[dict]:
    svc = _build()
    t = svc.users().threads().get(userId="me", id=thread_id, format="full").execute()
    out = []
    for m in t.get("messages", []):
        out.append({
            "id": m["id"], "from": _header(m, "From"), "to": _header(m, "To"), "subject": _header(m, "Subject"),
            "date": _header(m, "Date"), "message_id": _header(m, "Message-ID"), "body": _body_text(m.get("payload")),
            "labels": m.get("labelIds", []),
        })
    return out


def send(to: str, subject: str, body: str, thread_id: str | None = None, in_reply_to: str | None = None,
         label: str | None = None, cc: str | None = None) -> dict:
    svc = _build()
    msg = EmailMessage()
    msg["To"] = to
    if cc:
        msg["Cc"] = cc
    msg["Subject"] = subject
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to
    msg.set_content(body)
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    payload = {"raw": raw}
    if thread_id:
        payload["threadId"] = thread_id
    sent = svc.users().messages().send(userId="me", body=payload).execute()
    if label:
        try:
            label_thread(svc, sent["threadId"], label)
        except Exception:
            pass
    return sent


def create_draft(to: str, subject: str, body: str, thread_id: str | None = None) -> dict:
    svc = _build()
    msg = EmailMessage()
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    payload = {"message": {"raw": raw}}
    if thread_id:
        payload["message"]["threadId"] = thread_id
    return svc.users().drafts().create(userId="me", body=payload).execute()


# ---------------- sample mode ----------------

def load_sample(con) -> dict:
    """Populate a few clearly-labelled sample emails so the UI can be explored before Gmail is connected."""
    path = DATA / "sample_emails.json"
    if not path.exists():
        return {"new": 0, "matched": 0, "scanned": 0, "mode": "sample"}
    n = 0
    for e in json.loads(path.read_text()):
        if dbm.row(con, "SELECT id FROM emails WHERE gmail_id=?", (e["gmail_id"],)):
            continue
        c = dbm.row(con, "SELECT id FROM conferences WHERE slug=?", (e["slug"],))
        dbm.insert_row(con, "emails", {
            "gmail_id": e["gmail_id"], "thread_id": e["thread_id"], "conference_id": c["id"] if c else None, "date": e["date"],
            "from_addr": e["from"], "to_addr": e["to"], "subject": e["subject"], "snippet": e["body"][:120],
            "body_text": e["body"], "labels": json.dumps(e.get("labels", [])), "is_sent": e.get("is_sent", 0),
            "is_unread": e.get("is_unread", 0), "match_reason": "sample data",
        })
        n += 1
    return {"new": n, "matched": n, "scanned": n, "mode": "sample"}
