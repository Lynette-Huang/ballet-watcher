#!/usr/bin/env python3
"""
Watch Bellevue Classical Ballet (Acuity Scheduling) for open class slots
and alert via Slack when a wanted class has availability.

The booking site (bellevueclassicalballet.as.me) is a JS-rendered Acuity
scheduler, so we drive a headless Chromium via Playwright, let the class list
render, then parse the on-screen schedule. Each class renders as:

    Tuesday, August 11th, 2026
    7:30 PM
    Beginner 1 (Summer) with Bellevue Classical Ballet .
    1 hour 30 minutes @ $28.00
    BOOK
    No spots left        <-- full     ("N spots left" == open)

A small state.json remembers what was already open so you only get alerted the
moment a wanted slot flips from full -> open.

    DEBUG=1 python watch.py                      # dump rendered page for inspection
    EMAIL_USER=you@gmail.com EMAIL_PASS=app-password python watch.py   # normal run
"""
import datetime as dt
import json
import os
import pathlib
import re
import smtplib
import ssl
import sys
from email.message import EmailMessage

from playwright.sync_api import sync_playwright

# --------------------------------------------------------------------------
# Config: which classes to watch.
#
#   url      : the Acuity booking page. NOTE Acuity uses named slugs per term
#              (e.g. .../beginner1summer). The generic .../?appointmentType=ID
#              links are dormant ("scheduling not currently available") until a
#              term is published, so use the live slug. When the term changes
#              (summer -> fall), update this URL to the new slug.
#   weekdays : set of weekday ints to accept  (Mon=0 ... Sun=6)
#   min_hour : earliest class start hour to accept, 24h clock (17 == 5pm)
#   max_hour : latest start hour to accept (24 == no upper bound)
# --------------------------------------------------------------------------
TARGETS = [
    {
        "name": "Beginner 1",
        "url": "https://bellevueclassicalballet.as.me/beginner1summer",
        "weekdays": {0, 1, 2, 3, 4},   # Mon-Fri
        "min_hour": 17,                # 5pm or later
        "max_hour": 24,
    },
]

# The studio's public adult-program page. We watch it for schedule changes:
# new class sign-up links appearing or the posted schedule PDF changing name
# usually means a new term / updated schedule was published.
PROGRAM_URL = "https://www.bellevueclassicalballet.com/adult-program"

STATE_FILE = pathlib.Path(os.environ.get("STATE_FILE", "state.json"))
EMAIL_USER = os.environ.get("EMAIL_USER", "").strip()          # SMTP username / from address
EMAIL_PASS = os.environ.get("EMAIL_PASS", "").strip()          # SMTP password (Gmail app password)
EMAIL_TO = os.environ.get("EMAIL_TO", "").strip() or EMAIL_USER  # recipient (defaults to sender)
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com").strip()
SMTP_PORT = int(os.environ.get("SMTP_PORT", "465"))
DEBUG = os.environ.get("DEBUG") == "1"

_MONTHS = {
    m: i
    for i, m in enumerate(
        ["January", "February", "March", "April", "May", "June", "July",
         "August", "September", "October", "November", "December"],
        start=1,
    )
}
_DATE_RE = re.compile(
    r"^(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s+"
    r"([A-Z][a-z]+)\s+(\d{1,2})(?:st|nd|rd|th),\s+(\d{4})$"
)
_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})\s*(AM|PM)$", re.I)
_SPOTS_RE = re.compile(r"^(\d+)\s+spots?\s+left$", re.I)
_NO_SPOTS_RE = re.compile(r"^no\s+spots?\s+left$", re.I)


def log(*a):
    print(*a, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------
# Email (SMTP)
# --------------------------------------------------------------------------
def send_email(subject, body):
    if not (EMAIL_USER and EMAIL_PASS and EMAIL_TO):
        log("[warn] EMAIL_USER / EMAIL_PASS / EMAIL_TO not set - would have sent:\n"
            f"Subject: {subject}\n\n{body}")
        return
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = EMAIL_USER
    msg["To"] = EMAIL_TO
    msg.set_content(body)
    try:
        ctx = ssl.create_default_context()
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=ctx, timeout=20) as s:
            s.login(EMAIL_USER, EMAIL_PASS)
            s.send_message(msg)
        log(f"[email] sent to {EMAIL_TO}")
    except Exception as e:  # noqa: BLE001
        log(f"[error] email send failed: {e}")


# --------------------------------------------------------------------------
# Parse the rendered schedule text into [{start: datetime, seats: int}]
# --------------------------------------------------------------------------
def parse_page_text(text):
    slots = []
    cur_date = None       # (year, month, day)
    pending = None        # datetime awaiting its "... spots left" line
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        m = _DATE_RE.match(line)
        if m:
            mon, day, year = m.group(1), int(m.group(2)), int(m.group(3))
            cur_date = (year, _MONTHS.get(mon), day)
            pending = None
            continue
        m = _TIME_RE.match(line)
        if m and cur_date and cur_date[1]:
            hh, mm, ap = int(m.group(1)), int(m.group(2)), m.group(3).upper()
            if ap == "PM" and hh != 12:
                hh += 12
            if ap == "AM" and hh == 12:
                hh = 0
            pending = dt.datetime(cur_date[0], cur_date[1], cur_date[2], hh, mm)
            continue
        if pending is not None:
            if _NO_SPOTS_RE.match(line):
                slots.append({"start": pending, "seats": 0})
                pending = None
            else:
                sm = _SPOTS_RE.match(line)
                if sm:
                    slots.append({"start": pending, "seats": int(sm.group(1))})
                    pending = None
    return slots


