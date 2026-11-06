"""
Parses CricClubs API getScoreCard responses into rows for the
tca_db_scorecard_batting_v2 / tca_db_scorecard_bowling_v2 Supabase tables,
and upserts them.

Usage:
    # Single file:
    python scorecard_to_supabase.py responses/scorecard_match3200.json 3200

    # Every scorecard_match*.json in responses/:
    python scorecard_to_supabase.py --all

    # Re-upsert all files, ignoring what's already in the DB:
    python scorecard_to_supabase.py --all --full
"""

import argparse
import glob
import json
import os
import re

from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]  # service key needed to write

RESPONSES_DIR = "responses"
BATCH_SIZE = 500  # rows per upsert call, not API requests — Supabase, not CricClubs

REFRESH_WINDOW = 100  # re-process the most recent N match_ids already in the DB (may have been unfinished)


def get_existing_match_ids(supabase):
    """Return the set of match_ids already in the batting table.
    Paginates because Supabase caps each response at 1000 rows by default."""
    ids = set()
    page_size = 1000
    start = 0
    while True:
        resp = (
            supabase.table("tca_db_scorecard_batting_v2")
            .select("match_id")
            .order("match_id")
            .order("inning_number")
            .order("batter_id")  # stable ordering so pages don't overlap
            .range(start, start + page_size - 1)
            .execute()
        )
        rows = resp.data
        ids.update(r["match_id"] for r in rows)
        if len(rows) < page_size:
            break
        start += page_size
    return ids

def clean_name(first, last):
    return f"{first} {last}".strip()


# Known dismissal type codes -> readable labels. Codes not in this map
# (there are others we haven't seen yet) are left as-is, unmapped.
HOW_OUT_MAP = {
    "b": "bowled",
    "ct": "caught",
    "ctw": "caught_behind",
    "st": "stumped",
    "ro": "run_out",
    "lbw": "lbw",
}


def map_how_out(code, out_description=None):
    if not code:
        return None
    if code == "rt":
        desc = (out_description or "").lower()
        if "retired out" in desc:
            return "retired_out"
        if "retired hurt" in desc:
            return "retired_hurt"
        if "retired not out" in desc or "not out" in desc:
            return "retired_not_out"
        return "retired"  # description didn't clarify which kind
    return HOW_OUT_MAP.get(code, code)


def build_bowler_lookup(innings_data):
    """Map playerID (as string) -> full name, from this innings' bowling list."""
    lookup = {}
    for b in innings_data.get("bowling", []):
        lookup[str(b["playerID"])] = clean_name(b["firstName"], b["lastName"])
    return lookup


def parse_scorecard(raw_json, match_id):
    data = raw_json["data"]
    batting_rows = []
    bowling_rows = []

    for innings_num in [1, 2, 3, 4]:
        innings = data.get(f"innings{innings_num}")
        if not innings:
            continue

        bowler_lookup = build_bowler_lookup(innings)

        for pos, bat in enumerate(innings.get("batting", []), start=1):
            wicket_taker1 = bat.get("wicketTaker1") or None
            wicket_taker2 = bat.get("wicketTaker2") or None

            batting_rows.append({
                "match_id": match_id,
                "inning_number": innings_num,
                "batting_position": pos,
                "team_id": innings.get("teamId"),
                "batting_team": innings.get("teamName"),
                "batter_id": bat["playerID"],
                "batter_name": clean_name(bat["firstName"], bat["lastName"]),
                "runs": bat.get("runsScored", 0),
                "balls": bat.get("ballsFaced", 0),
                "fours": bat.get("fours", 0),
                "sixes": bat.get("sixers", 0),
                "is_out": bat.get("isOut") == "1",
                "dismissal_type": map_how_out(bat.get("howOut"), bat.get("outStringNoLink")),
                "out_description": bat.get("outStringNoLink") or None,
                "bowler_id": int(wicket_taker1) if wicket_taker1 else None,
                "bowler_name": bowler_lookup.get(wicket_taker1) if wicket_taker1 else None,
                "fielder_id": int(wicket_taker2) if wicket_taker2 else None,
                "fielder_name": bowler_lookup.get(wicket_taker2) if wicket_taker2 else None,
                "batting_style": bat.get("battingStyle") or None,
            })

        for bowl in innings.get("bowling", []):
            bowling_rows.append({
                "match_id": match_id,
                "inning_number": innings_num,
                "team_id": bowl.get("teamId"),
                "bowler_id": bowl["playerID"],
                "bowler_name": clean_name(bowl["firstName"], bowl["lastName"]),
                "balls": bowl.get("balls", 0),
                "runs": bowl.get("runs", 0),
                "wickets": bowl.get("wickets", 0),
                "maidens": bowl.get("maidens", 0),
                "wides": bowl.get("wides", 0),
                "no_balls": bowl.get("noBalls", 0),
                "dot_balls": bowl.get("dotBalls", 0),
                "hattricks": bowl.get("hattricks", 0),
                "bowling_style": bowl.get("bowlingStyle") or None,
            })

    return batting_rows, bowling_rows


