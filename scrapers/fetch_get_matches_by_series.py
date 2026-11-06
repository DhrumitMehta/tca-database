"""
Updates matches for EXISTING series after checking for new series.

For every series we already have a file for (responses_matches/matches_series{id}.json):
  1. Call getSeriesDetails (cheap) to check status.
     - status == "COMPLETED" (and not stale-overridden) -> CLOSED -> skip, no getMatches call.
     - anything else ("ONGOING", unrecognized, or details call failed) -> OPEN -> re-fetch via
       getMatches and overwrite the saved file.
  2. New series (ids with no saved file) are discovered.

Daily request budget:
  - This script enforces a hard cap (REQUEST_BUDGET, default 500) on the total number of HTTP
    requests it makes in a single run, counting BOTH getSeriesDetails and getMatches calls.
  - Series are processed in ascending order. Once the budget would be exceeded, the run stops
    early and reports which series were not yet checked, so you can resume next run/day.
  - Worst case is 2 requests per open series (1 details + 1 matches) and 1 request per closed
    series (details only, once we know it's closed we stop there).

Usage:
    python fetch_get_matches_by_series.py
    python fetch_get_matches_by_series.py --inspect-status
        # prints the getSeriesDetails status for every already-saved series, without making
        # any getMatches calls or writing any files. Still counts against REQUEST_BUDGET.
    python fetch_get_matches_by_series.py --budget 200
        # override the request budget for this run
"""

import argparse
import json
import os
import re
import time
from datetime import datetime, timedelta

import requests

MATCHES_URL = "https://core-prod-origin.cricclubs.com/core/match/getMatches"
SERIES_DETAILS_URL = "https://core-prod-origin.cricclubs.com/core/series/getSeriesDetails"

CLUB_ID = "7605"
X_API_KEY = "Tan@!75za"          # <-- same key as your scorecard fetcher
X_CONSUMER_KEY = "Tan#$72za5"    # <-- same key as your scorecard fetcher

OUT_DIR = "responses_matches"

LIMIT = 500  # per-series match cap; a series is unlikely to have more than this

DELAY_BETWEEN_REQUESTS_SECONDS = 1

MAX_CONSECUTIVE_MISSES = 5  # stop probing after this many IDs in a row that don't exist for this club

# Hard cap on total HTTP requests (getSeriesDetails + getMatches combined) made in one run.
REQUEST_BUDGET = 500

HEADERS = {
    "x-api-key": X_API_KEY,
    "x-consumer-key": X_CONSUMER_KEY,
}

# Confirmed via getSeriesDetails: data.status == "COMPLETED" for finished series,
# "ONGOING" for active ones. Anything not in this "closed" set is treated as still-open
# (conservative default: re-fetch rather than miss new matches) - UNLESS the age override
# below kicks in.
CLOSED_STATUS_VALUES = {"COMPLETED"}

# Fallback override: if a series isn't marked "COMPLETED" but its startDate/endDate
# (whichever is more recent / present) is older than this, treat it as closed anyway.
# Catches stale "ONGOING" data-entry (e.g. a series someone forgot to mark finished)
# without relying on status alone.
STALE_SERIES_AGE = timedelta(days=365)

DATE_FORMATS = ["%m/%d/%Y", "%Y-%m-%d"]

os.makedirs(OUT_DIR, exist_ok=True)

FILENAME_RE = re.compile(r"^matches_series(\d+)\.json$")

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "series_state.json")

def load_state():
    state = {"closed": {}}
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                raw = json.load(f)
            closed = raw.get("closed", {})
            if isinstance(closed, list):  # migrate old format: [101, 102] -> {"101": None, ...}
                closed = {str(i): None for i in closed}
            state["closed"] = {str(k): v for k, v in closed.items()}
        except (ValueError, OSError) as e:
            print(f"Could not read {STATE_FILE} ({e}); starting with empty state.")
    return state


def save_state(state):
    ordered = {k: state["closed"][k] for k in sorted(state["closed"], key=int)}
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"closed": ordered}, f, indent=2, ensure_ascii=False)
    os.replace(tmp, STATE_FILE)


def get_series_name(series_id):
    """Reads seriesName from the saved matches file. Returns None if unavailable
    (e.g. the series has no matches yet)."""
    path = f"{OUT_DIR}/matches_series{series_id}.json"
    try:
        with open(path, "r", encoding="utf-8") as f:
            matches = json.load(f).get("data") or []
        for m in matches:
            if m.get("seriesName"):
                return m["seriesName"]
    except (OSError, ValueError):
        pass
    return None

class BudgetExhausted(Exception):
    """Raised internally when the request budget would be exceeded."""


class RequestCounter:
    def __init__(self, budget):
        self.budget = budget
        self.used = 0

    def spend(self, n=1):
        if self.used + n > self.budget:
            raise BudgetExhausted()
        self.used += n


