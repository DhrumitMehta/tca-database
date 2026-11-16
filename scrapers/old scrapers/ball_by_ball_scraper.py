"""
cricclubs_scraper.py
--------------------
Scrapes ball-by-ball data from CricClubs and upserts it into Supabase.
Runs incrementally: queries Supabase for the highest existing match_id,
then scrapes the next `batch_size` match IDs from there.

Setup:
    pip install selenium beautifulsoup4 pandas supabase python-dotenv

Environment variables (set in .env or your deployment environment):
    SUPABASE_URL      = https://xxxx.supabase.co
    SUPABASE_KEY      = your-service-role-or-anon-key
"""

import os
import re
import time
import logging
from dotenv import load_dotenv

import pandas as pd
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
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
TANZANIA      = 7605
UGANDA        = 5335
INTERNATIONAL = 11707

CLUB = TANZANIA

# How many match IDs to attempt per run
BATCH_SIZE = 100

# Flush to Supabase every N successfully scraped matches
CHECKPOINT_EVERY = 20

# Supabase table name
TABLE_NAME = "tca_db_ball_by_ball"

# Scrape delay between requests (seconds)
SCRAPE_DELAY = 1.5

# ══════════════════════════════════════════════════════════════════════════════
# SELENIUM HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def create_driver() -> webdriver.Chrome:
    options = Options()
    options.add_argument("--headless")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1920,1080")
    options.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
    return webdriver.Chrome(options=options)


def get_page_soup(driver, url: str, wait_for: str = None, timeout: int = 10) -> BeautifulSoup:
    driver.get(url)
    if wait_for:
        try:
            WebDriverWait(driver, timeout).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, wait_for))
            )
        except Exception:
            pass
    time.sleep(1.5)
    return BeautifulSoup(driver.page_source, "html.parser")


# ══════════════════════════════════════════════════════════════════════════════
# SCRAPING
# ══════════════════════════════════════════════════════════════════════════════

def scrape_raw_ball_by_ball(driver, match_id: int, club_id: int) -> pd.DataFrame | None:
    url = (
        f"https://www.cricclubs.com/Tanzania/ballbyball.do"
        f"?matchId={match_id}&clubId={club_id}"
    )
    soup = get_page_soup(driver, url, wait_for="ul.list-inline.bbb-row")

    event_element = soup.select_one("div.match-summary h3 strong")
    if event_element is None:
        log.warning(f"  [SKIP] Event name not found for matchId={match_id}")
        return None

    event_name = event_element.text.strip()

    rows = soup.find_all("ul", class_="list-inline bbb-row")
    if not rows:
        log.warning(f"  [SKIP] No ball-by-ball data for matchId={match_id}")
        return None

    over_numbers, runs_obtained, batter_bowler, additional_infos = [], [], [], []

    for el in rows:
        ov = el.find("span", class_="ov")
        over_numbers.append(ov.text.strip() if ov else "N/A")

        runs_span   = el.find("span", class_="runs")
        zero_span   = el.find("span", class_="zero")
        wicket_span = el.find("span", class_="wicket")
        if runs_span:
            runs_obtained.append(runs_span.text.strip())
        elif zero_span:
            runs_obtained.append("0")
        elif wicket_span:
            runs_obtained.append(wicket_span.text.strip())
        else:
            runs_obtained.append("N/A")

        bb = el.find("li", class_="col3")
        batter_bowler.append(bb.text.strip() if bb else "N/A")

        ai = el.find("span", class_="hidden-phone")
        additional_infos.append(ai.text.strip() if ai else "N/A")

    return pd.DataFrame({
        "Over Number":       over_numbers,
        "Runs Obtained":     runs_obtained,
        "Batter and Bowler": batter_bowler,
        "Additional Info":   additional_infos,
        "Event Name":        event_name,
        "Match ID":          match_id,
    })


