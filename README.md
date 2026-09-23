# Hackercon Tracker

Keep track of every hacker, BSides, and maker event you might attend: dates, CFP and
vendor deadlines, who you've emailed and what they said, and a score that says how
worth-it each event is from where you live. Everything runs on your own computer and
stays in one SQLite file.

There is also a **public, read-only site** with the same conference list, a calendar and
a map: **https://holdthedoorhoid.github.io/hackercon-tracker/** — no login, nothing tracked.
Type your city and it shows drive times, computed in your browser.

It started as an internal tool for LSOH (hardware workshops and vendor tables at
security cons) and is shared here under the GPL-3.0 so other people who do the same
kind of circuit can run it.

## Run it

```bash
git clone https://github.com/holdTheDoorHoid/hackercon-tracker.git
cd hackercon-tracker
./start.sh
```

That builds a private Python environment the first time (about a minute), starts the
app, and opens http://localhost:8765. Leave the terminal open; Ctrl+C stops it.
Needs Python 3.11 or newer. On Windows, run the same thing by hand:

```bat
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python -m uvicorn app.main:app --port 8765
```

First run: open **Settings** and set your home city. That's all the setup that is
required. Gmail is optional; see [GMAIL-SETUP.md](GMAIL-SETUP.md) (about ten minutes, once).

Your phone can open the app at this computer's Wi-Fi address (for example
`http://192.168.1.20:8765`) while it is running.

## What's inside

- **Dashboard** – deadlines in the next 45 days, outreach waiting on a reply, hotel and
  flight reminders, unconfirmed dates that need a check (with any dates the event's
  website mentions), upcoming events with scores, and same-weekend conflicts.
- **Conferences** – the whole list, filterable by type, country, month, status, and
  "driveable". Each conference page has links (site, CFP, sponsor docs, Discord, socials,
  emails), the score and why, every year's edition with dates, deadlines, hotel/travel/PTO,
  outreach history and the matching email threads.
- **Calendar** – month view of events and deadlines, plus an `.ics` feed you can subscribe
  to from Google Calendar or Apple Calendar.
- **Conflicts** – events on the same weekend, ranked by score, with a side-by-side compare.
- **Email** – your Gmail threads matched to conferences (by label, contact address, sender
  domain, or the conference name), a compose screen with templates, and per-conference
  Gmail labels. You write or approve every email; nothing is sent automatically.
- **Settings** – home base, drive/fly cutoff, score weights, refresh schedule, templates,
  sources to watch, spreadsheet export/import, database location.

### The score

Each event gets 0–100 from six parts; the weights are yours to change in Settings.

| Part | Default weight | What it looks at |
|---|---|---|
| Size | 25 | Expected attendance |
| Travel | 20 | Drive time or flight from home; international is harder |
| Vending | 20 | Whether you can sell, and what a table costs |
| Workshop | 15 | Whether there's a workshop or village track |
| PTO | 10 | Weekday days you'd have to take off |
| Past results | 10 | How it went last time (your rating, sales) |

Hard rules on top: no vending caps the score at 35; "ignore" or cancelled scores 0.
Grades: A ≥ 70, B ≥ 60, C ≥ 50, D ≥ 40, otherwise F.

### What "dates status" means

- **confirmed** – checked by a person; refreshes never change it
- **announced** – published by the organizers (feed or website)
- **tentative** – organizers hinted but haven't published a firm date
- **estimated** – nobody has announced anything; we assumed the same weekend as last year
- **postponed / cancelled** – per the organizers

## Keeping the data fresh

Three things keep dates and deadlines current without you doing anything:

1. **The app refreshes itself** every 7 days (Settings → *Keep the data fresh*; set 0 to
   turn it off, or click **Refresh now**). It pulls bsides.org, makerfaire.com,
   cfptime.org and cfp.hex.dance, and reads the website of every event whose next date
   is still unconfirmed, keeping any date it mentions as a hint.
2. **A GitHub Action runs the same refresh every Monday** and commits the updated
   dataset ([data/seed/conferences.json](data/seed/conferences.json)), which also
   republishes the public site.
3. **`git pull` then restart the app**: a newer bundled dataset is merged into your
   database on startup.

What a refresh may do: add new editions and new US/Canada BSides or Maker Faires, set or
move dates and CFP / Call-for-Makers deadlines (only ever *extending* a deadline you
already have), and fill in blank links. What it never does: touch your statuses, notes,
hotel/travel/PTO, priorities, or a deadline's submitted/accepted state; change an edition
marked **confirmed**; or bring back a conference you deleted (those are listed in
Settings → Data, with a "forget" button if you change your mind).

Everything in the refresh is in one file, [app/feeds.py](app/feeds.py), and the CLI
version is `python research/refresh.py`.

## Fixing or adding a conference

Facts that a feed can't provide (vendor policy, table cost, the right contact, a date
you confirmed with the organizers) belong in [research/verified.json](research/verified.json).
Each entry is keyed by the conference slug and wins over every feed:

```json
"bsides-example": {
  "name": "BSides Example",
  "website": "https://bsidesexample.org/",
  "city": "Example", "region": "PA", "country": "US",
  "vending_policy": "yes", "table_cost": "$250",
  "contact_email": "info@bsidesexample.org",
  "editions": [
    {"year": 2027, "start_date": "2027-04-10", "end_date": "2027-04-11", "dates_status": "confirmed",
     "deadlines": [{"kind": "cfp", "due_date": "2027-01-31", "url": "https://bsidesexample.org/cfp"}]}
  ]
}
```

`"remove": true` drops a conference for good (it will not be re-added by feeds). After
editing, rebuild the dataset with `python research/build_seed.py --offline` and open a pull
request, or just open an issue with the fact and a link and we'll do it.

## Files

```
app/            the web app (FastAPI + Jinja2 + SQLite; no build step)
  feeds.py      feed fetching and the merge rules used by every refresh
  scoring.py    the attend score
  geo.py        city table, geocoding, drive-time estimate
  gmail.py      Gmail API sync and send
data/seed/conferences.json   the dataset that ships with the app
data/tracker.db              YOUR database (git-ignored; back it up by copying it)
research/       scripts that build the dataset from scratch, and verified.json
scripts/build_site.py        turns the dataset into the public site
site/           the public site (plain HTML/JS; data.json is generated)
.github/workflows/           weekly data refresh and the Pages deploy
```

Rebuilding from scratch (`research/build_seed.py`) merges an optional spreadsheet, the
candidate list from `research/new_conferences.py`, the feeds, the website crawl
(`research/crawl.py`) and `verified.json`. You only need it if you change the candidate
list; day to day, `refresh.py` is enough.

## Privacy

Nothing leaves your computer except the requests the refresh makes to the public feeds
and conference websites, the optional Gmail sync, and one geocoding request to
OpenStreetMap's Nominatim if your home city isn't in the built-in table. The public
site has no analytics; the map tiles come from CARTO/OpenStreetMap.

## License

GPL-3.0. See [LICENSE](LICENSE).