def parse_series_date(value):
    """Best-effort parse of startDate/endDate strings like '10/21/2018'."""
    if not value:
        return None
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def series_is_stale_by_date(details_data):
    """
    Returns True if the series' most relevant date (endDate if present, else startDate)
    is older than STALE_SERIES_AGE. Returns False if no usable date is found (so the
    status field remains the deciding factor).
    """
    end_date = parse_series_date(details_data.get("endDate"))
    start_date = parse_series_date(details_data.get("startDate"))
    reference_date = end_date or start_date
    if reference_date is None:
        return False
    return (datetime.now() - reference_date) > STALE_SERIES_AGE


def get_saved_series_ids():
    """Return the sorted list of seriesIds we already have a matches file for."""
    ids = set()
    for fname in os.listdir(OUT_DIR):
        m = FILENAME_RE.match(fname)
        if m:
            ids.add(int(m.group(1)))
    return sorted(ids)


def fetch_series_details(series_id, counter):
    """
    Calls getSeriesDetails for a series and returns the `data` dict (which includes
    "status", "startDate", "endDate", etc.), or None if the call failed / response
    malformed (treated as "unknown -> re-fetch to be safe" by the caller).

    Raises BudgetExhausted if this call would exceed the request budget (raised BEFORE
    the request is made).
    """
    counter.spend(1)

    params = {
        "clubId": CLUB_ID,
        "seriesId": str(series_id),
        "X-Auth-Token": "",  # required empty param, confirmed via testing
    }
    try:
        r = requests.get(SERIES_DETAILS_URL, params=params, headers=HEADERS, timeout=10)
    except requests.RequestException as e:
        print(f"[series {series_id}] getSeriesDetails request error: {e}")
        return None

    if r.status_code != 200:
        print(f"[series {series_id}] getSeriesDetails status={r.status_code}")
        return None

    try:
        data = r.json()
    except ValueError:
        print(f"[series {series_id}] getSeriesDetails returned non-JSON")
        return None

    if data.get("responseState") is False or data.get("errorMessage"):
        print(f"[series {series_id}] getSeriesDetails error: {data.get('errorMessage')}")
        return None

    return data.get("data", {})


def classify_series(series_id, counter):
    """
    Checks getSeriesDetails for one series and classifies it as "closed" or "open".

    Decision order:
      1. status == "COMPLETED" -> closed.
      2. Otherwise, if the series' start/end date is older than STALE_SERIES_AGE ->
         closed anyway (catches stale "ONGOING" entries that were probably just never
         marked finished).
      3. Otherwise -> open (status is "ONGOING"/unknown and it's recent).
      4. If the details call itself failed (details is None) -> open, to be safe
         (we can't confirm it's closed, so don't risk skipping it).
    """
    details = fetch_series_details(series_id, counter)

    if details is None:
        print(f"[series {series_id}] details unavailable -> treating as OPEN")
        return "open"

    status = details.get("status")

    if status in CLOSED_STATUS_VALUES:
        print(f"[series {series_id}] status={status!r} -> CLOSED (skip)")
        return "closed"
    elif series_is_stale_by_date(details):
        print(f"[series {series_id}] status={status!r} but dates are >1yr old "
              f"(start={details.get('startDate')!r}, end={details.get('endDate')!r}) "
              f"-> treating as CLOSED anyway")
        return "closed"
    else:
        print(f"[series {series_id}] status={status!r} -> OPEN (will update)")
        return "open"


def fetch_matches(series_id, counter):
    counter.spend(1)
    params = {
        "clubId": CLUB_ID,
        "seriesId": str(series_id),
        "limit": LIMIT,
    }
    r = requests.get(MATCHES_URL, params=params, headers=HEADERS, timeout=10)
    return r


def update_series_matches(series_id, counter):
    """Re-fetches getMatches for one open series and overwrites its saved file."""
    try:
        r = fetch_matches(series_id, counter)
    except requests.RequestException as e:
        print(f"[series {series_id}] getMatches request error: {e}")
        return False

    if r.status_code != 200:
        print(f"[series {series_id}] getMatches status={r.status_code} (not updated)")
        return False

    data = r.json()

    if data.get("responseState") is False or data.get("errorMessage"):
        print(f"[series {series_id}] getMatches error: {data.get('errorMessage')}")
        return False

    matches = data.get("data") or []
    out_path = f"{OUT_DIR}/matches_series{series_id}.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

    print(f"[series {series_id}] updated, saved {len(matches)} matches")

    if len(matches) == LIMIT:
        print(
            f"  WARNING: series {series_id} returned exactly LIMIT ({LIMIT}) matches - "
            f"this series may have more than we captured. Consider raising LIMIT."
        )
    return True

