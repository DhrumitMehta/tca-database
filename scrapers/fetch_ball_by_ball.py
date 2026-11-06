"""
Fetches getBallByBall responses for a range of match IDs and saves each to
responses_ballbyball/ballbyball_matchN.json.

Same pattern as your scorecard fetcher: skips matches already saved, so it's
safe to re-run / resume across days if you hit the rate limit.

Usage:
    python fetch_ball_by_ball.py
"""

import json
import os
import time

import requests

BASE_URL = "https://core-prod-origin.cricclubs.com/core/scoreCard/getBallByBall"
CLUB_ID = "7605"
X_API_KEY = "Tan@!75za"          # <-- same key as your scorecard fetcher
X_CONSUMER_KEY = "Tan#$72za5"    # <-- same key as your scorecard fetcher

OUT_DIR = "responses_ballbyball"

MATCH_ID_START = 5001
MATCH_ID_END = 5500  # inclusive — adjust to your actual known match ID range

# RATE LIMIT: same 500 requests/day constraint as your scorecard fetcher.
# This range covers thousands of match IDs, so you will need multiple days
# (or a higher-tier key) to get through it all. already_fetched() below
# makes it safe to stop and resume.
DELAY_BETWEEN_REQUESTS_SECONDS = 1

HEADERS = {
    "x-api-key": X_API_KEY,
    "x-consumer-key": X_CONSUMER_KEY,
}

os.makedirs(OUT_DIR, exist_ok=True)


def already_fetched(match_id):
    return os.path.exists(f"{OUT_DIR}/ballbyball_match{match_id}.json")


def fetch_match(match_id):
    params = {"matchId": str(match_id), "clubId": CLUB_ID}
    r = requests.get(BASE_URL, params=params, headers=HEADERS, timeout=10)
    return r


def main():
    fetched = 0
    skipped = 0
    failed = []

    for match_id in range(MATCH_ID_START, MATCH_ID_END + 1):
        if already_fetched(match_id):
            skipped += 1
            continue

        try:
            r = fetch_match(match_id)
        except requests.RequestException as e:
            print(f"[match {match_id}] request error: {e}")
            failed.append(match_id)
            continue

        if r.status_code != 200:
            print(f"[match {match_id}] status={r.status_code} (skipping, not saved)")
            failed.append(match_id)
            continue

        data = r.json()

        if data.get("responseState") is False or data.get("errorMessage"):
            print(f"[match {match_id}] no data: {data.get('errorMessage')}")
            failed.append(match_id)
            continue

        out_path = f"{OUT_DIR}/ballbyball_match{match_id}.json"
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

        fetched += 1
        print(f"[match {match_id}] saved ({fetched} fetched so far)")

        time.sleep(DELAY_BETWEEN_REQUESTS_SECONDS)

    print("---")
    print(f"Fetched: {fetched}, skipped (already had): {skipped}, failed/empty: {len(failed)}")
    if failed:
        print(f"Failed/empty match IDs: {failed}")


if __name__ == "__main__":
    main()