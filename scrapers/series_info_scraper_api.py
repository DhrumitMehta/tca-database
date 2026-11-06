"""
series_info_scraper_api.py
---------------------------
Same purpose as series_info_scraper.py, but pulls series metadata from
CricClubs' internal JSON API (getSeriesDetails) instead of scraping rendered
HTML with Selenium.

Team rosters are NOT fetched here. The API response includes a `teams` URL
(the public viewLeaguePageTeams.do page), but requesting it with plain
`requests` gets 403'd by the site's bot protection. Rather than fight that,
this script just stores the URL in a new `teams_url` column so you have a
one-click link to check manually, and leaves `teams` (the JSONB array column)
null for now. Backfill that separately later — e.g. with a lightweight
Selenium pass targeted only at rows where teams IS NULL, similar to the
original script's backfill_teams().

NOTE: requires a new `teams_url` (text) column on tca_db_series_info —
add it in Supabase before running, or drop that field from the record below.

Checks Supabase for the highest existing league_id, then scrapes forward
from there, same incremental pattern as the original script.

Setup:
    pip install requests supabase python-dotenv

Usage:
    python series_info_scraper_api.py
"""

import os
import json
import time
import logging
from datetime import datetime, timezone
from dotenv import load_dotenv
import requests
from supabase import create_client, Client

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ══════════════════════════════════════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════════════════════════════════════
CLUB_ID = "7605"
TABLE_NAME = "tca_db_series_info"

BASE_URL = "https://core-prod-origin.cricclubs.com/core/series/getSeriesDetails"
HEADERS = {
    "x-api-key": "Tan@!75za",
    "x-consumer-key": "Tan#$72za5",
}

BATCH_SIZE = 30
CHECKPOINT_EVERY = 5
REQUEST_DELAY = 0.5   # APIs are much lighter than full page loads; still be polite
TIMEOUT = 10

# ── Daily request budget guard ──────────────────────────────────────────────
# You have a hard cap of 500 requests/day. This tracks usage in a local file
# so repeated runs (and crashes/retries) don't blow past your remaining quota
# without you noticing. Adjust DAILY_LIMIT / already-used count as needed.
DAILY_LIMIT = 500
BUDGET_FILE = "api_request_budget.json"
SAFETY_MARGIN = 20  # stop this many requests short of the true limit


def _load_budget() -> dict:
    today = datetime.now(timezone.utc).date().isoformat()
    if os.path.exists(BUDGET_FILE):
        with open(BUDGET_FILE) as f:
            budget = json.load(f)
        if budget.get("date") == today:
            return budget
    return {"date": today, "used": 0}


def _save_budget(budget: dict) -> None:
    with open(BUDGET_FILE, "w") as f:
        json.dump(budget, f)


def _check_and_increment_budget() -> bool:
    """Returns True if a request is safe to make, False if budget exhausted."""
    budget = _load_budget()
    if budget["used"] >= DAILY_LIMIT - SAFETY_MARGIN:
        log.error(
            f"  BUDGET STOP: {budget['used']}/{DAILY_LIMIT} requests used today "
            f"(safety margin {SAFETY_MARGIN}). Refusing further calls."
        )
        return False
    budget["used"] += 1
    _save_budget(budget)
    return True


# ══════════════════════════════════════════════════════════════════════════
# API CALL
# ══════════════════════════════════════════════════════════════════════════

def fetch_series_details(series_id: int, retries: int = 3) -> dict | None:
    params = {"clubId": CLUB_ID, "seriesId": str(series_id), "X-Auth-Token": ""}
    for attempt in range(retries):
        if not _check_and_increment_budget():
            return None  # hard stop — do not spend any more requests today
        try:
            r = requests.get(BASE_URL, params=params, headers=HEADERS, timeout=TIMEOUT)
            r.raise_for_status()
            return r.json()
        except requests.exceptions.RequestException as e:
            # NOTE: a failed/retried request still consumed one call above.
            # Retries are capped low (retries=3) on purpose to avoid quietly
            # burning quota on a series that's just erroring out.
            if attempt < retries - 1:
                wait = 3 * (2 ** attempt)
                log.warning(f"  seriesId={series_id} request failed ({attempt+1}/{retries}): {e}. Retry in {wait}s")
                time.sleep(wait)
            else:
                log.error(f"  seriesId={series_id} failed after {retries} attempts: {e}")
                return None


# ══════════════════════════════════════════════════════════════════════════
# PARSING — ADJUST FIELD NAMES ONCE YOU'VE SEEN A REAL RESPONSE
# ══════════════════════════════════════════════════════════════════════════

