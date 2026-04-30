"""
match_info_scraper.py
---------------------
Scrapes match info page data from CricClubs Tanzania and upserts into Supabase.
Runs incrementally: queries Supabase for the highest existing match_id,
then scrapes the next `batch_size` match IDs from there.

Table used:
    tca_db_match_info — one row per match

Setup:
    pip install selenium beautifulsoup4 supabase python-dotenv webdriver-manager

Environment variables (set in .env or your deployment environment):
    SUPABASE_URL = https://xxxx.supabase.co
    SUPABASE_KEY = your-service-role-or-anon-key

Supabase table DDL (run once in SQL editor):
─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS tca_db_match_info (
    id                      BIGSERIAL PRIMARY KEY,
    match_id                INTEGER      NOT NULL UNIQUE,
    series                  TEXT,
    match_date              DATE,
    toss_winner             TEXT,
    toss_decision           TEXT,
    player_of_match_id      TEXT,
    player_of_match_name    TEXT,
    umpire_1_id             TEXT,
    umpire_1_name           TEXT,
    umpire_2_id             TEXT,
    umpire_2_name           TEXT,
    venue                   TEXT,
    points_team_1           TEXT,
    points_team_1_score     INTEGER,
    points_team_2           TEXT,
    points_team_2_score     INTEGER,
    innings_1_duration_min  INTEGER,
    innings_1_start         TEXT,
    innings_1_end           TEXT,
    innings_break_min       INTEGER,
    innings_break_start     TEXT,
    innings_break_end       TEXT,
    innings_2_duration_min  INTEGER,
    innings_2_start         TEXT,
    innings_2_end           TEXT,
    last_updated_by         TEXT,
    last_updated_at         TEXT,
    scraped_at              TIMESTAMPTZ  DEFAULT NOW()
);
─────────────────────────────────────────────────────────────────────────────
"""

import os
import re
import time
import logging
from datetime import datetime, timezone, date
from dotenv import load_dotenv

from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
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

BATCH_SIZE       = 100  # match IDs to attempt per run
CHECKPOINT_EVERY = 20    # flush to Supabase every N successful matches
SCRAPE_DELAY     = 1.5   # seconds between requests

MATCH_INFO_TABLE = "tca_db_match_info"

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


def get_soup(driver, url: str, wait_css: str = None, timeout: int = 15,
             retries: int = 3, retry_delay: float = 15.0) -> BeautifulSoup:
    from selenium.common.exceptions import WebDriverException
    for attempt in range(retries):
        try:
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
        except WebDriverException as e:
            if "ERR_INTERNET_DISCONNECTED" in str(e) or "ERR_CONNECTION" in str(e):
                if attempt < retries - 1:
                    log.warning(f"  [NET] Network error on attempt {attempt+1}/{retries} for {url}. "
                                f"Retrying in {retry_delay}s…")
                    time.sleep(retry_delay)
                else:
                    log.error(f"  [NET] Network error after {retries} attempts. Giving up.")
                    raise
            else:
                raise


# ══════════════════════════════════════════════════════════════════════════════
# PARSING HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _safe_int(val, default=None):
    try:
        return int(str(val).strip())
    except (ValueError, TypeError):
        return default


def _player_id_from_href(href: str) -> str | None:
    if not href or href.strip().startswith("javascript:"):
        return None
    if "playerId=" in href:
        return href.split("playerId=")[-1].split("&")[0]
    return None


def _umpire_id_from_href(href: str) -> str | None:
    if not href or href.strip().startswith("javascript:"):
        return None
    if "umpireUId=" in href:
        return href.split("umpireUId=")[-1].split("&")[0]
    return None


def _parse_duration_and_times(text: str) -> tuple[int | None, str | None, str | None]:
    """
    Parse a cell like: '87 min   10:15 AM   11:42 AM'
    Returns (duration_minutes, start_time, end_time)
    """
    duration = None
    start = None
    end = None

    m = re.search(r"(\d+)\s*min", text)
    if m:
        duration = int(m.group(1))

    times = re.findall(r"\d{1,2}:\d{2}\s*(?:AM|PM)", text)
    if len(times) >= 1:
        start = times[0].strip()
    if len(times) >= 2:
        end = times[1].strip()

    return duration, start, end