def match_id_from_filename(path):
    m = re.search(r"scorecard_match(\d+)\.json$", os.path.basename(path))
    return int(m.group(1)) if m else None


def find_duplicate_keys(rows, innings_field, id_field):
    """Find rows sharing the same (match_id, innings_field, id_field) — the upsert
    conflict key. Returns a dict of key -> list of rows."""
    from collections import defaultdict
    groups = defaultdict(list)
    for row in rows:
        key = (row["match_id"], row[innings_field], row[id_field])
        groups[key].append(row)
    return {k: v for k, v in groups.items() if len(v) > 1}


def dedupe_keep_first(rows, innings_field, id_field):
    """Drop later rows sharing the same conflict key, keeping only the first occurrence."""
    seen = set()
    deduped = []
    for row in rows:
        key = (row["match_id"], row[innings_field], row[id_field])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)
    return deduped


BOWLING_SUM_FIELDS = [
    "balls", "runs", "wickets", "maidens",
    "wides", "no_balls", "dot_balls", "hattricks",
]


def dedupe_sum_bowling(rows, innings_field, id_field):
    """Merge rows sharing the same conflict key by summing the numeric stat
    fields (spells split across a rain delay, retired-hurt return, etc).
    Non-numeric fields are taken from the first entry."""
    from collections import OrderedDict
    merged = OrderedDict()
    for row in rows:
        key = (row["match_id"], row[innings_field], row[id_field])
        if key not in merged:
            merged[key] = dict(row)
        else:
            for field in BOWLING_SUM_FIELDS:
                merged[key][field] = merged[key].get(field, 0) + row.get(field, 0)
    return list(merged.values())


def chunked(items, size):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def upsert_rows(supabase, batting_rows, bowling_rows):
    for batch in chunked(batting_rows, BATCH_SIZE):
        supabase.table("tca_db_scorecard_batting_v2") \
            .upsert(batch, on_conflict="match_id,inning_number,batter_id") \
            .execute()

    for batch in chunked(bowling_rows, BATCH_SIZE):
        supabase.table("tca_db_scorecard_bowling_v2") \
            .upsert(batch, on_conflict="match_id,inning_number,bowler_id") \
            .execute()


def run_single(supabase, json_path, match_id):
    with open(json_path, "r", encoding="utf-8") as f:
        raw_json = json.load(f)

    batting_rows, bowling_rows = parse_scorecard(raw_json, match_id)
    print(f"Parsed {len(batting_rows)} batting rows, {len(bowling_rows)} bowling rows")

    upsert_rows(supabase, batting_rows, bowling_rows)
    print("Upserted to Supabase.")


