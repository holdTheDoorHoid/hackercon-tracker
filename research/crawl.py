"""Crawl conference websites and pull out contact info, social links, CFP /
sponsor / village pages, PDFs, and date-looking snippets.

usage: crawl.py targets.json outdir
targets.json: [{"slug": "...", "website": "https://..."}]
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
MONTH = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?"
DATE_RES = [
    re.compile(rf"\b({MONTH}\s+\d{{1,2}}(?:st|nd|rd|th)?(?:\s*[-–—&]\s*(?:{MONTH}\s+)?\d{{1,2}}(?:st|nd|rd|th)?)?,?\s+20(?:26|27|28))", re.I),
    re.compile(rf"\b(\d{{1,2}}(?:st|nd|rd|th)?(?:\s*[-–—&]\s*\d{{1,2}}(?:st|nd|rd|th)?)?\s+{MONTH},?\s+20(?:26|27|28))", re.I),
    re.compile(r"\b(20(?:26|27|28)-\d{2}-\d{2})\b"),
    re.compile(r"\b(\d{1,2}/\d{1,2}/20(?:26|27|28))\b"),
]
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
BAD_EMAIL = re.compile(r"(\.png|\.jpg|\.gif|\.svg|\.webp|example\.|sentry|wixpress|@\d|noreply|no-reply|@w3\.org|@2x|@3x|domain\.com|email\.com|yourdomain)", re.I)
SOCIAL = {
    "discord": re.compile(r"discord\.(gg|com/invite)", re.I),
    "twitter": re.compile(r"^https?://(www\.)?(twitter|x)\.com/([A-Za-z0-9_]{1,15})/?$", re.I),
    "bluesky": re.compile(r"bsky\.app/profile/", re.I),
    "linkedin": re.compile(r"linkedin\.com/(company|in|groups)/", re.I),
    "instagram": re.compile(r"^https?://(www\.)?instagram\.com/[A-Za-z0-9_.]+/?$", re.I),
    "youtube": re.compile(r"youtube\.com/(@|c/|channel/|user/)", re.I),
    "facebook": re.compile(r"^https?://(www\.)?facebook\.com/[A-Za-z0-9_.]+/?$", re.I),
    "meetup": re.compile(r"meetup\.com/", re.I),
    "mastodon": re.compile(r"^https?://(infosec\.exchange|mastodon\.[a-z.]+|fosstodon\.org|hachyderm\.io|defcon\.social|chaos\.social|social\.[a-z.]+|[a-z.]+\.social)/@[\w.]+/?$", re.I),
    "tickets": re.compile(r"(eventbrite|tickets|tito\.io|ti\.to|pretix|humanitix|universe\.com|hopin)", re.I),
}
PAGE_KINDS = {
    "cfp": re.compile(r"(cfp|call[-_ ]?for[-_ ]?(papers|presentations|speakers|proposals|talks)|speak|sessionize|pretalx|papercall|submit)", re.I),
    "sponsor": re.compile(r"(sponsor|vendor|exhibit|prospectus|partner|booth|expo)", re.I),
    "village": re.compile(r"(village|workshop|training|trainings|classes|labs)", re.I),
    "contact": re.compile(r"(contact|about|team|organi[sz]ers?)", re.I),
    "schedule": re.compile(r"(schedule|agenda|program|dates|when)", re.I),
}
ATTEND_RE = re.compile(r"(\d[\d,]{2,6})\+?\s*(?:attendees|hackers|participants|people|guests|infosec professionals|security professionals|visitors)", re.I)
PRICE_RE = re.compile(r"(\$\s?\d{1,3}(?:,\d{3})*(?:\.\d\d)?)", re.I)


def clean_text(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    for t in soup(["script", "style", "noscript", "svg"]):
        t.decompose()
    return re.sub(r"\s+", " ", soup.get_text(" ", strip=True))


def extract(url: str, html: str) -> dict:
    soup = BeautifulSoup(html, "lxml")
    title = (soup.title.string or "").strip() if soup.title and soup.title.string else ""
    text = clean_text(html)
    out = {"url": url, "title": title, "emails": {}, "social": {}, "pages": {}, "pdfs": [], "dates": [], "attendance": [], "prices": [],
           "text_len": len(text), "snippet": text[:600]}
    for m in EMAIL_RE.findall(html):
        if not BAD_EMAIL.search(m):
            out["emails"][m.lower()] = url
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("javascript:", "#", "tel:")):
            continue
        if href.startswith("mailto:"):
            e = href[7:].split("?")[0].strip().lower()
            if e and not BAD_EMAIL.search(e):
                out["emails"][e] = url
            continue
        full = urljoin(url, href)
        label = a.get_text(" ", strip=True)[:80]
        for k, rx in SOCIAL.items():
            if rx.search(full):
                out["social"].setdefault(k, [])
                if full not in out["social"][k]:
                    out["social"][k].append(full)
        low = (full + " " + label).lower()
        if full.lower().endswith(".pdf") or ".pdf?" in full.lower():
            if full not in [p[0] for p in out["pdfs"]]:
                out["pdfs"].append((full, label))
        for kind, rx in PAGE_KINDS.items():
            if rx.search(low) and urlparse(full).netloc and not any(rx2.search(full) for rx2 in (SOCIAL["twitter"], SOCIAL["linkedin"], SOCIAL["facebook"], SOCIAL["instagram"])):
                out["pages"].setdefault(kind, [])
                if full not in out["pages"][kind] and len(out["pages"][kind]) < 8:
                    out["pages"][kind].append(full)
    seen = set()
    for rx in DATE_RES:
        for m in rx.findall(text):
            m = re.sub(r"\s+", " ", m).strip()
            if m.lower() not in seen:
                seen.add(m.lower())
                # keep a little context around the match
                i = text.find(m)
                ctx = text[max(0, i - 70): i + len(m) + 50] if i >= 0 else ""
                out["dates"].append({"date": m, "ctx": ctx})
    out["dates"] = out["dates"][:20]
    for m in ATTEND_RE.finditer(text):
        out["attendance"].append(text[max(0, m.start() - 60): m.end() + 20])
    out["attendance"] = out["attendance"][:6]
    for m in PRICE_RE.finditer(text):
        ctx = text[max(0, m.start() - 80): m.end() + 40]
        if re.search(r"(ticket|table|vendor|sponsor|booth|admission|registration|pass|badge|training|workshop)", ctx, re.I):
            out["prices"].append(ctx)
    out["prices"] = out["prices"][:8]
    return out


async def fetch(client: httpx.AsyncClient, url: str) -> tuple[str, int, str]:
    try:
        r = await client.get(url, follow_redirects=True)
        ctype = r.headers.get("content-type", "")
        if "html" not in ctype and "xml" not in ctype and "text" not in ctype:
            return str(r.url), r.status_code, ""
        return str(r.url), r.status_code, r.text
    except Exception as e:  # noqa
        return url, 0, f"ERROR {type(e).__name__}: {e}"


async def crawl_one(client, sem, t: dict, outdir: Path):
    slug, site = t["slug"], t["website"]
    async with sem:
        final, status, html = await fetch(client, site)
        rec = {"slug": slug, "website": site, "final_url": final, "status": status, "pages_fetched": [], "home": None, "sub": []}
        if status == 0 or not html or html.startswith("ERROR"):
            rec["error"] = html[:200] if html else f"status {status}"
            (outdir / f"{slug}.json").write_text(json.dumps(rec, indent=1))
            print(f"FAIL {slug} {site} -> {status} {html[:80]}")
            return
        home = extract(final, html)
        rec["home"] = home
        rec["pages_fetched"].append(final)
        # follow the most useful subpages on the same host
        host = urlparse(final).netloc
        todo = []
        for kind in ("sponsor", "cfp", "village", "contact", "schedule"):
            for u in home["pages"].get(kind, [])[:2]:
                if urlparse(u).netloc.endswith(host.replace("www.", "")) and u not in todo and u.rstrip("/") != final.rstrip("/"):
                    todo.append(u)
        for u in todo[:6]:
            f2, s2, h2 = await fetch(client, u)
            if s2 == 200 and h2 and not h2.startswith("ERROR"):
                sub = extract(f2, h2)
                sub["kind"] = [k for k, us in home["pages"].items() if u in us]
                rec["sub"].append(sub)
                rec["pages_fetched"].append(f2)
        (outdir / f"{slug}.json").write_text(json.dumps(rec, indent=1))
        emails = set(home["emails"]) | {e for s in rec["sub"] for e in s["emails"]}
        print(f"OK   {slug} {status} {final} | emails={len(emails)} discord={'y' if home['social'].get('discord') else '-'} dates={len(home['dates'])} sub={len(rec['sub'])}")


async def main(targets_path: str, outdir: str):
    targets = json.loads(Path(targets_path).read_text())
    out = Path(outdir)
    out.mkdir(parents=True, exist_ok=True)
    sem = asyncio.Semaphore(8)
    async with httpx.AsyncClient(headers={"User-Agent": UA, "Accept": "text/html,application/xhtml+xml,*/*;q=0.8", "Accept-Language": "en-US,en;q=0.9"},
                                 timeout=25, verify=False) as client:
        await asyncio.gather(*(crawl_one(client, sem, t, out) for t in targets if t.get("website")))


if __name__ == "__main__":
    import warnings
    warnings.filterwarnings("ignore")
    asyncio.run(main(sys.argv[1], sys.argv[2]))