def get_team_innings_mapping(driver, match_id: int, club_id: int) -> pd.DataFrame | None:
    url = (
        f"https://www.cricclubs.com/Tanzania/ballbyball.do"
        f"?matchId={match_id}&clubId={club_id}"
    )
    soup = get_page_soup(driver, url, wait_for="li[id^='ballByBallTeamTab']")

    batting_team_elements = soup.find_all(
        "li", id=lambda x: x and x.startswith("ballByBallTeamTab")
    )
    batting_teams  = [el.find("a").text.strip() for el in batting_team_elements]
    inning_numbers = list(range(1, len(batting_teams) + 1))

    if not batting_teams:
        log.warning(f"  [SKIP] No innings tab data for matchId={match_id}")
        return None

    if len(batting_teams) > 1:
        bowling_team = batting_teams[::-1]
    else:
        team_elements = soup.find_all("li", class_="win")
        team_names    = [el.find("span", class_="teamName").text.strip() for el in team_elements]
        bowling_team  = [t for t in team_names if t not in batting_teams]

    if len(bowling_team) != len(batting_teams):
        log.warning(
            f"  [SKIP] Innings/team length mismatch for matchId={match_id} "
            f"(batting={len(batting_teams)}, bowling={len(bowling_team)})"
        )
        return None

    return pd.DataFrame({
        "Match ID":      match_id,
        "inning_number": inning_numbers,
        "batting_team":  batting_teams,
        "bowling_team":  bowling_team,
    })


# ══════════════════════════════════════════════════════════════════════════════
# CLEANING & TRANSFORMATION
# ══════════════════════════════════════════════════════════════════════════════

