"""
player_info_scraper.py
----------------------
Scrapes player profile data from CricClubs and upserts into Supabase.
Runs incrementally: queries Supabase for the highest existing player_id,
then scrapes the next `batch_size` player IDs from there.

Table used:
    tca_db_player_info — one row per player

Setup:
    pip install selenium beautifulsoup4 supabase python-dotenv webdriver-manager

Environment variables (.env):
    SUPABASE_URL = https://xxxx.supabase.co
    SUPABASE_KEY = your-service-role-or-anon-key
"""

import os
import re
import time
import logging
from datetime import datetime, timezone
from dotenv import load_dotenv

from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager
from supabase import create_client, Client

load_dotenv()

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()],
)
log = logging.getLogger(__name__)

# ── Supabase client ────────────────────────────────────────────────────────────
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ══════════════════════════════════════════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════════════════════════════════════════
CLUB_ID   = 7605
CLUB_SLUG = "Tanzania"

BATCH_SIZE       = 5000
CHECKPOINT_EVERY = 50
SCRAPE_DELAY     = 0.7

TABLE_NAME = "tca_db_player_info"

# ══════════════════════════════════════════════════════════════════════════════
# SELENIUM HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def create_driver() -> webdriver.Chrome:
    options = Options()
    options.add_argument("--headless")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    options.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=options)
    driver.execute_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    )
    return driver


def get_soup(driver, url: str, wait_css: str = None, timeout: int = 12) -> BeautifulSoup:
    driver.get(url)
    if wait_css:
        try:
            WebDriverWait(driver, timeout).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, wait_css))
            )
        except Exception:
            pass
    time.sleep(1.5)
    return BeautifulSoup(driver.page_source, "html.parser")


def _safe_text(value) -> str | None:
    if value is None:
        return None
    return str(value).strip() or None


def _safe_int(value) -> int | None:
    if value is None:
        return None
    try:
        return int(str(value).strip())
    except ValueError:
        return None


def _p_label_value(soup: BeautifulSoup, label: str) -> str | None:
    """
    Finds a <p> whose text contains `label`, then returns the text of
    the first <strong> inside it.

    Matches HTML like:
        <p>Playing Role : <strong>All Rounder</strong></p>
        <p>Batting Style : <strong>Right Handed Batter</strong></p>
    """
    for p in soup.select("p"):
        # get_text includes the strong text, so check raw text for the label
        raw = p.get_text(" ", strip=True)
        if re.search(rf"{re.escape(label)}\s*:", raw, re.IGNORECASE):
            strong = p.find("strong")
            if strong:
                return _safe_text(strong.get_text(strip=True))
    return None


def _p_label_raw(soup: BeautifulSoup, label: str) -> str | None:
    """
    Finds a <p> whose text contains `label :`, then returns everything
    after the colon as raw text (no <strong> wrapping).

    Matches HTML like:
        <p>Teams : Aksc,Upanga Warriors,TZ GREEN,...</p>
    """
    for p in soup.select("p"):
        raw = p.get_text(" ", strip=True)
        if re.search(rf"{re.escape(label)}\s*:", raw, re.IGNORECASE):
            # Return the part after the first colon
            after = raw.split(":", 1)[1].strip()
            return after or None
    return None


