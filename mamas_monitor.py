#!/usr/bin/env python3
"""
Mama's Fish House Reservation Availability Monitor
---------------------------------------------------
Polls SevenRooms for table availability on the configured date(s) and sends
an email the moment a slot opens up. Tracks notified slots in a small JSON
file so you don't get spammed.

Setup:
    pip install requests
    Set environment variables (see README.md): EMAIL_FROM, EMAIL_TO, EMAIL_PASSWORD

Run:
    python mamas_monitor.py            # long-running loop
    python mamas_monitor.py --once     # single check (good for cron)
"""

import argparse
import json
import logging
import os
import smtplib
import sys
import time
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import requests

# ========== CONFIGURATION ==========
VENUE = "mamasfishhouserestaurantinn"
TARGET_DATES = ["2026-06-05","2026-06-07"]           # YYYY-MM-DD; add more dates if you want
PARTY_SIZE = 2
POLL_INTERVAL_SECONDS = 300              # 5 minutes — be polite to their API
STATE_FILE = "notified_slots.json"       # remembers what we've already alerted on

# Email — set via environment variables, not hardcoded
EMAIL_FROM     = os.environ.get("EMAIL_FROM", "")
EMAIL_TO       = os.environ.get("EMAIL_TO", "")
EMAIL_PASSWORD = os.environ.get("EMAIL_PASSWORD", "")
SMTP_HOST      = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT      = int(os.environ.get("SMTP_PORT", "587"))
# ===================================

API_URL = "https://www.sevenrooms.com/api-yoa/availability/ng/widget/range"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("mamas")


def fetch_availability(date: str) -> dict:
    """Hit the SevenRooms availability endpoint for a single date."""
    params = {
        "venue": VENUE,
        "party_size": PARTY_SIZE,
        "halo_size_interval": 100,
        "start_date": date,
        "num_days": 1,
        "channel": "SEVENROOMS_WIDGET",
        "exclude_pdr": "true",
        "actual_id": "",
    }
    headers = {
        "accept": "*/*",
        "accept-language": "en-US,en;q=0.9",
        "cache-control": "no-cache",
        "pragma": "no-cache",
        "referer": f"https://www.sevenrooms.com/explore/{VENUE}/reservations/create/search?date={date}",
        "user-agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
        ),
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
    }
    resp = requests.get(API_URL, params=params, headers=headers, timeout=30)
    resp.raise_for_status()
    return resp.json()


def parse_slots(data: dict, requested_date: str) -> list:
    """
    Extract genuinely-bookable time slots from the SevenRooms response.

    Real structure:
        data.availability[<date>] -> list of SHIFTS (LUNCH/DINNER/etc.)
        each shift has a `times` list of actual time slots.

    A time slot is truly available only when:
        - type == "book"                                    (one-click bookable)
        - OR type == "request" AND is_requestable is True   (accepting requests)

    type == "request" with is_requestable == False is a PLACEHOLDER for
    "no availability" — SevenRooms returns these even when fully booked.
    """
    slots = []
    avail = (data.get("data") or {}).get("availability") or {}
    # Look up only the exact date we asked for. SevenRooms sometimes silently
    # returns data for a different date when the requested one is closed.
    shifts = avail.get(requested_date) or []
    for shift in shifts:
        if shift.get("is_closed") or shift.get("is_forced_empty_availability"):
            continue
        shift_label = shift.get("shift_category") or shift.get("name") or ""
        for t in shift.get("times") or []:
            slot_type = t.get("type")
            is_requestable = bool(t.get("is_requestable"))
            if slot_type == "book" or (slot_type == "request" and is_requestable):
                slots.append({
                    "time":     t.get("time") or "",            # "5:30 PM" — for display
                    "time_iso": t.get("time_iso") or "",        # "2026-06-05 17:30:00" — unique key
                    "type":     slot_type,
                    "label":    shift_label,
                })
    return slots


def load_state() -> set:
    p = Path(STATE_FILE)
    if not p.exists():
        return set()
    try:
        return set(json.loads(p.read_text()))
    except Exception:
        log.warning("State file unreadable, starting fresh")
        return set()


def save_state(state: set) -> None:
    Path(STATE_FILE).write_text(json.dumps(sorted(state)))


def send_email(subject: str, body: str) -> None:
    if not (EMAIL_FROM and EMAIL_TO and EMAIL_PASSWORD):
        log.error("Email not configured — set EMAIL_FROM, EMAIL_TO, EMAIL_PASSWORD")
        log.info("Would have sent:\nSubject: %s\n\n%s", subject, body)
        return
    msg = MIMEMultipart()
    msg["From"] = EMAIL_FROM
    msg["To"] = EMAIL_TO
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
        s.starttls()
        s.login(EMAIL_FROM, EMAIL_PASSWORD)
        s.send_message(msg)
    log.info("Email sent to %s", EMAIL_TO)


def check_once(state: set) -> set:
    new_found = []
    for date in TARGET_DATES:
        try:
            data = fetch_availability(date)
        except Exception as e:
            log.warning("Fetch failed for %s: %s", date, e)
            continue
        slots = parse_slots(data, date)
        log.info("%s — %d slot(s) detected", date, len(slots))
        for s in slots:
            # Unique key per (date, exact ISO time, type) — avoids dupe emails.
            key = f"{date}|{s['time_iso']}|{s['type']}"
            if key not in state:
                new_found.append((date, s))
                state.add(key)

    if new_found:
        lines = [
            f"🐟 Mama's Fish House — {len(new_found)} new slot(s) available "
            f"(party of {PARTY_SIZE}):",
            "",
        ]
        for date, s in new_found:
            badge = "BOOKABLE" if s["type"] == "book" else "REQUEST"
            extra = f" — {s['label']}" if s["label"] else ""
            lines.append(f"  • {date}  {s['time']}  [{badge}]{extra}")
        lines.append("")
        lines.append("Book here:")
        for date in TARGET_DATES:
            lines.append(
                f"  https://www.sevenrooms.com/explore/{VENUE}"
                f"/reservations/create/search?date={date}&party_size={PARTY_SIZE}"
            )
        body = "\n".join(lines)
        send_email(
            subject=f"🐟 Mama's Fish House: {len(new_found)} slot(s) opened!",
            body=body,
        )
    return state


def main():
    parser = argparse.ArgumentParser(description="Mama's Fish House availability monitor")
    parser.add_argument("--once", action="store_true", help="Run a single check and exit (for cron)")
    args = parser.parse_args()

    log.info(
        "Monitor starting — venue=%s party=%d dates=%s",
        VENUE, PARTY_SIZE, ", ".join(TARGET_DATES),
    )
    state = load_state()

    if args.once:
        state = check_once(state)
        save_state(state)
        return

    while True:
        try:
            state = check_once(state)
            save_state(state)
        except KeyboardInterrupt:
            log.info("Stopping (ctrl-c).")
            sys.exit(0)
        except Exception as e:
            log.exception("Unexpected error: %s", e)
        log.info("Sleeping %ds…", POLL_INTERVAL_SECONDS)
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