def _parse_match_date(text: str) -> str | None:
    """Parse DD/MM/YYYY → ISO date string YYYY-MM-DD, or None."""
    m = re.search(r"(\d{2})/(\d{2})/(\d{4})", text)
    if m:
        d, mo, y = m.group(1), m.group(2), m.group(3)
        return f"{y}-{mo}-{d}"
    return None


def _parse_points(text: str) -> tuple[str | None, int | None, str | None, int | None]:
    """
    Parse 'AZANIA : 2 , TZ RED WOMEN : 0'
    Returns (team1_name, team1_score, team2_name, team2_score)
    """
    parts = [p.strip() for p in text.split(",")]
    team1 = team1_score = team2 = team2_score = None
    for i, part in enumerate(parts[:2]):
        m = re.match(r"(.+?)\s*:\s*(\d+)", part)
        if m:
            if i == 0:
                team1 = m.group(1).strip()
                team1_score = int(m.group(2))
            else:
                team2 = m.group(1).strip()
                team2_score = int(m.group(2))
    return team1, team1_score, team2, team2_score


def _parse_toss(text: str) -> tuple[str | None, str | None]:
    """
    Parse 'AZANIA won the toss and elected to bat'
    Returns (toss_winner, toss_decision)
    """
    # Split on 'won the toss' to get winner, then on 'elected to' to get decision
    parts = re.split(r"\s+won the toss", text, maxsplit=1, flags=re.IGNORECASE)
    if len(parts) == 2:
        winner = parts[0].strip()
        decision_match = re.search(r"elected to\s+(.+)", parts[1], re.IGNORECASE)
        decision = decision_match.group(1).strip() if decision_match else None
        return winner, decision
    return text.strip(), None


def _parse_last_updated(text: str) -> tuple[str | None, str | None]:
    """
    Parse 'Ramadhani Mbunde (16/04/2026 :: 01:46 PM)'
    Returns (updated_by, updated_at_str)
    """
    m = re.match(r"(.+?)\s*\((.+?)\)", text.strip())
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return text.strip(), None


# ══════════════════════════════════════════════════════════════════════════════
# MAIN SCRAPING FUNCTION
# ══════════════════════════════════════════════════════════════════════════════

