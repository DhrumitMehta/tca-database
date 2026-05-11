"""
retry_missing.py
----------------
1. Queries Supabase for all match_ids present in tca_db_scorecard_batting
   between 1 and MAX_ID (default 6200).
2. Computes the gap — IDs in that range that are absent from the table.
3. Scrapes only those missing IDs using the updated scorecard_scraper logic
   (which now handles rain-abandoned / single-inning matches).

Usage examples
--------------
# Dry-run: just print missing IDs, don't scrape
python retry_missing.py --dry-run

# Scrape a single match ID to test
python retry_missing.py --only 6147

# Scrape all missing IDs up to 6200
python retry_missing.py

# Scrape all missing IDs up to a custom ceiling
python retry_missing.py --max-id 5000
"""

import os
import sys
import time
import logging
import argparse
from dotenv import load_dotenv
from supabase import create_client, Client

# ── make scorecard_scraper importable from the same directory ─────────────────
sys.path.insert(0, os.path.dirname(__file__))
from scorecard_scraper import (
    CLUB_ID,
    CLUB_SLUG,
    BATTING_TABLE,
    BOWLING_TABLE,
    SCRAPE_DELAY,
    CHECKPOINT_EVERY,
    create_driver,
    scrape_scorecard,
    flush,
)

load_dotenv()

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()],
)
log = logging.getLogger(__name__)

# ── Supabase client ───────────────────────────────────────────────────────────
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)


# ══════════════════════════════════════════════════════════════════════════════
# STEP 1 — find missing IDs
# ══════════════════════════════════════════════════════════════════════════════

def get_present_match_ids(max_id: int = 6200) -> set[int]:
    """
    Page through tca_db_scorecard_batting and return every distinct match_id
    that already exists in the range [1, max_id].

    Supabase returns at most 1 000 rows per call, so we page with range().
    """
    present: set[int] = set()
    page_size = 1000
    offset = 0

    log.info(f"Fetching existing match_ids from {BATTING_TABLE} (range 1–{max_id})…")
    while True:
        res = (
            supabase.table(BATTING_TABLE)
            .select("match_id")
            .gte("match_id", 1)
            .lte("match_id", max_id)
            .range(offset, offset + page_size - 1)
            .execute()
        )
        rows = res.data or []
        for row in rows:
            present.add(int(row["match_id"]))
        log.info(f"  fetched {len(rows)} rows (offset={offset}), running total={len(present)}")
        if len(rows) < page_size:
            break
        offset += page_size

    return present


def find_missing_ids(max_id: int = 6200) -> list[int]:
    full_range = set(range(1, max_id + 1))
    present    = get_present_match_ids(max_id)
    missing    = sorted(full_range - present)
    log.info(f"Found {len(missing)} missing match IDs out of {max_id} total.")
    return missing


# ══════════════════════════════════════════════════════════════════════════════
# STEP 2 — scrape the missing IDs
# ══════════════════════════════════════════════════════════════════════════════

def scrape_ids(
    ids: list[int],
    delay: float = SCRAPE_DELAY,
) -> None:
    if not ids:
        log.info("Nothing to scrape.")
        return

    log.info(f"Starting retry scrape for {len(ids)} match ID(s): {ids[:10]}{'…' if len(ids) > 10 else ''}")

    driver = create_driver()
    batting_buf: list[dict] = []
    bowling_buf: list[dict] = []
    success_ids, failed_ids = [], []

    try:
        log.info("Warming up session…")
        driver.get(f"https://www.cricclubs.com/{CLUB_SLUG}/home.do?clubId={CLUB_ID}")
        time.sleep(2)

        for match_id in ids:
            log.info(f"Scraping matchId={match_id}…")
            bat, bowl = scrape_scorecard(driver, match_id)
            time.sleep(delay)

            if bat is None and bowl is None:
                failed_ids.append(match_id)
            else:
                if bat:
                    batting_buf.extend(bat)
                if bowl:
                    bowling_buf.extend(bowl)
                success_ids.append(match_id)

            # Checkpoint flush every CHECKPOINT_EVERY successes
            if len(success_ids) % CHECKPOINT_EVERY == 0 and success_ids:
                flush(batting_buf, bowling_buf, label=f"checkpoint matchId={match_id}")
                batting_buf.clear()
                bowling_buf.clear()

    finally:
        driver.quit()

    # Final flush for whatever remains
    if batting_buf or bowling_buf:
        flush(batting_buf, bowling_buf, label="final flush")
    else:
        log.info("No remaining data to flush.")

    log.info("═" * 60)
    log.info(f"Retry complete: {len(success_ids)} saved | {len(failed_ids)} still failed/empty")
    if failed_ids:
        log.warning(f"Still-missing IDs after retry: {failed_ids}")
    log.info("═" * 60)


# ══════════════════════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Find and retry missing CricClubs scorecard match IDs."
    )
    parser.add_argument(
        "--max-id", type=int, default=6200,
        help="Upper bound of the match ID range to check (default: 6200)",
    )
    parser.add_argument(
        "--only", type=int, default=None,
        help="Scrape a single specific match ID (skips the missing-ID lookup)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Just print missing IDs without scraping anything",
    )
    parser.add_argument(
        "--delay", type=float, default=SCRAPE_DELAY,
        help=f"Seconds between requests (default: {SCRAPE_DELAY})",
    )
    args = parser.parse_args()

    if args.only:
        # ── Single-ID test mode ───────────────────────────────────────────────
        log.info(f"Single-ID mode: scraping matchId={args.only} only.")
        scrape_ids([args.only], delay=args.delay)

    elif args.dry_run:
        # ── Dry-run: just list missing IDs ───────────────────────────────────
        missing = find_missing_ids(args.max_id)
        print(f"\nMissing match IDs (1–{args.max_id})  [{len(missing)} total]:")
        print(missing)

    else:
        # ── Full retry mode ───────────────────────────────────────────────────
        missing = find_missing_ids(args.max_id)
        if missing:
            scrape_ids(missing, delay=args.delay)
        else:
            log.info("No missing IDs found — database is complete up to the given ceiling.")