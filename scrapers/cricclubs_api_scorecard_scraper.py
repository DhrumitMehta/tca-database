import json
import os
import time

import requests

BASE_URL = "https://core-prod-origin.cricclubs.com/core/scoreCard/getScoreCard"
CLUB_ID = "7605"
X_API_KEY = "Tan@!75za"            # <-- paste your x-api-key here
X_CONSUMER_KEY = "Tan#$72za5"  # <-- paste your x-consumer-key here

MATCH_ID_START = 7050
MATCH_ID_END = 7100  # inclusive

"""
usage: python cricclubs_api_scorecard_scraper.py
"""

# RATE LIMIT: 500 requests/day on this key. This range is exactly 500
# match IDs, so this alone would use the ENTIRE daily quota with nothing
# left over for retries or other work. Consider narrowing the range or
# running this across multiple days if you need headroom.
DELAY_BETWEEN_REQUESTS_SECONDS = 1

HEADERS = {
    "x-api-key": X_API_KEY,
    "x-consumer-key": X_CONSUMER_KEY,
}

os.makedirs("responses", exist_ok=True)


def already_fetched(match_id):
    return os.path.exists(f"responses/scorecard_match{match_id}.json")


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

        # CricClubs returns 200 with responseState=false / an errorMessage
        # for match IDs that don't exist or belong to other clubs.
        if data.get("responseState") is False or data.get("errorMessage"):
            print(f"[match {match_id}] no data: {data.get('errorMessage')}")
            failed.append(match_id)
            continue

        out_path = f"responses/scorecard_match{match_id}.json"
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