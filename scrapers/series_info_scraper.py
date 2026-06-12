"""
series_info_scraper.py
----------------------
Scrapes series metadata from CricClubs (viewLeague.do) and upserts into Supabase.
Also scrapes the teams for each series from viewLeaguePageTeams.do, storing them
as a JSONB array: [{"team_id": 2721, "team_name": "Magadu Stars Women", "player_count": 12}, ...]

Runs incrementally: queries Supabase for the highest existing league_id,
then scrapes the next `batch_size` league IDs from there.

Table used:
    tca_db_series_info — one row per series

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

BATCH_SIZE       = 350
CHECKPOINT_EVERY = 20
SCRAPE_DELAY     = 1.5

TABLE_NAME = "tca_db_series_info"

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


def _parse_date(text: str) -> str | None:
    """Parse MM/DD/YYYY format used by CricClubs."""
    if not text:
        return None
    text = text.strip()
    patterns = [
        "%m/%d/%Y",   # 09/05/2026  ← CricClubs default
        "%d/%m/%Y",
        "%d/%m/%y",
        "%d %b %Y",
        "%d %B %Y",
        "%Y-%m-%d",
    ]
    for fmt in patterns:
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return None


# ══════════════════════════════════════════════════════════════════════════════
# TEAM SCRAPER
# ══════════════════════════════════════════════════════════════════════════════

def scrape_teams(driver, league_id: int) -> list[dict]:
    """
    Scrapes viewLeaguePageTeams.do for the given league_id.

    Returns a list of dicts:
        [
            {"team_id": 2721, "team_name": "Magadu Stars Women", "player_count": 12},
            ...
        ]

    Parses two sources for team_id (whichever is available):
      1. The <tr id="row{team_id}"> attribute on each row
      2. The href on the team name link: viewTeam.do?teamId={team_id}&...
    """
    url = (
        f"https://www.cricclubs.com/{CLUB_SLUG}/viewLeaguePageTeams.do"
        f"?league={league_id}&clubId={CLUB_ID}"
    )
    soup = get_soup(driver, url, wait_css="#anyid, table.table", timeout=12)

    teams = []

    # The teams table has id="anyid" per the observed HTML
    table = soup.select_one("table#anyid") or soup.select_one(".about-table table")
    if not table:
        log.debug(f"  [teams] No teams table found for league_id={league_id}")
        return teams

    for row in table.select("tbody tr"):
        # ── Extract team_id ────────────────────────────────────────────────────
        team_id = None

        # Method 1: from row id attribute e.g. id="row2721"
        row_id_attr = row.get("id", "")
        row_id_match = re.match(r"^row(\d+)$", row_id_attr)
        if row_id_match:
            team_id = int(row_id_match.group(1))

        # Method 2: from the team link href (fallback)
        if team_id is None:
            link = row.select_one("a[href*='viewTeam.do']")
            if link:
                href = link.get("href", "")
                tid_match = re.search(r"teamId=(\d+)", href)
                if tid_match:
                    team_id = int(tid_match.group(1))

        if team_id is None:
            log.debug(f"  [teams] Could not extract team_id from row, skipping")
            continue

        # ── Extract team name ──────────────────────────────────────────────────
        link = row.select_one("a[href*='viewTeam.do']")
        if not link:
            continue
        team_name = _safe_text(link.get_text(strip=True))
        if not team_name:
            continue

        # ── Extract player count ───────────────────────────────────────────────
        # The cell contains: "Magadu Stars Women\n(12\n)"
        # We grab the text of the full <td> and parse the number in parens
        cell_text = link.parent.get_text(" ", strip=True) if link.parent else ""
        player_count = None
        pc_match = re.search(r"\((\d+)\s*\)", cell_text)
        if pc_match:
            player_count = int(pc_match.group(1))

        teams.append({
            "team_id":      team_id,
            "team_name":    team_name,
            "player_count": player_count,
        })

    if teams:
        log.info(f"  [teams] league_id={league_id} → {len(teams)} teams: {[t['team_name'] for t in teams]}")
    else:
        log.debug(f"  [teams] league_id={league_id} → 0 teams found")

    return teams


# ══════════════════════════════════════════════════════════════════════════════
# SERIES SCRAPER
# ══════════════════════════════════════════════════════════════════════════════

def scrape_league(driver, league_id: int) -> dict | None:
    """
    Scrapes viewLeague.do?league={league_id}&clubId={CLUB_ID}
    Then also scrapes viewLeaguePageTeams.do for the teams list.

    The page layout:
      <h3 class="theme-color">SERIES NAME</h3>
      <table id="leagueInfoTable">
        <tr>
          <td>Start Date</td><td>:</td><td>09/05/2026</td>
          <td>Category</td><td>:</td><td>Women</td>
        </tr>
        ...
      </table>
    """
    url = (
        f"https://www.cricclubs.com/{CLUB_SLUG}/viewLeague.do"
        f"?league={league_id}&clubId={CLUB_ID}"
    )
    soup = get_soup(driver, url, wait_css="#leagueInfoTable, h3.theme-color", timeout=12)

    # Series name
    name_el = soup.select_one("h3.theme-color")
    if not name_el:
        log.warning(f"  [SKIP] No series name found for league_id={league_id}")
        return None

    series_name = _safe_text(name_el.get_text(strip=True))
    if not series_name:
        return None

    # Parse #leagueInfoTable
    table = soup.select_one("#leagueInfoTable")
    if not table:
        log.warning(f"  [SKIP] #leagueInfoTable missing for league_id={league_id}")
        return None

    data = {}
    for row in table.select("tr"):
        cells = [td.get_text(strip=True) for td in row.select("td")]
        if len(cells) >= 6:
            if cells[0] and cells[1] == ":":
                data[cells[0]] = cells[2]
            if cells[3] and cells[4] == ":":
                data[cells[3]] = cells[5]

    # Scrape teams for this league
    time.sleep(0.5)  # brief pause before second request
    teams = scrape_teams(driver, league_id)

    record = {
        "league_id":   league_id,
        "club_id":     CLUB_ID,
        "series_name": series_name,
        "start_date":  _parse_date(data.get("Start Date")),
        "category":    _safe_text(data.get("Category")),
        "ball_type":   _safe_text(data.get("Ball Type")),
        "level":       _safe_text(data.get("Level")),
        "series_type": _safe_text(data.get("Series Type")),
        "max_overs":   int(data["Max Overs"]) if data.get("Max Overs", "").isdigit() else None,
        "winner":      _safe_text(data.get("Winner")) or None,
        "runner_up":   _safe_text(data.get("Runner-up")) or None,
        "teams":       teams if teams else None,   # JSONB array or null
        "scraped_at":  datetime.now(timezone.utc).isoformat(),
    }

    log.info(f"  [OK] league_id={league_id} — '{series_name}' ({len(teams)} teams)")
    return record


# ══════════════════════════════════════════════════════════════════════════════
# SUPABASE HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def get_last_league_id() -> int:
    res = (
        supabase.table(TABLE_NAME)
        .select("league_id")
        .order("league_id", desc=True)
        .limit(1)
        .execute()
    )
    if res.data:
        val = int(res.data[0]["league_id"])
        log.info(f"Last league_id in {TABLE_NAME}: {val}")
        return val
    log.info(f"No existing data in {TABLE_NAME} — starting from league ID 1")
    return 0


def upsert_with_retry(records: list[dict], retries: int = 4, base_backoff: float = 5.0) -> None:
    BATCH = 500
    for i in range(0, len(records), BATCH):
        batch = records[i : i + BATCH]
        for attempt in range(retries):
            try:
                supabase.table(TABLE_NAME).upsert(batch, on_conflict="league_id").execute()
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
        log.info(f"[{label}] Upserting {len(buf)} series info rows…")
        upsert_with_retry(buf)


# ══════════════════════════════════════════════════════════════════════════════
# BACKFILL: update teams for existing series rows that have teams=null
# ══════════════════════════════════════════════════════════════════════════════

def backfill_teams(delay: float = SCRAPE_DELAY) -> None:
    """
    Fetches all rows where teams IS NULL and re-scrapes the teams page for each.
    Useful for populating the new column on already-scraped series.
    Run once after the migration with:
        python series_info_scraper.py --backfill
    """
    log.info("═" * 60)
    log.info("Backfill mode: fetching series rows with teams=null")
    log.info("═" * 60)

    # Paginate through all null-teams rows
    PAGE = 1000
    offset = 0
    null_rows = []
    while True:
        res = (
            supabase.table(TABLE_NAME)
            .select("league_id, series_name")
            .is_("teams", "null")
            .order("league_id")
            .range(offset, offset + PAGE - 1)
            .execute()
        )
        batch = res.data or []
        null_rows.extend(batch)
        if len(batch) < PAGE:
            break
        offset += PAGE

    if not null_rows:
        log.info("No rows with teams=null found. Nothing to backfill.")
        return

    log.info(f"Found {len(null_rows)} series rows to backfill.")

    driver = create_driver()
    driver.get(f"https://www.cricclubs.com/{CLUB_SLUG}/home.do?clubId={CLUB_ID}")
    time.sleep(2)

    buf = []
    try:
        for i, row in enumerate(null_rows):
            league_id   = row["league_id"]
            series_name = row["series_name"]
            log.info(f"  [{i+1}/{len(null_rows)}] league_id={league_id} '{series_name}'")

            teams = scrape_teams(driver, league_id)
            time.sleep(delay)

            buf.append({
                "league_id": league_id,
                "teams":     teams if teams else None,
            })

            # Checkpoint every 20
            if len(buf) >= CHECKPOINT_EVERY:
                for record in buf:
                    supabase.table(TABLE_NAME).update(
                        {"teams": record["teams"]}
                    ).eq("league_id", record["league_id"]).execute()
                log.info(f"  Checkpointed {len(buf)} rows.")
                buf.clear()
    finally:
        driver.quit()

    # Final flush
    for record in buf:
        supabase.table(TABLE_NAME).update(
            {"teams": record["teams"]}
        ).eq("league_id", record["league_id"]).execute()
    if buf:
        log.info(f"  Final flush: {len(buf)} rows.")

    log.info("Backfill complete ✓")


# ══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

def run_scraper(batch_size: int = BATCH_SIZE, delay: float = SCRAPE_DELAY) -> None:
    log.info("═" * 60)
    log.info("Starting CricClubs series info scraper (incremental)")
    log.info("═" * 60)

    last_id = get_last_league_id()
    start_id = last_id + 1
    end_id = last_id + batch_size
    log.info(f"Scraping league IDs {start_id} → {end_id}  (batch_size={batch_size})")

    driver = create_driver()
    buf: list[dict] = []
    success_ids, failed_ids = [], []

    try:
        log.info("Warming up session…")
        driver.get(f"https://www.cricclubs.com/{CLUB_SLUG}/home.do?clubId={CLUB_ID}")
        time.sleep(2)

        for league_id in range(start_id, end_id + 1):
            record = scrape_league(driver, league_id)
            time.sleep(delay)

            if record is None:
                failed_ids.append(league_id)
            else:
                buf.append(record)
                success_ids.append(league_id)

            if len(success_ids) % CHECKPOINT_EVERY == 0 and success_ids:
                flush(buf, label=f"checkpoint league_id={league_id}")
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

    parser = argparse.ArgumentParser(description="CricClubs series info scraper")
    parser.add_argument("--start",     type=int,   default=None, help="Start league ID (overrides incremental)")
    parser.add_argument("--end",       type=int,   default=None, help="End league ID (inclusive)")
    parser.add_argument("--batch-size",type=int,   default=BATCH_SIZE,  help="How many IDs to attempt")
    parser.add_argument("--delay",     type=float, default=SCRAPE_DELAY, help="Seconds between requests")
    parser.add_argument("--backfill",  action="store_true", help="Backfill teams for existing rows where teams=null")
    args = parser.parse_args()

    # ── Backfill mode ──────────────────────────────────────────────────────────
    if args.backfill:
        backfill_teams(delay=args.delay)

    # ── Manual range mode ──────────────────────────────────────────────────────
    elif args.start and args.end:
        log.info(f"Manual range: {args.start} → {args.end}")
        driver = create_driver()
        driver.get(f"https://www.cricclubs.com/{CLUB_SLUG}/home.do?clubId={CLUB_ID}")
        time.sleep(2)

        buf: list[dict] = []
        success_ids, failed_ids = [], []

        try:
            for league_id in range(args.start, args.end + 1):
                record = scrape_league(driver, league_id)
                time.sleep(args.delay)

                if record is None:
                    failed_ids.append(league_id)
                else:
                    buf.append(record)
                    success_ids.append(league_id)

                if len(success_ids) % CHECKPOINT_EVERY == 0 and success_ids:
                    flush(buf, label=f"checkpoint league_id={league_id}")
                    buf.clear()
        finally:
            driver.quit()

        flush(buf, label="final flush")
        log.info(f"Done ✓  succeeded={len(success_ids)}  failed={len(failed_ids)}")
        if failed_ids:
            log.warning(f"Failed IDs: {failed_ids}")

    # ── Incremental mode ───────────────────────────────────────────────────────
    else:
        run_scraper(batch_size=args.batch_size, delay=args.delay)