"""
espn_draft_sync_spike.py

One-off validation spike (see PROJECTNOTES.md "ESPN Live Draft Sync —
Design Notes", Option A): does espn_api's League.draft reflect picks
made in an ESPN draft WHILE the draft is still in progress, or only
after it's marked complete? There's an open, unanswered GitHub issue
asking exactly this (cwendt94/espn-api#558) — this answers it directly
by polling every 5 seconds and printing whenever the pick count changes.

If picks show up within ~10-15s of being made in the UI: Option A
(polling the private API) works, build live sync on it.
If picks only appear after the draft ends: Option A is a dead end for
live sync, fall back to Option B (Playwright reading the draft room DOM).

Usage:
    python tests/espn_draft_sync_spike.py <league_id> [year]

Then make a pick in the ESPN mock draft UI and watch this output.

Auth: reads optional SWID/espn_s2 cookies from
tests/espn_credentials.local.json (gitignored — see .gitignore and
PROJECTNOTES.md's "Auth handling note"), only needed if the draft
requires them. Format:
    {"swid": "{...}", "espn_s2": "..."}
"""

import functools
import json
import sys
import time
from pathlib import Path

from espn_api.football import League

# Unbuffered — this polls in a loop and we want each line visible
# immediately, not batched up until the process exits.
print = functools.partial(print, flush=True)

CREDENTIALS_PATH = Path(__file__).parent / "espn_credentials.local.json"
POLL_INTERVAL_SECONDS = 5


def load_credentials() -> dict:
    if CREDENTIALS_PATH.exists():
        return json.loads(CREDENTIALS_PATH.read_text())
    return {}


def main():
    if len(sys.argv) < 2:
        print("Usage: python tests/espn_draft_sync_spike.py <league_id> [year]")
        sys.exit(1)

    league_id = int(sys.argv[1])
    year = int(sys.argv[2]) if len(sys.argv) > 2 else 2026

    creds = load_credentials()
    swid = creds.get("swid")
    espn_s2 = creds.get("espn_s2")
    if swid or espn_s2:
        print(f"Using auth cookies from {CREDENTIALS_PATH.name}")

    print(f"Polling league_id={league_id} year={year} every {POLL_INTERVAL_SECONDS}s. Ctrl+C to stop.")
    print("Make a pick in the ESPN draft room UI and watch for a change below.\n")

    last_count = None
    while True:
        try:
            league = League(league_id=league_id, year=year, swid=swid, espn_s2=espn_s2)
            picks = league.draft
            count = len(picks)
        except Exception as e:
            print(f"  [{time.strftime('%H:%M:%S')}] Error polling: {e}")
            time.sleep(POLL_INTERVAL_SECONDS)
            continue

        timestamp = time.strftime("%H:%M:%S")
        if last_count is None:
            print(f"[{timestamp}] Initial pick count: {count}")
        elif count != last_count:
            print(f"[{timestamp}] *** Pick count changed: {last_count} -> {count} ***")
            if picks:
                latest = picks[-1]
                print(f"  Latest pick: {latest}")
        else:
            print(f"[{timestamp}] no change ({count} picks)")

        last_count = count
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
