"""
Reads every responses_matches/matches_series*.json file (from
fetch_get_matches_by_series.py), dedupes matches by matchId, and upserts
them into the Supabase table tca_db_match_info_v2.

This is a separate script from matches_to_excel.py — it does NOT touch
that file or its Excel report output. Both scripts share the same
load/dedupe logic (later files win on a matchId conflict) since a given
match should be identical regardless of which series query surfaced it.

Usage:
    python matches_to_supabase.py
"""

import glob
import json
import os
from datetime import datetime

from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]  # service key needed to write

RESPONSES_DIR = "responses_matches"
TABLE_NAME = "tca_db_match_info_v2"
BATCH_SIZE = 500  # rows per upsert call


def load_all_matches():
    """Same load/dedupe logic as matches_to_excel.py: later files win on a
    matchId conflict, since a given match should be identical regardless
    of which series query surfaced it."""
    files = sorted(glob.glob(os.path.join(RESPONSES_DIR, "matches_series*.json")))
    print(f"Found {len(files)} series batch files in {RESPONSES_DIR}/")

    matches_by_id = {}
    for path in files:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)

        # Guard against empty files, "null" JSON, or "data": null
        if not isinstance(raw, dict):
            print(f"  Skipping {os.path.basename(path)}: unexpected content ({type(raw).__name__})")
            continue

        data = raw.get("data") or []
        if not data:
            print(f"  Skipping {os.path.basename(path)}: no matches in 'data'")
            continue

        for match in data:
            match_id = match.get("matchId")
            if match_id is None:
                continue
            matches_by_id[match_id] = match

    return matches_by_id


def parse_match_date(date_str):
    """JSON dates come as MM/DD/YYYY strings."""
    if not date_str:
        return None
    try:
        return datetime.strptime(date_str, "%m/%d/%Y").date().isoformat()
    except ValueError:
        print(f"  Warning: couldn't parse matchDate '{date_str}'")
        return None


def parse_last_updated(date_str):
    """lastUpdatedDate comes as ISO-8601 with milliseconds and offset,
    e.g. '2018-12-02T15:46:47.000+00:00' — already a format Postgres/
    Supabase can parse directly, so just pass it through."""
    return date_str or None


def to_bool(value):
    """JSON uses 0/1 (and sometimes real booleans) for flag fields."""
    if value is None:
        return None
    return bool(value)


def map_match_to_row(match):
    team_one_id = match.get("teamOne")
    team_two_id = match.get("teamTwo")
    winner_id = match.get("winner")

    winner_name = None
    points_team_1 = None
    points_team_2 = None
    if winner_id is not None:
        if winner_id == team_one_id:
            winner_name = match.get("teamOneName")
            points_team_1, points_team_2 = 2, 0
        elif winner_id == team_two_id:
            winner_name = match.get("teamTwoName")
            points_team_1, points_team_2 = 0, 2

    return {
        "match_id": match.get("matchId"),
        "team_one_id": team_one_id,
        "team_one_name": match.get("teamOneName"),
        "team_one_code": match.get("teamOneCode"),
        "team_two_id": team_two_id,
        "team_two_name": match.get("teamTwoName"),
        "team_two_code": match.get("teamTwoCode"),
        "overs": match.get("overs"),
        "match_date": parse_match_date(match.get("matchDate")),
        "team_one_total": match.get("t1total"),
        "team_two_total": match.get("t2total"),
        "team_one_balls": match.get("t1balls"),
        "team_two_balls": match.get("t2balls"),
        "team_one_wickets": match.get("t1wickets"),
        "team_two_wickets": match.get("t2wickets"),
        "match_type": match.get("matchType"),
        "is_complete": to_bool(match.get("isComplete")),
        "result": match.get("result"),
        "winner_team_id": winner_id,
        "winner_team_name": winner_name,
        "no_of_balls_per_over": match.get("noOfBallsPerOver"),
        "is_dls": to_bool(match.get("isDls")),
        "is_followon": to_bool(match.get("isFollowon")),
        "series_type": match.get("seriesType"),
        "series_name": match.get("seriesName"),
        "club_name": match.get("clubName"),
        "club_id": match.get("clubId"),
        "location": match.get("location"),
        "t2_revised_overs": match.get("t2RevisedOvers"),
        "last_updated_date": parse_last_updated(match.get("lastUpdatedDate")),
        "points_team_1": points_team_1,
        "points_team_2": points_team_2,
    }


def chunked(iterable, size):
    for i in range(0, len(iterable), size):
        yield iterable[i:i + size]


def upload_to_supabase(supabase, rows):
    total = len(rows)
    print(f"Upserting {total} rows into '{TABLE_NAME}' in batches of {BATCH_SIZE}...")

    for i, batch in enumerate(chunked(rows, BATCH_SIZE), start=1):
        supabase.table(TABLE_NAME).upsert(batch, on_conflict="match_id").execute()
        print(f"  Batch {i}: {len(batch)} rows upserted")

    print("Done.")


def main():
    matches_by_id = load_all_matches()
    if not matches_by_id:
        print("No matches found — check that fetch_get_matches_by_series.py has run "
              f"and populated {RESPONSES_DIR}/")
        return

    print(f"Total unique matches: {len(matches_by_id)}")

    rows = [map_match_to_row(m) for m in matches_by_id.values()]

    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    upload_to_supabase(supabase, rows)


if __name__ == "__main__":
    main()