def scrape_match_info(driver, match_id: int) -> dict | None:
    """
    Scrape one match info page.
    Returns a dict ready for Supabase upsert, or None if the page has no usable data.
    """
    url = (
        f"https://www.cricclubs.com/{CLUB_SLUG}/info.do"
        f"?matchId={match_id}&clubId={CLUB_ID}"
    )
    soup = get_soup(driver, url, wait_css="table.table")

    page_text = soup.get_text()
    if len(page_text) < 200:
        log.warning(f"  [SKIP] Sparse page for matchId={match_id}")
        return None

    # Find the match info table (Topic / Details)
    info_table = None
    for tbl in soup.find_all("table", class_="table"):
        header = tbl.find("tr")
        if header and "Topic" in header.get_text():
            info_table = tbl
            break

    if not info_table:
        log.warning(f"  [SKIP] No info table found for matchId={match_id}")
        return None

    # Build a label → content mapping from all rows
    rows = info_table.find_all("tr")[1:]  # skip header
    row_map: dict[str, BeautifulSoup] = {}
    for row in rows:
        ths = row.find_all("th")
        if len(ths) >= 2:
            label = ths[0].get_text(strip=True).rstrip(":").strip()
            row_map[label] = ths[1]  # keep the BS element so we can extract links

    if not row_map:
        log.warning(f"  [SKIP] Empty row map for matchId={match_id}")
        return None

    record: dict = {
        "match_id":   match_id,
        "scraped_at": datetime.now(timezone.utc).isoformat(),
    }

    # ── Series ────────────────────────────────────────────────────────────────
    if "Series" in row_map:
        record["series"] = row_map["Series"].get_text(strip=True) or None

    # ── Match Date ───────────────────────────────────────────────────────────
    if "Match Date" in row_map:
        record["match_date"] = _parse_match_date(row_map["Match Date"].get_text(strip=True))

    # ── Toss ─────────────────────────────────────────────────────────────────
    if "Toss" in row_map:
        toss_raw = row_map["Toss"].get_text(" ", strip=True)
        toss_text = " ".join(toss_raw.split())  # collapse all whitespace/newlines
        winner, decision = _parse_toss(toss_text)
        record["toss_winner"]   = winner
        record["toss_decision"] = decision

    # ── Player of the Match ───────────────────────────────────────────────────
    if "Player of the Match" in row_map:
        link = row_map["Player of the Match"].find("a", href=True)
        if link:
            record["player_of_match_id"]   = _player_id_from_href(link["href"])
            record["player_of_match_name"] = link.get_text(strip=True) or None

    # ── Umpires ───────────────────────────────────────────────────────────────
    # Umpires row uses nested tables, so we search the full info_table for
    # any <th> whose text contains "Umpires" and then grab links from that row
    for row in info_table.find_all("tr"):
        row_text = row.get_text()
        if "Umpires" in row_text:
            umpire_links = row.find_all("a", href=True)
            for i, ul in enumerate(umpire_links[:2]):
                uid = _umpire_id_from_href(ul["href"])
                name = ul.get_text(strip=True) or None
                if i == 0:
                    record["umpire_1_id"]   = uid
                    record["umpire_1_name"] = name
                else:
                    record["umpire_2_id"]   = uid
                    record["umpire_2_name"] = name
            break

    # ── Location ──────────────────────────────────────────────────────────────
    if "Location" in row_map:
        record["venue"] = row_map["Location"].get_text(strip=True) or None

    # ── Points Earned ─────────────────────────────────────────────────────────
    if "Points Earned" in row_map:
        pts_text = row_map["Points Earned"].get_text(" ", strip=True)
        t1, t1s, t2, t2s = _parse_points(pts_text)
        record["points_team_1"]       = t1
        record["points_team_1_score"] = t1s
        record["points_team_2"]       = t2
        record["points_team_2_score"] = t2s

    # ── Innings timings ───────────────────────────────────────────────────────
    # The timing rows have <th> elements directly in the row (no label/detail split),
    # so we need to look at the raw rows again.
    for row in rows:
        ths = row.find_all("th")
        if len(ths) < 2:
            continue
        label = ths[0].get_text(strip=True).rstrip(":").strip()
        content = ths[1].get_text(" ", strip=True)

        if "1st Innings" in label:
            dur, start, end = _parse_duration_and_times(content)
            record["innings_1_duration_min"] = dur
            record["innings_1_start"]        = start
            record["innings_1_end"]          = end

        elif "Innings break" in label or "Innings Break" in label:
            dur, start, end = _parse_duration_and_times(content)
            record["innings_break_min"]   = dur
            record["innings_break_start"] = start
            record["innings_break_end"]   = end

        elif "2nd Innings" in label:
            dur, start, end = _parse_duration_and_times(content)
            record["innings_2_duration_min"] = dur
            record["innings_2_start"]        = start
            record["innings_2_end"]          = end

        elif "Last Updated" in label:
            updated_by, updated_at = _parse_last_updated(content)
            record["last_updated_by"] = updated_by
            record["last_updated_at"] = updated_at

    log.info(f"  [OK]  matchId={match_id} — series='{record.get('series')}' date='{record.get('match_date')}'")
    return record