def clean_ball_df(ball_df: pd.DataFrame, team_innings_df: pd.DataFrame) -> pd.DataFrame:

    # ── Build playing squad DataFrame from "Players:" rows ───────────────────
    playing_squad_rows = ball_df[ball_df["Batter and Bowler"].str.contains("Players:", na=False)].copy()

    def extract_players_and_captain(player_string):
        result = {"team": {}, "players": {}, "captain": {}}
        if isinstance(player_string, str):
            parts = player_string.split(":")
            if len(parts) > 1:
                team_name    = parts[0].strip().replace(" Players", "")
                player_names = [name.strip() for name in parts[1].split(",")]
                result["team"] = team_name
                captain_name   = next((name.replace("*", "") for name in player_names if "*" in name), None)
                if captain_name:
                    result["captain"] = captain_name
                player_names     = [p.replace("*", "") for p in player_names]
                result["players"] = player_names
        return result

    squad_results = []
    for _, row in playing_squad_rows.iterrows():
        match_result = extract_players_and_captain(row["Batter and Bowler"])
        squad_results.append({"Match ID": row["Match ID"], **match_result})

    if squad_results:
        new_playing_squad_df = pd.DataFrame(squad_results)
    else:
        new_playing_squad_df = pd.DataFrame(columns=["Match ID", "team", "players", "captain", "full name", "short name"])

    full_names_col, short_names_col = [], []
    for _, row in new_playing_squad_df.iterrows():
        players_list    = row["players"] if isinstance(row["players"], list) else []
        team_full_names, team_short_names = [], []
        for player in players_list:
            parts      = player.split()
            full_name  = " ".join(parts)
            short_name = f"{parts[0][0]} {parts[-1]}" if len(parts) > 1 else player
            team_full_names.append(full_name)
            team_short_names.append(short_name)
        full_names_col.append(team_full_names)
        short_names_col.append(team_short_names)
    new_playing_squad_df["full name"]  = full_names_col
    new_playing_squad_df["short name"] = short_names_col

    # ── Build full_names_df from "comes to the crease" rows ──────────────────
    dfs_to_concat = []
    for _, row in ball_df.iterrows():
        if pd.isna(row["Batter and Bowler"]):
            continue
        val = row["Batter and Bowler"]
        if "comes into the attack" in val or "comes to the crease" in val:
            full_name_parts = val.split(",")[0]
            full_name_parts = re.sub(r" comes to the crease| comes into the attack|\d+", "", full_name_parts).strip()
            dfs_to_concat.append(pd.DataFrame({"full name": [full_name_parts], "Match ID": [row["Match ID"]]}))

    if dfs_to_concat:
        full_names_df = pd.concat(dfs_to_concat, ignore_index=True)
        full_names_df["short name"] = full_names_df["full name"].apply(
            lambda x: f"{x.split()[0][0]} {x.split()[-1]}" if len(x.split()) > 1 else x
        )
        full_names_df = full_names_df.drop_duplicates()
    else:
        full_names_df = pd.DataFrame(columns=["full name", "short name", "Match ID"])

    player_full_names  = full_names_df["full name"].tolist()
    player_short_names = full_names_df["short name"].tolist()

    # ── Filter to rows that have actual ball data ─────────────────────────────
    df = ball_df[~pd.isna(ball_df["Runs Obtained"])].copy()
    df = df[~pd.isna(df["Over Number"])].copy()
    df["Runs Obtained"] = df["Runs Obtained"].replace(["", "N/A"], pd.NA)
    df = df.dropna(subset=["Runs Obtained"]).copy()

    # ── Over / delivery split ─────────────────────────────────────────────────
    df["Over Number"] = df["Over Number"].astype(str)
    df["over"]     = df["Over Number"].apply(lambda x: x.split(".")[0] if "." in x and x.split(".")[0].isdigit() else "0")
    df["delivery"] = df["Over Number"].apply(lambda x: x.split(".")[1] if "." in x else "0")

    # ── Batter / bowler extract ───────────────────────────────────────────────
    df["batter"]   = df["Batter and Bowler"].apply(lambda x: x.split(" to ")[1].split(", ")[0] if " to " in x else None)
    df["bowler"]   = df["Batter and Bowler"].apply(lambda x: x.split(" to ")[0] if " to " in x else None)
    df["Comments"] = df["Batter and Bowler"]

    df = df[["Match ID", "Event Name", "over", "delivery", "batter", "bowler", "Comments", "Runs Obtained", "Additional Info"]]

    # ── Over numbers start from 1 ─────────────────────────────────────────────
    overs = []
    for over in df["over"]:
        try:
            overs.append(int(over) + 1)
        except ValueError:
            overs.append(1)
    df["over"] = overs

    # ── Inning detection ──────────────────────────────────────────────────────
    inning_numbers = []
    for match_id in df["Match ID"].unique():
        inning_number = 1
        match_df      = df[df["Match ID"] == match_id]
        prev_over     = None
        for _, row in match_df.iterrows():
            if prev_over is not None and int(row["over"]) < int(prev_over):
                inning_number += 1
            inning_numbers.append(inning_number)
            prev_over = int(row["over"])   # <-- cast to int to avoid string comparison bugs
    df["inning_number"] = inning_numbers

    # ── Merge team / innings mapping ──────────────────────────────────────────
    team_innings_df["inning_number"] = team_innings_df["inning_number"].astype(int)
    df = df.merge(team_innings_df, on=["Match ID", "inning_number"], how="left")

    # ── Clean batter column ───────────────────────────────────────────────────
    df["batter"] = df["batter"].str.replace("WIDES", "").str.replace("WIDE", "").str.replace("NO BALL", "")
    df["batter"] = df["batter"].str.extract(r"^(.*?)(OUT|$)", expand=False)[0].str.strip()
    df["batter"] = df["batter"].str.replace(r"\d+", "", regex=True).str.strip()

    # ── Handle RETIRED balls ──────────────────────────────────────────────────
    df = df.reset_index(drop=True)
    for i in range(len(df)):
        if pd.notna(df.loc[i, "Comments"]) and "RETIRED" in str(df.loc[i, "Comments"]):
            comment     = df.loc[i, "Comments"]
            delivery_no = df.loc[i, "delivery"]
            batter_name = comment.split(",")[0].split("\n")[0].strip()
            try:
                if int(delivery_no) > 1:
                    df.loc[i, "bowler"] = df.loc[i - 1, "bowler"]
                else:
                    df.loc[i, "bowler"] = df.loc[i + 1, "bowler"]
            except Exception:
                pass
            df.loc[i, "batter"] = batter_name

    # ── Split into wide / out / other rows ────────────────────────────────────
    wide_mask  = df["Comments"].str.contains("WIDE|WIDES", na=False)
    out_mask   = df["Comments"].str.contains("OUT", na=False)

    wide_rows  = df[wide_mask & ~out_mask].copy()
    out_rows   = df[out_mask].copy()
    other_rows = df[~wide_mask & ~out_mask].copy()

    # ── Process OTHER rows ────────────────────────────────────────────────────
    other_rows[["ball_outcome", "ball_details"]] = other_rows["Comments"].str.split(",", n=1, expand=True)

    def extract_number(text):
        if not isinstance(text, str):
            return None
        for word in text.split():
            if word.isdigit():
                return int(word)
        return None

    other_rows["extras_runs"] = other_rows["ball_details"].apply(lambda x: extract_number(x) if isinstance(x, str) and "LEG BYE" in x else None)
    other_rows["extras_type"] = other_rows["ball_details"].apply(lambda x: "legbyes" if isinstance(x, str) and "LEG BYE" in x else None)
    other_rows["extras_runs"] = other_rows["extras_runs"].fillna(other_rows["ball_details"].apply(lambda x: extract_number(x) if isinstance(x, str) and "BYE" in x else None))
    other_rows["extras_type"] = other_rows["extras_type"].fillna(other_rows["ball_details"].apply(lambda x: "byes" if isinstance(x, str) and "BYE" in x else None))
    other_rows["extras_runs"] = other_rows["extras_runs"].fillna(other_rows["ball_details"].apply(lambda x: 1 if isinstance(x, str) and "NO BALL" in x else None))
    other_rows["extras_type"] = other_rows["extras_type"].fillna(other_rows["ball_details"].apply(lambda x: "noballs" if isinstance(x, str) and "NO BALL" in x else None))
    other_rows["total_runs"]  = other_rows["ball_details"].apply(lambda x: extract_number(x) if isinstance(x, str) else None)

    def calculate_batter_runs_other(row):
        if row["extras_type"] in ["legbyes", "byes"]:
            return 0
        elif row["extras_type"] == "noballs":
            return (row["total_runs"] or 0) - 1
        else:
            return row["total_runs"]

    other_rows["batter_runs"]   = other_rows.apply(calculate_batter_runs_other, axis=1)
    other_rows["wickets"]       = 0
    other_rows["wicket_type"]   = None
    other_rows["player_out"]    = None
    other_rows["bowler_wicket"] = 0

    for idx, row in other_rows.iterrows():
        if "RETIRED" in str(row["Comments"]):
            comment      = row["Comments"]
            retired_name = comment.split("\n")[-1].split(" is RETIRED")[0].strip()
            other_rows.loc[idx, "player_out"]  = retired_name
            other_rows.loc[idx, "wicket_type"] = "retired_hurt"
            for col in ["total_runs", "batter_runs", "extras_runs"]:
                if pd.isnull(other_rows.loc[idx, col]):
                    other_rows.loc[idx, col] = 0
            if pd.isnull(other_rows.loc[idx, "extras_type"]):
                other_rows.loc[idx, "extras_type"] = None
        if "PENALTY" in str(row["Comments"]):
            other_rows.loc[idx, "batter_runs"] = 0
            other_rows.loc[idx, "extras_type"] = "penalty_runs"

    # ── Process WIDE rows ─────────────────────────────────────────────────────
    wide_rows[["ball_outcome", "ball_details"]] = wide_rows["Comments"].str.extract(r"(\d*)\s*(WIDE|WIDES)")
    wide_rows["extras_runs"]   = wide_rows["ball_outcome"].apply(lambda x: 1 if str(x).strip() == "" else x)
    wide_rows["extras_type"]   = wide_rows["ball_details"].str.lower() + "s"
    wide_rows["total_runs"]    = wide_rows["extras_runs"]
    wide_rows["batter_runs"]   = 0
    wide_rows["wickets"]       = 0
    wide_rows["wicket_type"]   = None
    wide_rows["player_out"]    = None
    wide_rows["bowler_wicket"] = 0

    # ── Process OUT rows ──────────────────────────────────────────────────────
    def clean_ball_details(comment):
        if not isinstance(comment, str):
            return ""
        return comment.replace("\n", "").replace("\r", "").replace("\xa0", " ")

    out_rows["ball_outcome"] = "OUT"
    out_rows["ball_details"] = out_rows["Comments"].str.split("OUT!").str[1].apply(clean_ball_details)

    words_to_isolate = ["CAUGHT & BOWLED", "LBW", "CAUGHT", "BOWLED", "STUMPED", "RUN OUT", "HIT WICKET", "OBSTRUCTING THE FIELD"]

    def isolate_word(comment):
        for word in words_to_isolate:
            if isinstance(comment, str) and word in comment:
                return word.lower()
        return None

    out_rows["wicket_type"] = out_rows["ball_details"].apply(isolate_word)

    def join_words(text):
        if text:
            words = text.split()
            return "_".join(words) if len(words) == 2 else text
        return ""

    out_rows["wicket_type"] = out_rows["wicket_type"].apply(join_words)

    def check_player_names(comment):
        if not isinstance(comment, str):
            return None
        for name in player_full_names + player_short_names:
            if name and name in comment:
                return name
        return None

    out_rows["player_out"] = out_rows["ball_details"].apply(check_player_names)

    def extract_runs(comment):
        if not isinstance(comment, str):
            return 0
        match = re.search(r"(\d+) run", comment)
        return int(match.group(1)) if match else 0

    out_rows["total_runs"]  = out_rows["Comments"].apply(extract_runs)
    out_rows["extras_runs"] = 0
    out_rows["extras_type"] = None

    extra_types = ["LEG BYE", "BYE", "NO BALL", "WIDE"]
    for extra_type in extra_types:
        def _extract(row, et=extra_type):
            if isinstance(row["ball_details"], str) and et in row["ball_details"]:
                m = re.search(r"(\d+)", row["ball_details"])
                return int(m.group(1)) if m else 0
            return row["extras_runs"]
        out_rows["extras_runs"] = out_rows.apply(_extract, axis=1)
        out_rows["extras_type"] = out_rows.apply(
            lambda row, et=extra_type: et.lower().replace(" ", "_") if isinstance(row["ball_details"], str) and et in row["ball_details"] else row["extras_type"],
            axis=1
        )

    out_rows["batter_runs"] = out_rows.apply(
        lambda row: row["total_runs"] - row["extras_runs"] if "RUN OUT" in str(row.get("ball_details", "")) else row["total_runs"],
        axis=1
    )
    out_rows["bowler_wicket"] = out_rows["wicket_type"].apply(
        lambda x: 0 if x in ["run_out", "obstructing_the_field"] else 1
    )
    out_rows["wickets"] = 1

    # ── Helper lambdas ────────────────────────────────────────────────────────
    def legal_del(row): return 0 if row.get("extras_type") in ["wides", "noballs"] else 1
    def dots(row):      return 1 if row.get("batter_runs") == 0 and row.get("extras_type") != "wides" else 0
    def ones(row):      return 1 if row.get("batter_runs") == 1 and row.get("extras_type") != "wides" else 0
    def twos(row):      return 1 if row.get("batter_runs") == 2 and row.get("extras_type") != "wides" else 0
    def threes(row):    return 1 if row.get("batter_runs") == 3 and row.get("extras_type") != "wides" else 0
    def fours(row):     return 1 if row.get("batter_runs") == 4 and row.get("extras_type") != "wides" else 0
    def sixes(row):     return 1 if row.get("batter_runs") == 6 and row.get("extras_type") != "wides" else 0
    def wides(row):     return row.get("extras_runs", 0) if row.get("extras_type") == "wides" else 0

    for sub_df in [out_rows, wide_rows, other_rows]:
        sub_df["legal_delivery"] = sub_df.apply(legal_del, axis=1)
        sub_df["0s"]    = sub_df.apply(dots,   axis=1)
        sub_df["1s"]    = sub_df.apply(ones,   axis=1)
        sub_df["2s"]    = sub_df.apply(twos,   axis=1)
        sub_df["3s"]    = sub_df.apply(threes, axis=1)
        sub_df["4s"]    = sub_df.apply(fours,  axis=1)
        sub_df["6s"]    = sub_df.apply(sixes,  axis=1)
        sub_df["wides"] = sub_df.apply(wides,  axis=1)

    # ── Merge all subsets ─────────────────────────────────────────────────────
    merged = pd.concat([out_rows, wide_rows, other_rows], ignore_index=True)

    final_cols = [
        "Match ID", "Event Name", "inning_number", "batting_team", "bowling_team",
        "over", "delivery", "batter", "bowler", "total_runs", "batter_runs",
        "extras_runs", "extras_type", "wickets", "bowler_wicket", "player_out",
        "wicket_type", "legal_delivery", "0s", "1s", "2s", "3s", "4s", "6s", "wides",
    ]
    existing = [c for c in final_cols if c in merged.columns]
    merged   = merged[existing]
    merged   = merged.sort_values(by=["Match ID", "inning_number", "over", "delivery"])
    merged["batter"] = merged["batter"].str.replace(r"\d+", "", regex=True).str.strip().str.replace(",", "")

    num_cols = ["total_runs", "batter_runs", "extras_runs", "legal_delivery", "0s", "1s", "2s", "3s", "4s", "6s", "wides"]
    for col in num_cols:
        if col in merged.columns:
            merged[col] = merged[col].fillna(0).astype(int)

    # ── Replace short names with full names for batter / bowler ──────────────
    for index, row in merged.iterrows():
        match_id     = row["Match ID"]
        batting_team = row["batting_team"]
        bowling_team = row["bowling_team"]

        bat_squad_rows  = new_playing_squad_df[(new_playing_squad_df["Match ID"] == match_id) & (new_playing_squad_df["team"] == batting_team)]
        bowl_squad_rows = new_playing_squad_df[(new_playing_squad_df["Match ID"] == match_id) & (new_playing_squad_df["team"] == bowling_team)]

        if not bat_squad_rows.empty and not bowl_squad_rows.empty:
            bat_short_list  = bat_squad_rows.iloc[0]["short name"]
            bat_full_list   = bat_squad_rows.iloc[0]["full name"]
            bowl_short_list = bowl_squad_rows.iloc[0]["short name"]
            bowl_full_list  = bowl_squad_rows.iloc[0]["full name"]

            batter_short = row["batter"]
            bowler_short = row["bowler"]

            if batter_short in bat_short_list:
                merged.at[index, "batter"] = bat_full_list[bat_short_list.index(batter_short)]
            if bowler_short in bowl_short_list:
                merged.at[index, "bowler"] = bowl_full_list[bowl_short_list.index(bowler_short)]

            player_out_val = row.get("player_out")
            if pd.notna(player_out_val) and player_out_val:
                all_short = bat_short_list + bowl_short_list
                all_full  = bat_full_list  + bowl_full_list
                if player_out_val in all_short:
                    merged.at[index, "player_out"] = all_full[all_short.index(player_out_val)]
                elif player_out_val in all_full:
                    pass  # already a full name
                else:
                    log.warning(f"player_out '{player_out_val}' not found in squad for match {match_id}")

    # ── Assign ball_number ────────────────────────────────────────────────────
    merged = merged.sort_values(by=["Match ID", "inning_number", "over", "delivery"])
    merged["ball_number"] = merged.groupby(["Match ID", "inning_number"]).cumcount() + 1

    return merged


