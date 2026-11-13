"""
Daily CricClubs scorecard scraper.

Finds the highest match_id already in Supabase, then fetches scorecards from
that +1 upward, saving each to responses/scorecard_match<ID>.json.
Also re-fetches the last REFRESH_LAST match_ids already in the DB, in case
they were still in progress when last scraped.

Stops when any of these is hit:
  - MAX_CONSECUTIVE_MISSES new IDs in a row return no data (we've passed the newest match)
  - MAX_REQUESTS_PER_RUN requests made (protects the 500/day quota)
  - an HTTP 429 (rate limited)

Required env vars:
  CRICCLUBS_API_KEY, CRICCLUBS_CONSUMER_KEY, SUPABASE_URL, SUPABASE_KEY
"""

import json
import os
import time

import requests
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

BASE_URL = "https://core-prod-origin.cricclubs.com/core/scoreCard/getScoreCard"
CLUB_ID = os.environ.get("CRICCLUBS_CLUB_ID", "7605")
X_API_KEY = os.environ["CRICCLUBS_API_KEY"]
X_CONSUMER_KEY = os.environ["CRICCLUBS_CONSUMER_KEY"]
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]

DELAY_BETWEEN_REQUESTS_SECONDS = 1
MAX_REQUESTS_PER_RUN = 400        # quota is 500/day; leave headroom
MAX_CONSECUTIVE_MISSES = 15       # stop after this many empty new IDs in a row
REFRESH_LAST = 5                  # re-fetch the latest N match_ids already in the DB

HEADERS = {
    "x-api-key": X_API_KEY,
    "x-consumer-key": X_CONSUMER_KEY,
}

RESPONSES_DIR = "responses"
os.makedirs(RESPONSES_DIR, exist_ok=True)


def get_recent_match_ids(supabase, n):
    """Return up to n distinct match_ids from the DB, highest first."""
    resp = (
        supabase.table("tca_db_scorecard_batting_v2")
        .select("match_id")
        .order("match_id", desc=True)
        .limit(1000)
        .execute()
    )
    ids = []
    for row in resp.data:
        if row["match_id"] not in ids:
            ids.append(row["match_id"])
        if len(ids) == n:
            break
    return ids


def fetch_match(match_id):
    params = {"matchId": str(match_id), "clubId": CLUB_ID}
    return requests.get(BASE_URL, params=params, headers=HEADERS, timeout=10)


def main():
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

    recent = get_recent_match_ids(supabase, REFRESH_LAST)
    if not recent:
        raise SystemExit("No match_ids in the DB yet. Seed the table first.")

    highest = recent[0]
    print(f"Highest match_id in DB: {highest}")

    refresh_ids = sorted(recent)
    print(f"Refreshing: {refresh_ids}")

    requests_made = 0
    saved = 0
    misses = 0
    match_id = highest

    def process(mid):
        """Returns True if saved, False if no data. Raises StopIteration on 429."""
        nonlocal requests_made
        requests_made += 1
        try:
            r = fetch_match(mid)
        except requests.RequestException as e:
            print(f"[match {mid}] request error: {e}")
            return False

        if r.status_code == 429:
            print("Rate limited (429). Stopping.")
            raise StopIteration
        if r.status_code != 200:
            print(f"[match {mid}] status={r.status_code}")
            return False

        data = r.json()
        if data.get("responseState") is False or data.get("errorMessage"):
            print(f"[match {mid}] no data: {data.get('errorMessage')}")
            return False

        with open(f"{RESPONSES_DIR}/scorecard_match{mid}.json", "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        print(f"[match {mid}] saved")
        return True

    try:
        # 1) refresh recent matches
        for mid in refresh_ids:
            if process(mid):
                saved += 1
            time.sleep(DELAY_BETWEEN_REQUESTS_SECONDS)

        # 2) walk forward from highest + 1
        while requests_made < MAX_REQUESTS_PER_RUN and misses < MAX_CONSECUTIVE_MISSES:
            match_id += 1
            if process(match_id):
                saved += 1
                misses = 0
            else:
                misses += 1
            time.sleep(DELAY_BETWEEN_REQUESTS_SECONDS)
    except StopIteration:
        pass

    print("---")
    print(f"Requests made: {requests_made}, files saved: {saved}, last ID tried: {match_id}")
    if misses >= MAX_CONSECUTIVE_MISSES:
        print(f"Stopped after {MAX_CONSECUTIVE_MISSES} consecutive empty IDs (caught up).")
    elif requests_made >= MAX_REQUESTS_PER_RUN:
        print("Stopped at the per-run request cap; the next run will continue.")


if __name__ == "__main__":
    main()