def run_all(supabase, full=False):
    files = sorted(glob.glob(os.path.join(RESPONSES_DIR, "scorecard_match*.json")))
    print(f"Found {len(files)} scorecard files in {RESPONSES_DIR}/")

    if not full:
        existing_ids = get_existing_match_ids(supabase)
        # the most recent N match_ids already in the DB get re-processed too
        refresh_ids = set(sorted(existing_ids)[-REFRESH_WINDOW:])
        print(f"{len(existing_ids)} matches already in DB; "
              f"re-checking the latest {len(refresh_ids)}")

        def needs_processing(path):
            mid = match_id_from_filename(path)
            return mid is None or mid not in existing_ids or mid in refresh_ids

        files = [p for p in files if needs_processing(p)]
        print(f"{len(files)} files to process (new + refresh window)")

    if not files:
        print("Nothing to do.")
        return

    all_batting_rows = []
    all_bowling_rows = []
    skipped = []

    for path in files:
        match_id = match_id_from_filename(path)
        if match_id is None:
            print(f"Skipping {path} (couldn't extract match_id from filename)")
            skipped.append(path)
            continue

        with open(path, "r", encoding="utf-8") as f:
            raw_json = json.load(f)

        try:
            batting_rows, bowling_rows = parse_scorecard(raw_json, match_id)
        except (KeyError, TypeError) as e:
            print(f"[match {match_id}] parse error: {e}")
            skipped.append(path)
            continue

        all_batting_rows.extend(batting_rows)
        all_bowling_rows.extend(bowling_rows)

    print(f"Parsed {len(all_batting_rows)} batting rows, {len(all_bowling_rows)} bowling rows "
          f"from {len(files) - len(skipped)} matches ({len(skipped)} skipped)")

    batting_dupes = find_duplicate_keys(all_batting_rows, "inning_number", "batter_id")
    bowling_dupes = find_duplicate_keys(all_bowling_rows, "inning_number", "bowler_id")

    if batting_dupes or bowling_dupes:
        print(f"\nFound {len(batting_dupes)} duplicate batting keys, "
              f"{len(bowling_dupes)} duplicate bowling keys (same match/innings/player "
              f"appearing more than once):\n")
        for (match_id, innings, player_id), dupe_rows in list(batting_dupes.items())[:20]:
            names = [r["batter_name"] for r in dupe_rows]
            runs = [r["runs"] for r in dupe_rows]
            print(f"  batting: match {match_id}, innings {innings}, player {player_id} "
                f"({names[0]}): {len(dupe_rows)} entries, runs={runs} -> kept first")
        for (match_id, innings, player_id), dupe_rows in list(bowling_dupes.items())[:20]:
            names = [r["bowler_name"] for r in dupe_rows]
            wickets = [r["wickets"] for r in dupe_rows]
            print(f"  bowling: match {match_id}, innings {innings}, player {player_id} "
                f"({names[0]}): {len(dupe_rows)} entries, wickets={wickets} -> summed")
        print()

        all_batting_rows = dedupe_keep_first(all_batting_rows, "inning_number", "batter_id")
        all_bowling_rows = dedupe_sum_bowling(all_bowling_rows, "inning_number", "bowler_id")

    upsert_rows(supabase, all_batting_rows, all_bowling_rows)

    print("Upserted all rows to Supabase.")
    if skipped:
        print(f"Skipped files: {skipped}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("json_path", nargs="?", help="Path to a single scorecard JSON file")
    parser.add_argument("match_id", nargs="?", type=int, help="Match ID for the single file")
    parser.add_argument("--all", action="store_true", help=f"Process every file in {RESPONSES_DIR}/")
    parser.add_argument("--full", action="store_true", help="Re-upsert every file, ignoring what's already in the DB")
    args = parser.parse_args()

    client = create_client(SUPABASE_URL, SUPABASE_KEY)

    if args.all:
        run_all(client, full=args.full)
    elif args.json_path and args.match_id is not None:
        run_single(client, args.json_path, args.match_id)
    else:
        parser.error("Provide either --all, or both json_path and match_id")