# ══════════════════════════════════════════════════════════════════════════════
# SUPABASE HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def get_last_match_id() -> int:
    """Return the highest match_id in tca_db_match_info, or 0 if empty."""
    res = (
        supabase.table(MATCH_INFO_TABLE)
        .select("match_id")
        .order("match_id", desc=True)
        .limit(1)
        .execute()
    )
    if res.data:
        val = int(res.data[0]["match_id"])
        log.info(f"Last match_id in {MATCH_INFO_TABLE}: {val}")
        return val
    log.info(f"No existing data in {MATCH_INFO_TABLE} — starting from match ID 1")
    return 0


def upsert_with_retry(
    records: list[dict],
    retries: int = 4,
    base_backoff: float = 5.0,
) -> None:
    BATCH = 500
    for i in range(0, len(records), BATCH):
        batch = records[i : i + BATCH]
        for attempt in range(retries):
            try:
                supabase.table(MATCH_INFO_TABLE).upsert(batch, on_conflict="match_id").execute()
                log.info(f"  Upserted rows {i}–{min(i + BATCH, len(records))} → {MATCH_INFO_TABLE}")
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
        log.info(f"[{label}] Upserting {len(buf)} match info rows…")
        upsert_with_retry(buf)


# ══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

def run_scraper(batch_size: int = BATCH_SIZE, delay: float = SCRAPE_DELAY) -> None:
    """
    Incremental scrape → upsert pipeline.
    Reads the highest match_id from tca_db_match_info to decide where to start.
    """
    log.info("═" * 60)
    log.info("Starting CricClubs match info scraper (incremental)")
    log.info("═" * 60)

    last_id  = get_last_match_id()
    start_id = last_id + 1
    end_id   = last_id + batch_size
    log.info(f"Scraping match IDs {start_id} → {end_id}  (batch_size={batch_size})")

    driver = create_driver()
    buf: list[dict] = []
    success_ids, failed_ids = [], []

    try:
        log.info("Warming up session…")
        driver.get(f"https://www.cricclubs.com/{CLUB_SLUG}/home.do?clubId={CLUB_ID}")
        time.sleep(2)

        for match_id in range(start_id, end_id + 1):
            record = scrape_match_info(driver, match_id)
            time.sleep(delay)

            if record is None:
                failed_ids.append(match_id)
            else:
                buf.append(record)
                success_ids.append(match_id)

            # Checkpoint flush
            if len(success_ids) % CHECKPOINT_EVERY == 0 and success_ids:
                flush(buf, label=f"checkpoint matchId={match_id}")
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

    parser = argparse.ArgumentParser(description="CricClubs match info scraper")
    parser.add_argument("--start",      type=int, default=None,        help="Start match ID (overrides incremental)")
    parser.add_argument("--end",        type=int, default=None,        help="End match ID (inclusive)")
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE,  help="How many IDs to attempt")
    parser.add_argument("--delay",      type=float, default=SCRAPE_DELAY, help="Seconds between requests")
    args = parser.parse_args()

    if args.start and args.end:
        log.info(f"Manual range: {args.start} → {args.end}")
        driver = create_driver()
        driver.get(f"https://www.cricclubs.com/{CLUB_SLUG}/home.do?clubId={CLUB_ID}")
        time.sleep(2)

        buf: list[dict] = []
        success_ids, failed_ids = [], []

        try:
            for match_id in range(args.start, args.end + 1):
                record = scrape_match_info(driver, match_id)
                time.sleep(args.delay)

                if record is None:
                    failed_ids.append(match_id)
                else:
                    buf.append(record)
                    success_ids.append(match_id)

                if len(success_ids) % CHECKPOINT_EVERY == 0 and success_ids:
                    flush(buf, label=f"checkpoint matchId={match_id}")
                    buf.clear()
        finally:
            driver.quit()

        flush(buf, label="final flush")
        log.info(f"Done ✓  succeeded={len(success_ids)}  failed={len(failed_ids)}")
        if failed_ids:
            log.warning(f"Failed IDs: {failed_ids}")
    else:
        run_scraper(batch_size=args.batch_size, delay=args.delay)