def wanted(slot, target):
    start = slot["start"]
    return (
        start.weekday() in target["weekdays"]
        and target["min_hour"] <= start.hour < target["max_hour"]
    )


# --------------------------------------------------------------------------
# Browser
# --------------------------------------------------------------------------
def scrape(target):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        log(f"[nav] {target['name']}: {target['url']}")
        page.goto(target["url"], wait_until="networkidle", timeout=60_000)
        page.wait_for_timeout(3000)  # let lazy class list settle
        page_text = page.inner_text("body")
        browser.close()

    if DEBUG:
        dbg = pathlib.Path(f"debug-{re.sub(r'[^a-z0-9]+', '-', target['name'].lower())}.txt")
        dbg.write_text(page_text)
        log(f"[debug] wrote rendered page to {dbg}")

    if "not currently available" in page_text.lower():
        log(f"[warn] {target['name']}: Acuity reports scheduling not currently "
            f"available for this URL (term may have changed / slug outdated).")

    return parse_page_text(page_text)


def scrape_program():
    """Signature (sorted list) of the adult-program page's booking links and
    schedule PDFs. A change means the studio published a new schedule/term.
    Returns None on failure (so we don't false-alarm on a transient hiccup)."""
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(PROGRAM_URL, wait_until="networkidle", timeout=60_000)
            page.wait_for_timeout(1500)
            hrefs = page.eval_on_selector_all(
                "a[href]", "els => els.map(e => e.getAttribute('href'))"
            )
            browser.close()
    except Exception as e:  # noqa: BLE001
        log(f"[warn] program page scrape failed: {e}")
        return None
    sig = set()
    for h in hrefs or []:
        if not h:
            continue
        h = h.strip()
        low = h.lower()
        if "as.me" in low or "acuityscheduling.com" in low or low.endswith(".pdf"):
            sig.add(h)
    if not sig:
        log("[warn] program page: no booking links found (page structure changed?)")
        return None
    return sorted(sig)


# --------------------------------------------------------------------------
# State (so we don't re-alert every run while a slot stays open)
# --------------------------------------------------------------------------
def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:  # noqa: BLE001
            pass
    return {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))


# --------------------------------------------------------------------------
def main():
    raw = load_state()
    # Migrate old flat {slot-key: bool} format to the structured one.
    if raw and not any(k in raw for k in ("slots", "patterns", "program")):
        raw = {"slots": raw}
    slot_state = raw.get("slots", {})
    pattern_state = raw.get("patterns", {})
    program_state = raw.get("program")

    new_slots = {}
    new_patterns = {}
    open_alerts = []      # a wanted class opened up
    change_alerts = []    # the schedule itself changed

    for target in TARGETS:
        slots = scrape(target)
        log(f"[{target['name']}] {len(slots)} class time(s) on page")

        # (1) Spot opened up in a wanted slot.
        for slot in slots:
            if not wanted(slot, target):
                continue
            key = f"{target['name']}|{slot['start'].isoformat()}"
            open_now = slot["seats"] > 0
            new_slots[key] = open_now
            was_open = slot_state.get(key, False)
            log(f"  wanted: {slot['start']:%a %b %d %I:%M %p} -> {slot['seats']} spot(s)")
            if open_now and not was_open:
                open_alerts.append(
                    f"{target['name']} has {slot['seats']} spot(s): "
                    f"{slot['start']:%A %b %-d, %-I:%M %p}\n{target['url']}"
                )

        # (2) Recurring class times added/removed (schedule change).
        if slots:
            patterns = sorted({f"{s['start']:%a %-I:%M %p}" for s in slots})
            new_patterns[target["name"]] = patterns
            prev = pattern_state.get(target["name"])
            if prev is not None and set(patterns) != set(prev):
                added = [p for p in patterns if p not in prev]
                removed = [p for p in prev if p not in patterns]
                parts = []
                if added:
                    parts.append("New times: " + ", ".join(added))
                if removed:
                    parts.append("No longer listed: " + ", ".join(removed))
                change_alerts.append(
                    f"{target['name']} class times changed.\n"
                    + "\n".join(parts) + f"\n{target['url']}"
                )
        else:
            # Empty (transient failure or nothing offered) -> keep old patterns.
            new_patterns[target["name"]] = pattern_state.get(target["name"], [])

    # (3) Program page links / schedule PDF changed (new term published).
    prog_sig = scrape_program()
    if prog_sig is None:
        new_program = program_state
    else:
        new_program = prog_sig
        if program_state is not None and set(prog_sig) != set(program_state):
            added = [x for x in prog_sig if x not in program_state]
            removed = [x for x in program_state if x not in prog_sig]
            parts = []
            if added:
                parts.append("New / changed links:\n  " + "\n  ".join(added))
            if removed:
                parts.append("Gone:\n  " + "\n  ".join(removed))
            change_alerts.append(
                "Adult-program page changed (possible new schedule / term):\n"
                + "\n".join(parts) + f"\n{PROGRAM_URL}"
            )

    # Keep state for wanted slots we still track but didn't see this run.
    for k, v in slot_state.items():
        new_slots.setdefault(k, v)
    save_state({"slots": new_slots, "patterns": new_patterns, "program": new_program})

    if open_alerts:
        send_email("🩰 Ballet class opening!", "\n\n".join(open_alerts))
    if change_alerts:
        send_email("🗓️ Ballet schedule updated", "\n\n".join(change_alerts))
    if not open_alerts and not change_alerts:
        log("[done] no newly-open slots and no schedule changes")


if __name__ == "__main__":
    main()