# ══════════════════════════════════════════════════════════════════════════════
# SUPABASE HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def get_last_match_id() -> int:
    """Return the highest match_id currently in Supabase, or 0 if table is empty."""
    result = (
        supabase.table(TABLE_NAME)
        .select("match_id")
        .order("match_id", desc=True)
        .limit(1)
        .execute()
    )
    if result.data:
        last_id = int(result.data[0]["match_id"])
        log.info(f"Last match_id in Supabase: {last_id}")
        return last_id
    log.info("No existing data in Supabase — starting from match ID 1")
    return 0


def upsert_to_supabase(df: pd.DataFrame) -> None:
    if df.empty:
        log.warning("Empty DataFrame — nothing to upsert.")
        return

    rename_map = {
        "Match ID":   "match_id",
        "Event Name": "event_name",
        "0s":         "dots",
        "1s":         "ones",
        "2s":         "twos",
        "3s":         "threes",
        "4s":         "fours",
        "6s":         "sixes",
    }
    df = df.rename(columns=rename_map)

    # Replace inf/-inf with NaN first, then NaN with None
    df = df.replace([float("inf"), float("-inf")], pd.NA)
    records = df.where(pd.notnull(df), other=None).to_dict(orient="records")

    # Sanitise any remaining non-finite floats at the record level
    def sanitise(val):
        if isinstance(val, float) and (val != val or val in (float("inf"), float("-inf"))):
            return None
        return val

    records = [{k: sanitise(v) for k, v in rec.items()} for rec in records]

    BATCH = 500
    for i in range(0, len(records), BATCH):
        batch = records[i : i + BATCH]
        upsert_with_retry(batch)
        log.info(f"  Upserted rows {i}–{min(i + BATCH, len(records))}")