CHECKPOINT_EVERY = 5  # print a progress checkpoint after this many new series are saved


def discover_new_series(counter, start_id):
    """Probe series IDs above start_id. For each ID that exists for this club,
    fetch its matches and save the file (written immediately). Stops after
    MAX_CONSECUTIVE_MISSES misses in a row, or when the request budget runs out."""
    discovered = []
    misses = 0
    series_id = start_id + 1

    while misses < MAX_CONSECUTIVE_MISSES:
        try:
            details = fetch_series_details(series_id, counter)
        except BudgetExhausted:
            print(f"\nBudget reached during discovery at series {series_id}. "
                  f"Resume next run.")
            break

        time.sleep(DELAY_BETWEEN_REQUESTS_SECONDS)

        if not details:  # None (error / not this club's series) or empty
            misses += 1
            series_id += 1
            continue

        misses = 0
        print(f"[series {series_id}] NEW series found (status={details.get('status')!r})")
        try:
            if update_series_matches(series_id, counter):
                discovered.append(series_id)
                if len(discovered) % CHECKPOINT_EVERY == 0:
                    print(f"  >> Checkpoint: {len(discovered)} new series saved so far "
                          f"(latest: {series_id}, requests used: {counter.used}/{counter.budget})")
        except BudgetExhausted:
            print(f"\nBudget reached before fetching matches for new series {series_id}.")
            break

        time.sleep(DELAY_BETWEEN_REQUESTS_SECONDS)
        series_id += 1

    return discovered

def run(counter, inspect_only=False):
    state = load_state()
    all_saved_ids = get_saved_series_ids()

    # Backfill names for closed series that don't have one yet (no requests needed).
    if not inspect_only:
        backfilled = 0
        for key, name in state["closed"].items():
            if not name:
                found = get_series_name(int(key))
                if found:
                    state["closed"][key] = found
                    backfilled += 1
        if backfilled:
            print(f"Backfilled names for {backfilled} closed series.")
            save_state(state)

    saved_ids = [s for s in all_saved_ids if str(s) not in state["closed"]]
    skipped_known_closed = len(all_saved_ids) - len(saved_ids)

    print(f"Found {len(all_saved_ids)} series files "
          f"({skipped_known_closed} already known closed, {len(saved_ids)} to check). "
          f"Budget: {counter.budget} requests.")

    new_series = []
    if not inspect_only:
        # Use ALL saved ids (not just open ones) as the discovery starting point.
        new_series = discover_new_series(counter, max(all_saved_ids, default=0))

    closed = []
    updated = []
    still_open_unchecked = []
    unprocessed = []

    try:
        for idx, series_id in enumerate(saved_ids):
            try:
                classification = classify_series(series_id, counter)
            except BudgetExhausted:
                unprocessed = saved_ids[idx:]
                print(f"\nRequest budget ({counter.budget}) reached during status checks. "
                      f"Stopping early.")
                break

            time.sleep(DELAY_BETWEEN_REQUESTS_SECONDS)

            if classification == "closed":
                closed.append(series_id)
                if not inspect_only:
                    state["closed"][str(series_id)] = get_series_name(series_id)
                    save_state(state)  # persist immediately so progress survives a crash
                continue

            if inspect_only:
                still_open_unchecked.append(series_id)
                continue

            try:
                if update_series_matches(series_id, counter):
                    updated.append(series_id)
            except BudgetExhausted:
                unprocessed = saved_ids[idx:]
                print(f"\nRequest budget ({counter.budget}) reached before updating series "
                      f"{series_id}. Stopping early.")
                break

            time.sleep(DELAY_BETWEEN_REQUESTS_SECONDS)
    finally:
        if not inspect_only:
            save_state(state)

    print("---")
    print(f"Requests used: {counter.used}/{counter.budget}")
    print(f"Skipped (already known closed, 0 requests): {skipped_known_closed}")
    if new_series:
        print(f"New series added: {new_series}")
    print(f"Newly closed this run: {len(closed)}")
    if inspect_only:
        print(f"Open (would update): {len(still_open_unchecked)}")
        if still_open_unchecked:
            print(f"Open series IDs: {still_open_unchecked}")
    else:
        print(f"Updated: {len(updated)}")
        if updated:
            print(f"Updated series IDs: {updated}")
    if unprocessed:
        print(f"Not yet checked this run (resume next time): {unprocessed}")

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--inspect-status", action="store_true",
                         help="Only print status for saved series; no getMatches calls, no writes.")
    parser.add_argument("--budget", type=int, default=REQUEST_BUDGET,
                         help=f"Max total HTTP requests this run (default {REQUEST_BUDGET}).")
    args = parser.parse_args()

    counter = RequestCounter(args.budget)
    run(counter, inspect_only=args.inspect_status)


if __name__ == "__main__":
    main()