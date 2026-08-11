# ballet-watcher

Watches [Bellevue Classical Ballet](https://www.bellevueclassicalballet.com/adult-program)
adult classes (booked through Acuity Scheduling) and emails you when a wanted
class has an open spot.

Currently watching: **Beginner 1**, Mon–Fri, 5:00 pm or later.
Edit `TARGETS` in `watch.py` to change or add classes.

## How it works

The booking page is a JavaScript-rendered Acuity scheduler, so a headless
Chromium (via Playwright) loads the class page and parses the on-screen
schedule (date / time / "N spots left" vs "No spots left"). Slots are filtered
by weekday/time and checked for availability. A tiny `state.json` remembers
what was already open so you only get alerted the moment a slot flips from full
to open.

## One-time setup

### 1. Email sender (Gmail app password)
Gmail needs an **app password** (not your normal password) for SMTP:
1. Turn on 2-Step Verification: <https://myaccount.google.com/security>
2. Create an app password: <https://myaccount.google.com/apppasswords> → name it
   `ballet-watcher` → copy the 16-character password.

Env vars used:
- `EMAIL_USER` — your Gmail address (also the SMTP login)
- `EMAIL_PASS` — the 16-char app password
- `EMAIL_TO` — where to send alerts (defaults to `EMAIL_USER` if omitted)

Non-Gmail works too: set `SMTP_HOST` / `SMTP_PORT` (SSL) for your provider.

### 2. Run it once locally
```bash
cd ballet-watcher
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium

DEBUG=1 EMAIL_USER='you@gmail.com' EMAIL_PASS='app-password' python watch.py
```
Check the `wanted:` log lines show the right classes and seat counts. Since the
weekday evening classes are usually full, no email is sent on a normal run —
that's expected (you only get mail when a slot opens).

### 3. Deploy to GitHub Actions (24/7, free)
1. Create a repo (a **public** repo gets unlimited Actions minutes; a private
   repo has a monthly cap, so bump the cron interval if you go private).
2. Push these files.
3. Repo **Settings → Secrets and variables → Actions** → add secrets:
   - `EMAIL_USER` = your Gmail address
   - `EMAIL_PASS` = your app password
   - `EMAIL_TO` = recipient (optional; defaults to `EMAIL_USER`)
4. **Actions** tab → enable workflows → run **ballet-watcher** once manually
   (`Run workflow`) to confirm it works.

It then runs every 10 minutes on its own.

> Note: GitHub disables scheduled workflows after 60 days of no repo activity —
> just push any commit to re-arm it. Scheduled runs can also be delayed a few
> minutes when GitHub is busy.

## Change what's watched
Edit `TARGETS` in `watch.py`. Each entry:
```python
{
    "name": "Beginner 1",
    "url": "https://bellevueclassicalballet.as.me/?appointmentType=82140860",
    "weekdays": {0, 1, 2, 3, 4},  # Mon=0 ... Sun=6
    "min_hour": 17,               # 5pm
    "max_hour": 24,               # no upper bound
}
```
appointmentType IDs for other classes are on the
[adult program page](https://www.bellevueclassicalballet.com/adult-program)
sign-up links (e.g. Beginner 2 = `82140981`, Intermediate = `82141042`).