def upsert_with_retry(batch: list, retries: int = 4, base_backoff: float = 5.0) -> None:
    """Upsert a single batch with exponential backoff on failure."""
    for attempt in range(retries):
        try:
            supabase.table(TABLE_NAME).upsert(
                batch,
                on_conflict="match_id,inning_number,ball_number"
            ).execute()
            return
        except Exception as e:
            if attempt < retries - 1:
                wait = base_backoff * (2 ** attempt)
                log.warning(f"  Upsert failed (attempt {attempt + 1}/{retries}): {e}. Retrying in {wait}s …")
                time.sleep(wait)
            else:
                log.error(f"  Upsert failed after {retries} attempts. Giving up on this batch.")
                raise


def flush_to_supabase(
    raw_data: list[pd.DataFrame],
    innings_data: list[pd.DataFrame],
    label: str = "flush",
) -> None:
    """Clean and upsert a collected batch of raw match data."""
    if not raw_data:
        return
    log.info(f"[{label}] Cleaning and upserting {len(raw_data)} match(es) …")
    combined_raw     = pd.concat(raw_data,     ignore_index=True)
    combined_innings = pd.concat(innings_data, ignore_index=True)
    cleaned = clean_ball_df(combined_raw, combined_innings)
    dupes = cleaned.duplicated(
        subset=["Match ID", "inning_number", "over", "delivery", "batter"], keep=False
    )
    if dupes.any():
        log.warning(f"Found {dupes.sum()} duplicate rows — sample:\n{cleaned[dupes].head(10)}")
    log.info(f"[{label}] Cleaned DataFrame: {len(cleaned)} rows")
    upsert_to_supabase(cleaned)