def _extract_player_details(soup: BeautifulSoup) -> dict:
    details = {}

    # ── Name ──────────────────────────────────────────────────────────────────
    # <h4><span>Dhrumit Mehta</span> ...</h4>
    name_elem = soup.select_one("h4 span")
    details["name"] = _safe_text(name_elem.get_text(strip=True)) if name_elem else None

    # ── Verified badge ─────────────────────────────────────────────────────────
    details["verified"] = soup.select_one("h4 img[alt='Verified']") is not None

    # ── CC Player ID ───────────────────────────────────────────────────────────
    # <p>CC Player ID :&nbsp; <strong>996332</strong></p>
    # Use the label helper — finds the <strong> inside the matching <p>
    raw_id = _p_label_value(soup, "CC Player ID")
    details["player_id_from_page"] = _safe_int(raw_id)

    # ── Current Team ───────────────────────────────────────────────────────────
    # <p>Current Team : <strong><a href="...?teamId=2701...">TZ GREEN</a></strong></p>
    team_link = soup.select_one("p a[href*='viewTeam.do']")
    if team_link:
        details["current_team"] = _safe_text(team_link.get_text(strip=True))
        href = team_link.get("href", "")
        m = re.search(r"teamId=(\d+)", href)
        if m:
            details["current_team_id"] = int(m.group(1))

    # ── Teams list ─────────────────────────────────────────────────────────────
    # <p>Teams : Aksc,Upanga Warriors,TZ GREEN,...</p>
    # No <strong> here — raw text after the colon, comma-separated
    teams_raw = _p_label_raw(soup, "Teams")
    if teams_raw:
        teams_list = [t.strip() for t in teams_raw.split(",") if t.strip()]
        details["teams"] = teams_list if teams_list else None

    # ── Age ────────────────────────────────────────────────────────────────────
    # <p>Age : <strong><span id="age">23</span></strong></p>
    age_elem = soup.select_one("span#age")
    details["age"] = _safe_int(age_elem.get_text(strip=True)) if age_elem else None

    # ── Playing Role / Batting Style / Bowling Style ───────────────────────────
    # <p>Playing Role : <strong>All Rounder</strong></p>
    details["playing_role"]  = _p_label_value(soup, "Playing Role")
    details["batting_style"] = _p_label_value(soup, "Batting Style")
    details["bowling_style"] = _p_label_value(soup, "Bowling Style")

    return details


def scrape_player_info(driver, player_id: int) -> dict | None:
    url = (
        f"https://www.cricclubs.com/{CLUB_SLUG}/viewPlayer.do"
        f"?playerId={player_id}&clubId={CLUB_ID}"
    )
    soup = get_soup(driver, url, wait_css="h4, .col-sm-6", timeout=12)

    details = _extract_player_details(soup)

    if not details.get("name"):
        log.warning(f"  [SKIP] Player name not found for playerId={player_id}")
        return None

    record = {
        "player_id":       player_id,
        "club_id":         CLUB_ID,
        "name":            details.get("name"),
        "verified":        details.get("verified", False),
        "current_team":    details.get("current_team"),
        "current_team_id": details.get("current_team_id"),
        "teams":           details.get("teams"),
        "age":             details.get("age"),
        "playing_role":    details.get("playing_role"),
        "batting_style":   details.get("batting_style"),
        "bowling_style":   details.get("bowling_style"),
        "scraped_at":      datetime.now(timezone.utc).isoformat(),
    }

    log.info(f"  [OK] playerId={player_id} — name='{record['name']}'")
    return record


# ══════════════════════════════════════════════════════════════════════════════
# SUPABASE HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def get_known_player_ids() -> list[int]:
    """
    Collects all distinct player IDs seen in the scorecard tables,
    then subtracts any already scraped in tca_db_player_info.
    Returns a sorted list of IDs still needing to be scraped.
    """
    def fetch_all_ids(table: str, col: str) -> set[int]:
        ids = set()
        BATCH = 1000
        offset = 0
        while True:
            res = (
                supabase.table(table)
                .select(col)
                .range(offset, offset + BATCH - 1)
                .execute()
            )
            if not res.data:
                break
            for row in res.data:
                val = row.get(col)
                if val is not None:
                    ids.add(int(val))
            if len(res.data) < BATCH:
                break
            offset += BATCH
        return ids

    log.info("Fetching batter_ids from tca_db_scorecard_batting…")
    batter_ids = fetch_all_ids("tca_db_scorecard_batting", "batter_id")
    log.info(f"  {len(batter_ids)} unique batter IDs")

    log.info("Fetching bowler_ids from tca_db_scorecard_bowling…")
    bowler_ids = fetch_all_ids("tca_db_scorecard_bowling", "bowler_id")
    log.info(f"  {len(bowler_ids)} unique bowler IDs")

    all_ids = batter_ids | bowler_ids
    log.info(f"  {len(all_ids)} combined unique player IDs")

    log.info("Fetching already-scraped player_ids from tca_db_player_info…")
    scraped_ids = fetch_all_ids(TABLE_NAME, "player_id")
    log.info(f"  {len(scraped_ids)} already scraped")

    remaining = sorted(all_ids - scraped_ids)
    log.info(f"  {len(remaining)} IDs left to scrape")
    return remaining


