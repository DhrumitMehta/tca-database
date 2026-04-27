"""
scorecard_scraper.py
--------------------
Scrapes batting and bowling scorecard data from CricClubs and upserts into Supabase.
Runs incrementally: queries Supabase for the highest existing match_id,
then scrapes the next `batch_size` match IDs from there.

Tables used:
    tca_db_scorecard_batting   — one row per batter per innings
    tca_db_scorecard_bowling   — one row per bowler per innings

Setup:
    pip install selenium beautifulsoup4 supabase python-dotenv

Environment variables (set in .env or your deployment environment):
    SUPABASE_URL = https://xxxx.supabase.co
    SUPABASE_KEY = your-service-role-or-anon-key

Supabase table DDL (run once in SQL editor):
─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS tca_db_scorecard_batting (
    id               BIGSERIAL PRIMARY KEY,
    match_id         INTEGER      NOT NULL,
    inning_number    INTEGER      NOT NULL,
    event_name       TEXT,
    match_format     VARCHAR(10),
    batting_team     TEXT,
    bowling_team     TEXT,
    winning_team     TEXT,
    batter_id        TEXT,
    batter_name      TEXT,
    dismissal_type   TEXT,
    fielder_id       TEXT,
    fielder_name     TEXT,
    bowler_id        TEXT,
    bowler_name      TEXT,
    runs             INTEGER      DEFAULT 0,
    balls            INTEGER      DEFAULT 0,
    fours            INTEGER      DEFAULT 0,
    sixes            INTEGER      DEFAULT 0,
    strike_rate      NUMERIC(6,2),
    byes             INTEGER      DEFAULT 0,
    leg_byes         INTEGER      DEFAULT 0,
    scraped_at       TIMESTAMPTZ  DEFAULT NOW(),
    UNIQUE (match_id, inning_number, batting_team, batter_id)
);

CREATE TABLE IF NOT EXISTS tca_db_scorecard_bowling (
    id               BIGSERIAL PRIMARY KEY,
    match_id         INTEGER      NOT NULL,
    inning_number    INTEGER      NOT NULL,
    event_name       TEXT,
    match_format     VARCHAR(10),
    batting_team     TEXT,
    bowling_team     TEXT,
    winning_team     TEXT,
    bowler_id        TEXT,
    bowler_name      TEXT,
    overs            NUMERIC(5,1),
    maidens          INTEGER      DEFAULT 0,
    runs             INTEGER      DEFAULT 0,
    wickets          INTEGER      DEFAULT 0,
    economy          NUMERIC(6,2),
    wides            INTEGER      DEFAULT 0,
    no_balls         INTEGER      DEFAULT 0,
    scraped_at       TIMESTAMPTZ  DEFAULT NOW(),
    UNIQUE (match_id, inning_number, bowling_team, bowler_id)
);
─────────────────────────────────────────────────────────────────────────────
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
CLUB_ID    = 7605
CLUB_SLUG  = "Tanzania"

BATCH_SIZE        = 1000   # match IDs to attempt per run
CHECKPOINT_EVERY  = 20    # flush to Supabase every N successful matches
SCRAPE_DELAY      = 1.5   # seconds between requests

BATTING_TABLE = "tca_db_scorecard_batting"
BOWLING_TABLE = "tca_db_scorecard_bowling"

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


def get_soup(driver, url: str, wait_css: str = None, timeout: int = 15) -> BeautifulSoup:
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


# ══════════════════════════════════════════════════════════════════════════════
# PARSING HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _safe_int(val, default=0) -> int:
    try:
        return int(str(val).strip())
    except (ValueError, TypeError):
        return default


def _safe_float(val, default=None):
    try:
        return float(str(val).strip())
    except (ValueError, TypeError):
        return default


def detect_match_format(team_info_texts: list[str]) -> str | None:
    """Detect T10 / T20 / T30 / T40 / T50 from the team info paragraph text."""
    for fmt in ["10", "20", "30", "40", "50"]:
        if all(fmt in t for t in team_info_texts):
            return f"T{fmt}"
    for text in team_info_texts:
        m = re.findall(r"(\d+)(?:\.\d+)?(?:/\d+(?:\.\d+)?)?\s*ov", text)
        if m and m[0] in ["10", "20", "30", "40", "50"]:
            return f"T{m[0]}"
    return None


def extract_byes_legbyes(batting_table) -> tuple[int, int]:
    """Pull byes / leg-byes out of the Extras row of a batting table."""
    byes = leg_byes = 0
    for row in batting_table.find_all("tr"):
        first_th = row.find("th")
        if first_th and "Extras" in first_th.get_text():
            extras_th = row.find("th", class_="hidden-phone")
            if extras_th:
                txt = extras_th.get_text(strip=True)
                m = re.search(r"\(b\s+(\d+)", txt)
                if m:
                    byes = int(m.group(1))
                m = re.search(r"lb\s+(\d+)", txt)
                if m:
                    leg_byes = int(m.group(1))
            break
    return byes, leg_byes


def extract_bowling_extras(extras_text: str) -> tuple[int, int]:
    """Extract wides and no-balls from a bowling row extras cell."""
    wides = no_balls = 0
    if extras_text:
        m = re.search(r"(\d+)\s*w\b", extras_text)
        if m:
            wides = int(m.group(1))
        m = re.search(r"(\d+)\s*nb\b", extras_text)
        if m:
            no_balls = int(m.group(1))
    return wides, no_balls


# Dismissal type normalisation — ordered longest-first so short prefixes
# like "b" don't accidentally swallow "bowled" / "b " etc.
_DISMISSAL_MAP = [
    ("caught & bowled",       "caught_and_bowled"),
    ("c&b",                   "caught_and_bowled"),
    ("c †",                   "wktKpr_catch"),
    ("hit wicket",            "hit_wicket"),
    ("obstructing the field", "obstructing_the_field"),
    ("obstructing",           "obstructing_the_field"),
    ("retired hurt",          "retired_hurt"),
    ("retired",               "retired_hurt"),
    ("not out",               "not_out"),
    ("run out",               "run_out"),
    ("stumped",               "stumped"),
    ("lbw b",                 "lbw"),
    ("lbw",                   "lbw"),
    ("c ",                    "caught"),       # "c " prefix → caught (fielder follows)
    ("b ",                    "bowled"),       # "b " prefix → bowled
    ("b\t",                   "bowled"),
    # bare single letters that appear when the dismissal div only contains "b" or "c"
    ("b",                     "bowled"),
    ("c",                     "caught"),
    ("st ",                   "stumped"),
    ("st\t",                  "stumped"),
    ("st",                    "stumped"),
]

def normalise_dismissal(raw: str) -> str:
    if not raw:
        return "not_out"
    lower = raw.strip().lower()
    for key, val in _DISMISSAL_MAP:
        if lower == key or lower.startswith(key):
            return val
    return raw.strip()


def _player_id_from_href(href: str) -> str | None:
    """Extract numeric playerId from a /player URL. Returns None for javascript: hrefs."""
    if not href or href.strip().startswith("javascript:"):
        return None
    if "playerId=" in href:
        return href.split("playerId=")[-1].split("&")[0]
    return None


def _clean_name(name: str | None) -> str | None:
    """Strip captaincy asterisk and surrounding whitespace from a player name."""
    if not name:
        return name
    return name.replace("*", "").strip()


# ══════════════════════════════════════════════════════════════════════════════
# MAIN SCRAPING FUNCTION
# ══════════════════════════════════════════════════════════════════════════════

def scrape_scorecard(
    driver, match_id: int
) -> tuple[list[dict], list[dict]] | tuple[None, None]:
    """
    Scrape one match scorecard.

    Returns (batting_rows, bowling_rows) — both lists of dicts ready for
    Supabase upsert — or (None, None) if the page has no usable data.
    """
    # ── Step 1: match info page (format + team names) ────────────────────────
    info_url = (
        f"https://www.cricclubs.com/{CLUB_SLUG}/info.do"
        f"?matchId={match_id}&clubId={CLUB_ID}"
    )
    info_soup = get_soup(driver, info_url, wait_css="li.win, li.lose")

    if len(info_soup.get_text()) < 200:
        log.warning(f"  [SKIP] Sparse info page for matchId={match_id}")
        return None, None

    # Collect team names and format
    team_sections = info_soup.find_all("li", class_=["win", "lose"])
    if len(team_sections) < 2:
        log.warning(f"  [SKIP] <2 team sections for matchId={match_id}")
        return None, None

    team_names: list[str] = []
    team_info_texts: list[str] = []
    for sec in team_sections[:2]:
        span = sec.find("span", class_="teamName")
        if span:
            team_names.append(span.get_text(strip=True))
        p = sec.find("p")
        if p:
            team_info_texts.append(p.get_text(strip=True))

    match_format = detect_match_format(team_info_texts) if len(team_info_texts) == 2 else None
    if not match_format:
        log.warning(f"  [SKIP] Could not detect format for matchId={match_id}")
        return None, None

    # ── Step 2: scorecard page ───────────────────────────────────────────────
    sc_url = (
        f"https://www.cricclubs.com/{CLUB_SLUG}/viewScorecard.do"
        f"?matchId={match_id}&clubId={CLUB_ID}"
    )
    sc_soup = get_soup(driver, sc_url, wait_css="div[id^='ballByBallTeam']")

    # Event name
    event_el = sc_soup.select_one("div.match-summary h3 strong")
    event_name = event_el.get_text(strip=True) if event_el else ""

    # Winning team
    winning_team = None
    summary = sc_soup.find("div", class_="match-summary")
    if summary:
        for li in summary.find_all("li"):
            if "win" in li.get("class", []):
                sp = li.find("span", class_="teamName")
                if sp:
                    winning_team = sp.get_text(strip=True)
                break

    # Team order: innings 1 bats first, innings 2 bats second
    # The scorecard divs are ballByBallTeam1, ballByBallTeam2
    batting_order: list[str] = []
    for li in sc_soup.find_all("li", id=lambda x: x and x.startswith("ballByBallTeamTab")):
        a = li.find("a")
        if a:
            batting_order.append(a.get_text(strip=True))

    # Fallback: use team_names order
    if len(batting_order) < 2:
        batting_order = team_names[:2] if len(team_names) >= 2 else ["Team1", "Team2"]

    batting_rows: list[dict] = []
    bowling_rows: list[dict] = []

    for inning_number in range(1, 3):
        div_id = f"ballByBallTeam{inning_number}"
        section = sc_soup.find("div", id=div_id)
        if not section:
            log.info(f"    No section {div_id} for matchId={match_id}")
            continue

        batting_team = batting_order[inning_number - 1] if inning_number - 1 < len(batting_order) else ""
        bowling_team = batting_order[2 - inning_number] if 2 - inning_number < len(batting_order) else ""

        common = dict(
            match_id=match_id,
            inning_number=inning_number,
            event_name=event_name,
            match_format=match_format,
            batting_team=batting_team,
            bowling_team=bowling_team,
            winning_team=winning_team,
            scraped_at=datetime.now(timezone.utc).isoformat(),
        )

        tables = section.find_all("table", class_="table")

        for table in tables:
            header_row = table.find("tr")
            if not header_row:
                continue
            header_text = header_row.get_text(" ", strip=True).lower()

            # ── Batting table ────────────────────────────────────────────────
            if "batting" in header_text or "r" in header_text and "b" in header_text and "sr" in header_text:
                byes, leg_byes = extract_byes_legbyes(table)

                for row in table.find_all("tr")[1:]:
                    link = row.find("a", href=True)
                    if not link:
                        continue
                    batter_id   = _player_id_from_href(link["href"])
                    batter_name = _clean_name(link.get_text(strip=True))

                    # Dismissal info
                    dismissal_div = row.find("div", class_="scorecard-out-text")
                    if dismissal_div:
                        raw_dismissal = (dismissal_div.find(string=True, recursive=False) or "").strip()
                        dismissal_type = normalise_dismissal(raw_dismissal)

                        # Only keep links that point to real player pages (not javascript:)
                        player_links = [
                            a for a in dismissal_div.find_all("a", href=True)
                            if _player_id_from_href(a["href"]) is not None
                        ]

                        if dismissal_type == "run_out":
                            # run out: first link = fielder, second = (sometimes) bowler
                            fielder_id   = _player_id_from_href(player_links[0]["href"]) if len(player_links) > 0 else None
                            fielder_name = _clean_name(player_links[0].get_text(strip=True)) if len(player_links) > 0 else None
                            bowler_id    = _player_id_from_href(player_links[1]["href"])  if len(player_links) > 1 else None
                            bowler_name  = _clean_name(player_links[1].get_text(strip=True))  if len(player_links) > 1 else None
                        else:
                            # caught / bowled / lbw / stumped etc:
                            # last real link = bowler; if 2+ links, first = fielder
                            fielder_id   = _player_id_from_href(player_links[0]["href"])  if len(player_links) > 1 else None
                            fielder_name = _clean_name(player_links[0].get_text(strip=True))  if len(player_links) > 1 else None
                            bowler_id    = _player_id_from_href(player_links[-1]["href"]) if player_links else None
                            bowler_name  = _clean_name(player_links[-1].get_text(strip=True)) if player_links else None
                    else:
                        dismissal_type = "not_out"
                        fielder_id = fielder_name = bowler_id = bowler_name = None

                    # Stats: Runs, Balls, Fours, Sixes, SR
                    stat_cells = row.find_all("th")[2:]
                    stats = [c.get_text(strip=True) for c in stat_cells]

                    batting_rows.append({
                        **common,
                        "batter_id":      batter_id,
                        "batter_name":    batter_name,
                        "dismissal_type": dismissal_type,
                        "fielder_id":     fielder_id,
                        "fielder_name":   fielder_name,
                        "bowler_id":      bowler_id,
                        "bowler_name":    bowler_name,
                        "runs":           _safe_int(stats[0]) if len(stats) > 0 else 0,
                        "balls":          _safe_int(stats[1]) if len(stats) > 1 else 0,
                        "fours":          _safe_int(stats[2]) if len(stats) > 2 else 0,
                        "sixes":          _safe_int(stats[3]) if len(stats) > 3 else 0,
                        "strike_rate":    _safe_float(stats[4])if len(stats) > 4 else None,
                        "byes":           byes,
                        "leg_byes":       leg_byes,
                    })

            # ── Bowling table ────────────────────────────────────────────────
            elif "bowling" in header_text or "ov" in header_text and "wk" in header_text:
                for row in table.find_all("tr")[1:]:
                    link = row.find("a", href=True)
                    if not link:
                        continue
                    href = link["href"]
                    bowler_id   = _player_id_from_href(href)
                    bowler_name = _clean_name(link.get_text(strip=True))

                    # Stats cells: Overs, Maidens, [Dots], Runs, Wickets, Econ, [Extras]
                    stat_cells = row.find_all(["th", "td"])[2:]
                    stats = [c.get_text(strip=True) for c in stat_cells]

                    # Detect whether a Dots column is present (7+ cols → dots present)
                    has_dots = len(stats) >= 7
                    if has_dots:
                        overs, maidens, _dots, runs, wickets, economy = (
                            stats[0], stats[1], stats[2], stats[3], stats[4], stats[5]
                        )
                        extras_text = stats[6] if len(stats) > 6 else ""
                    else:
                        overs, maidens, runs, wickets, economy = (
                            stats[0], stats[1], stats[2], stats[3], stats[4]
                        )
                        extras_text = stats[5] if len(stats) > 5 else ""

                    wides, no_balls = extract_bowling_extras(extras_text)

                    bowling_rows.append({
                        **common,
                        "bowler_id":   bowler_id,
                        "bowler_name": bowler_name,
                        "overs":       _safe_float(overs, 0),
                        "maidens":     _safe_int(maidens),
                        "runs":        _safe_int(runs),
                        "wickets":     _safe_int(wickets),
                        "economy":     _safe_float(economy),
                        "wides":       wides,
                        "no_balls":    no_balls,
                    })

    if not batting_rows and not bowling_rows:
        log.warning(f"  [SKIP] No batting or bowling data parsed for matchId={match_id}")
        return None, None

    log.info(
        f"  [OK]  matchId={match_id} — "
        f"{len(batting_rows)} batting rows, {len(bowling_rows)} bowling rows"
    )
    return batting_rows, bowling_rows


# ══════════════════════════════════════════════════════════════════════════════
# SUPABASE HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def get_last_match_id(table: str) -> int:
    """Return the highest match_id in *table*, or 0 if empty."""
    res = (
        supabase.table(table)
        .select("match_id")
        .order("match_id", desc=True)
        .limit(1)
        .execute()
    )
    if res.data:
        val = int(res.data[0]["match_id"])
        log.info(f"Last match_id in {table}: {val}")
        return val
    log.info(f"No existing data in {table} — starting from match ID 1")
    return 0


def upsert_with_retry(
    table: str,
    records: list[dict],
    conflict_cols: str,
    retries: int = 4,
    base_backoff: float = 5.0,
) -> None:
    BATCH = 500
    for i in range(0, len(records), BATCH):
        batch = records[i : i + BATCH]
        for attempt in range(retries):
            try:
                supabase.table(table).upsert(batch, on_conflict=conflict_cols).execute()
                log.info(f"  Upserted rows {i}–{min(i + BATCH, len(records))} → {table}")
                break
            except Exception as e:
                if attempt < retries - 1:
                    wait = base_backoff * (2 ** attempt)
                    log.warning(f"  Upsert failed ({attempt+1}/{retries}): {e}. Retry in {wait}s…")
                    time.sleep(wait)
                else:
                    log.error(f"  Upsert failed after {retries} attempts. Giving up on batch.")
                    raise


def _dedup(records: list[dict], key_cols: list[str]) -> list[dict]:
    """
    Remove duplicate rows within a batch that share the same conflict key.
    Last occurrence wins (most recently scraped data kept).
    Postgres cannot upsert two rows with the same conflict key in one batch.
    """
    seen: dict[tuple, dict] = {}
    for row in records:
        k = tuple(row.get(c) for c in key_cols)
        seen[k] = row  # overwrite → last wins
    deduped = list(seen.values())
    dropped = len(records) - len(deduped)
    if dropped:
        log.warning(f"  Deduped {dropped} duplicate row(s) before upsert (key={key_cols})")
    return deduped


def flush(
    batting_buf: list[dict],
    bowling_buf: list[dict],
    label: str = "flush",
) -> None:
    if batting_buf:
        clean = _dedup(batting_buf, ["match_id", "inning_number", "batting_team", "batter_id"])
        log.info(f"[{label}] Upserting {len(clean)} batting rows…")
        upsert_with_retry(
            BATTING_TABLE,
            clean,
            "match_id,inning_number,batting_team,batter_id",
        )
    if bowling_buf:
        clean = _dedup(bowling_buf, ["match_id", "inning_number", "bowling_team", "bowler_id"])
        log.info(f"[{label}] Upserting {len(clean)} bowling rows…")
        upsert_with_retry(
            BOWLING_TABLE,
            clean,
            "match_id,inning_number,bowling_team,bowler_id",
        )


# ══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

def run_scraper(
    club_id: int      = CLUB_ID,
    batch_size: int   = BATCH_SIZE,
    delay: float      = SCRAPE_DELAY,
) -> None:
    """
    Incremental scrape → upsert pipeline.
    Reads the highest match_id from scorecard_batting to decide where to start.
    """
    log.info("═" * 60)
    log.info("Starting CricClubs scorecard scraper (incremental)")
    log.info("═" * 60)

    last_id  = get_last_match_id(BATTING_TABLE)
    start_id = last_id + 1
    end_id   = last_id + batch_size
    log.info(f"Scraping match IDs {start_id} → {end_id}  (batch_size={batch_size})")

    driver = create_driver()
    batting_buf: list[dict] = []
    bowling_buf: list[dict] = []
    success_ids, failed_ids = [], []

    try:
        log.info("Warming up session…")
        driver.get(f"https://www.cricclubs.com/{CLUB_SLUG}/home.do?clubId={club_id}")
        time.sleep(2)

        for match_id in range(start_id, end_id + 1):
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

            # Checkpoint flush
            if len(success_ids) % CHECKPOINT_EVERY == 0 and success_ids:
                flush(batting_buf, bowling_buf, label=f"checkpoint matchId={match_id}")
                batting_buf.clear()
                bowling_buf.clear()

    finally:
        driver.quit()

    log.info(f"\nScraped: {len(success_ids)} succeeded | {len(failed_ids)} skipped/failed")
    if failed_ids:
        log.warning(f"Skipped IDs: {failed_ids}")

    if batting_buf or bowling_buf:
        flush(batting_buf, bowling_buf, label="final flush")
    else:
        log.info("No remaining data to flush.")

    log.info("Done ✓")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="CricClubs scorecard scraper")
    parser.add_argument("--start",      type=int, default=None,        help="Start match ID (overrides incremental)")
    parser.add_argument("--end",        type=int, default=None,        help="End match ID (inclusive)")
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE,  help="How many IDs to attempt")
    parser.add_argument("--delay",      type=float, default=SCRAPE_DELAY, help="Seconds between requests")
    args = parser.parse_args()

    if args.start and args.end:
        # Manual range mode
        log.info(f"Manual range: {args.start} → {args.end}")
        driver = create_driver()
        driver.get(f"https://www.cricclubs.com/{CLUB_SLUG}/home.do?clubId={CLUB_ID}")
        time.sleep(2)

        batting_buf: list[dict] = []
        bowling_buf: list[dict] = []
        success_ids, failed_ids = [], []

        try:
            for match_id in range(args.start, args.end + 1):
                bat, bowl = scrape_scorecard(driver, match_id)
                time.sleep(args.delay)
                if bat is None and bowl is None:
                    failed_ids.append(match_id)
                else:
                    if bat:
                        batting_buf.extend(bat)
                    if bowl:
                        bowling_buf.extend(bowl)
                    success_ids.append(match_id)

                if len(success_ids) % CHECKPOINT_EVERY == 0 and success_ids:
                    flush(batting_buf, bowling_buf, label=f"checkpoint matchId={match_id}")
                    batting_buf.clear()
                    bowling_buf.clear()
        finally:
            driver.quit()

        flush(batting_buf, bowling_buf, label="final flush")
        log.info(f"Done ✓  succeeded={len(success_ids)}  failed={len(failed_ids)}")
        if failed_ids:
            log.warning(f"Failed IDs: {failed_ids}")
    else:
        run_scraper(batch_size=args.batch_size, delay=args.delay)