def parse_series_record(series_id: int, payload: dict) -> dict | None:
    if not payload or not payload.get("data"):
        log.warning(f"  [SKIP] seriesId={series_id} — no 'data' in response")
        return None

    data = payload["data"]

    series_name = data.get("name")
    if not series_name:
        log.warning(f"  [SKIP] seriesId={series_id} — empty name")
        return None

    record = {
        "league_id":   data.get("leagueId", series_id),
        "club_id":     data.get("clubId"),
        "series_name": series_name,
        "start_date":  _normalize_date(data.get("startDate")),
        "category":    data.get("category") or None,
        "ball_type":   data.get("ballType") or None,
        "level":       data.get("level") or None,
        "series_type": data.get("seriesType") or None,
        "max_overs":   _to_int(data.get("maxOvers")),
        "winner":      data.get("winnerName") or None,
        "runner_up":   data.get("runnerName") or None,
        "teams":       None,  # left null — backfill later via a separate pass
        "teams_url":   data.get("teams") or None,  # NEW COLUMN — see note below
        "scraped_at":  datetime.now(timezone.utc).isoformat(),
    }
    return record


def _to_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _normalize_date(text: str) -> str | None:
    if not text:
        return None
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%d/%m/%Y", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text[: len(fmt)] if "T" in fmt else text, fmt).date().isoformat()
        except ValueError:
            continue
    return None


# ══════════════════════════════════════════════════════════════════════════
# SUPABASE HELPERS (unchanged logic from original script)
# ══════════════════════════════════════════════════════════════════════════

def get_last_league_id() -> int:
    res = (
        supabase.table(TABLE_NAME)
        .select("league_id")
        .order("league_id", desc=True)
        .limit(1)
        .execute()
    )
    if res.data:
        return int(res.data[0]["league_id"])
    return 0


def upsert_with_retry(records: list[dict], retries: int = 4, base_backoff: float = 5.0) -> None:
    BATCH = 500
    for i in range(0, len(records), BATCH):
        batch = records[i: i + BATCH]
        for attempt in range(retries):
            try:
                supabase.table(TABLE_NAME).upsert(batch, on_conflict="league_id").execute()
                log.info(f"  Upserted rows {i}-{min(i + BATCH, len(records))}")
                break
            except Exception as e:
                if attempt < retries - 1:
                    wait = base_backoff * (2 ** attempt)
                    log.warning(f"  Upsert failed ({attempt+1}/{retries}): {e}. Retry in {wait}s")
                    time.sleep(wait)
                else:
                    log.error("  Upsert failed after all retries.")
                    raise


def flush(buf: list[dict], label: str = "flush") -> None:
    if buf:
        log.info(f"[{label}] Upserting {len(buf)} rows")
        upsert_with_retry(buf)


# ══════════════════════════════════════════════════════════════════════════
# MAIN LOOP
# ══════════════════════════════════════════════════════════════════════════

def run_scraper(batch_size: int = BATCH_SIZE, delay: float = REQUEST_DELAY) -> None:
    budget = _load_budget()
    remaining = DAILY_LIMIT - SAFETY_MARGIN - budget["used"]
    if remaining < batch_size:
        log.warning(
            f"  Only {remaining} API requests left in today's budget "
            f"({budget['used']}/{DAILY_LIMIT} used), but batch_size={batch_size}. "
            f"Reducing batch_size to {max(remaining, 0)}."
        )
        batch_size = max(remaining, 0)
    if batch_size == 0:
        log.error("  No API budget remaining today. Exiting without making requests.")
        return

    last_id = get_last_league_id()
    start_id, end_id = last_id + 1, last_id + batch_size
    log.info(f"Scraping series IDs {start_id} → {end_id} ({batch_size} API calls, {remaining} available)")

    buf, success_ids, failed_ids = [], [], []
    attempted = 0

    for series_id in range(start_id, end_id + 1):
        payload = fetch_series_details(series_id)
        try:
            record = parse_series_record(series_id, payload) if payload else None
        except Exception as e:
            log.error(f"  [ERROR] seriesId={series_id} — unexpected parse failure: {e}")
            record = None
        time.sleep(delay)

        attempted += 1

        if record is None:
            failed_ids.append(series_id)
        else:
            buf.append(record)
            success_ids.append(series_id)
            log.info(f"  [OK] seriesId={series_id} — '{record['series_name']}'")

        # Checkpoint on TOTAL attempts, not just successes — otherwise a run
        # of failures/skips means valid records sit unsaved for longer than
        # intended, risking loss (and wasted budget) if something crashes.
        if attempted % CHECKPOINT_EVERY == 0:
            flush(buf, label=f"checkpoint seriesId={series_id}")
            buf.clear()

    flush(buf, label="final flush")
    log.info(f"Done. succeeded={len(success_ids)} failed={len(failed_ids)}")
    if failed_ids:
        log.warning(f"Failed/empty IDs: {failed_ids}")


if __name__ == "__main__":
    budget = _load_budget()
    log.info(f"Budget so far today: {budget['used']}/{DAILY_LIMIT} used.")
    run_scraper()