def upsert_with_retry(records: list[dict], retries: int = 4, base_backoff: float = 5.0) -> None:
    BATCH = 500
    for i in range(0, len(records), BATCH):
        batch = records[i : i + BATCH]
        for attempt in range(retries):
            try:
                supabase.table(TABLE_NAME).upsert(batch, on_conflict="player_id").execute()
                log.info(f"  Upserted rows {i}–{min(i + BATCH, len(records))} → {TABLE_NAME}")
                break
            except Exception as e:
                if attempt < retries - 1:
                    wait = base_backoff * (2 ** attempt)
                    log.warning(f"  Upsert failed ({attempt+1}/{retries}): {e}. Retry in {wait}s…")
                    time.sleep(wait)
                else:
                    log.error(f"  Upsert failed after {retries} attempts. Giving up on batch.")
                    raise


def flush(buf: list[dict], label: str = "flush") -> None:
    if buf:
        log.info(f"[{label}] Upserting {len(buf)} player info rows…")
        upsert_with_retry(buf)


# ══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

def run_scraper(batch_size: int = BATCH_SIZE, delay: float = SCRAPE_DELAY) -> None:
    log.info("═" * 60)
    log.info("Starting CricClubs player info scraper (scorecard-driven)")
    log.info("═" * 60)

    player_ids = get_known_player_ids()

    if not player_ids:
        log.info("No new player IDs to scrape. All done.")
        return

    # Respect batch_size — only scrape up to that many per run
    player_ids = player_ids[:batch_size]
    log.info(f"Scraping {len(player_ids)} player IDs this run (batch_size={batch_size})")

    driver = create_driver()
    buf: list[dict] = []
    success_ids, failed_ids = [], []

    try:
        log.info("Warming up session…")
        driver.get(f"https://www.cricclubs.com/{CLUB_SLUG}/home.do?clubId={CLUB_ID}")
        time.sleep(2)

        for player_id in player_ids:
            record = scrape_player_info(driver, player_id)
            time.sleep(delay)

            if record is None:
                failed_ids.append(player_id)
            else:
                buf.append(record)
                success_ids.append(player_id)

            if len(success_ids) % CHECKPOINT_EVERY == 0 and success_ids:
                flush(buf, label=f"checkpoint playerId={player_id}")
                buf.clear()
    finally:
        driver.quit()

    log.info(f"\nScraped: {len(success_ids)} succeeded | {len(failed_ids)} skipped/failed")
    if failed_ids:
        log.warning(f"Skipped IDs: {failed_ids}")

    if buf:
        flush(buf, label="final flush")
    else:
        log.info("No remaining data to flush.")

    log.info("Done ✓")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="CricClubs player info scraper")
    parser.add_argument("--start", type=int, default=None, help="Start player ID (overrides incremental)")
    parser.add_argument("--end",   type=int, default=None, help="End player ID (inclusive)")
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE, help="How many IDs to attempt")
    parser.add_argument("--delay", type=float, default=SCRAPE_DELAY, help="Seconds between requests")
    args = parser.parse_args()

    if args.start and args.end:
        # Manual override — scrape an explicit range regardless of scorecard tables
        log.info(f"Manual range override: {args.start} → {args.end}")
        player_ids = list(range(args.start, args.end + 1))

        driver = create_driver()
        driver.get(f"https://www.cricclubs.com/{CLUB_SLUG}/home.do?clubId={CLUB_ID}")
        time.sleep(2)

        buf: list[dict] = []
        success_ids, failed_ids = [], []

        try:
            for player_id in player_ids:
                record = scrape_player_info(driver, player_id)
                time.sleep(args.delay)

                if record is None:
                    failed_ids.append(player_id)
                else:
                    buf.append(record)
                    success_ids.append(player_id)

                if len(success_ids) % CHECKPOINT_EVERY == 0 and success_ids:
                    flush(buf, label=f"checkpoint playerId={player_id}")
                    buf.clear()
        finally:
            driver.quit()

        flush(buf, label="final flush")
        log.info(f"Done ✓  succeeded={len(success_ids)}  failed={len(failed_ids)}")
        if failed_ids:
            log.warning(f"Failed IDs: {failed_ids}")
    else:
        run_scraper(batch_size=args.batch_size, delay=args.delay)