# ══════════════════════════════════════════════════════════════════════════════
# MAIN SCRAPE FUNCTION
# ══════════════════════════════════════════════════════════════════════════════

def run_scraper(
    club_id: int = CLUB,
    batch_size: int = BATCH_SIZE,
    delay: float = SCRAPE_DELAY,
) -> None:
    """
    Incremental scrape → clean → upsert pipeline.

    Queries Supabase for the highest existing match_id, then attempts to
    scrape the next `batch_size` match IDs sequentially.
    """
    log.info("═" * 60)
    log.info("Starting CricClubs scraper (incremental mode)")
    log.info("═" * 60)

    last_id  = get_last_match_id()
    start_id = last_id + 1
    end_id   = last_id + batch_size
    log.info(f"Scraping match IDs {start_id} → {end_id}  (batch_size={batch_size})")

    driver = create_driver()
    all_raw_data:     list[pd.DataFrame] = []
    all_innings_data: list[pd.DataFrame] = []
    success_ids, failed_ids = [], []

    try:
        log.info("Warming up session …")
        driver.get(f"https://www.cricclubs.com/Tanzania/home.do?clubId={club_id}")
        time.sleep(2)

        for match_id in range(start_id, end_id + 1):
            raw     = scrape_raw_ball_by_ball(driver, match_id, club_id)
            time.sleep(delay)
            innings = get_team_innings_mapping(driver, match_id, club_id)
            time.sleep(delay)

            if raw is None or innings is None:
                log.warning(f"  [SKIP] matchId={match_id} — no data found")
                failed_ids.append(match_id)
            else:
                all_raw_data.append(raw)
                all_innings_data.append(innings)
                success_ids.append(match_id)
                log.info(f"  [OK]  matchId={match_id} — {len(raw)} balls")

            # ── Checkpoint flush ──────────────────────────────────────────────
            if len(all_raw_data) >= CHECKPOINT_EVERY:
                flush_to_supabase(
                    all_raw_data,
                    all_innings_data,
                    label=f"checkpoint at matchId={match_id}",
                )
                all_raw_data.clear()
                all_innings_data.clear()

    finally:
        driver.quit()

    log.info(f"\nScraped: {len(success_ids)} succeeded | {len(failed_ids)} skipped/failed")
    if failed_ids:
        log.warning(f"Skipped IDs (no data or mismatch): {failed_ids}")

    # ── Final flush for any remaining data ────────────────────────────────────
    if all_raw_data:
        flush_to_supabase(all_raw_data, all_innings_data, label="final flush")
    else:
        log.info("No remaining data to flush.")

    log.info("Done ✓")


# ══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    run_scraper()