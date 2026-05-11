# cricclubs-scraper

A set of scraper scripts for extracting CricClubs match, player, series, and ball-by-ball data and loading it into Supabase.

The code is designed for incremental scraping: each scraper checks the current Supabase table state and only attempts the next block of IDs that have not yet been saved.

## Project overview

This repository includes the following components:

- `ball_by_ball_scraper.py`: scrapes ball-by-ball delivery data and inserts it into `tca_db_ball_by_ball`.
- `match_info_scraper.py`: scrapes match metadata and match-level info into `tca_db_match_info`.
- `scorecard_scraper.py`: scrapes batting and bowling scorecards into `tca_db_scorecard_batting` and `tca_db_scorecard_bowling`.
- `player_info_scraper.py`: scrapes player profile and career data into `tca_db_player_info`.
- `series_info_scraper.py`: scrapes league/series metadata into `tca_db_series_info`.
- `retry_missing_scorecards.py`: finds missing match IDs in existing scorecard data and retries scraping only those matches.
- `supabase_schema.sql`: SQL DDL for creating the Supabase tables used by this project.
- `requirements.txt`: Python dependencies required to run the scrapers.
- `stats.html`: a static statistics or reporting artifact generated from the scraped data.

## How it works

Each scraper:

1. loads environment variables from `.env` via `python-dotenv`.
2. creates a headless Selenium Chrome browser using `webdriver-manager`.
3. navigates CricClubs pages for the configured club or league.
4. parses HTML with `BeautifulSoup`.
5. transforms scraped page content into normalized records.
6. upserts the data into Supabase via the `supabase` Python client.

The incremental logic is:

- query Supabase for the highest existing ID in the target table,
- start scraping from the next ID,
- write progress to Supabase in checkpoint batches,
- keep retrying until the configured batch size is processed.

`retry_missing_scorecards.py` is a helper to:

- read all existing `match_id` values from `tca_db_scorecard_batting`,
- compute missing IDs in a given range,
- call `scorecard_scraper` only for the missing IDs,
- flush recovered rows back to Supabase.

## Setup

1. Create and activate a Python environment.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

2. Install dependencies.

```powershell
pip install -r requirements.txt
```

3. Create a `.env` file in the repository root with:

```dotenv
SUPABASE_URL=https://xxxx.supabase.co
SUPABASE_KEY=your-service-role-or-anon-key
```

> `.env` is intentionally excluded from version control via `.gitignore`.

4. Create or verify the required Supabase tables. Use `supabase_schema.sql` as the source of truth.

## Quick start

1. Prepare `.env` and install dependencies.
2. Run the core scrapers in this order:

```powershell
python series_info_scraper.py
python match_info_scraper.py
python scorecard_scraper.py
python ball_by_ball_scraper.py
python player_info_scraper.py
```

3. If needed, repair missing scorecards:

```powershell
python retry_missing_scorecards.py --dry-run
python retry_missing_scorecards.py
```

4. Verify data in Supabase and inspect `stats.html` for reporting.

## Usage

### Run a scraper incrementally

```powershell
python match_info_scraper.py
python scorecard_scraper.py
python ball_by_ball_scraper.py
python player_info_scraper.py
python series_info_scraper.py
```

Most scrapers use built-in incremental resume logic and will continue from the last scraped IDs.

### Run a manual ID range

Some scrapers support manual range mode:

```powershell
python match_info_scraper.py --start 100 --end 200 --batch-size 50 --delay 1.5
python scorecard_scraper.py --start 100 --end 120 --batch-size 20 --delay 2.0
python series_info_scraper.py --start 1 --end 50 --batch-size 20 --delay 1.5
```

### Retry missing scorecards

To identify and rescrape missing scorecard matches:

```powershell
python retry_missing_scorecards.py --dry-run
python retry_missing_scorecards.py --only 6147
python retry_missing_scorecards.py --max-id 5000
```

## Environment

Required environment variables:

- `SUPABASE_URL`
- `SUPABASE_KEY`

## Notes

- All scrapers use Selenium and headless Chrome to handle dynamic CricClubs page content.
- The code is tuned for the Tanzanian club/league context, but the core pattern can be adapted to other CricClubs sections.
- Check `requirements.txt` for the exact package versions used.

## File summary

- `ball_by_ball_scraper.py`: ball-by-ball event and delivery parser.
- `match_info_scraper.py`: match-level metadata, toss, umpires, points, and timing.
- `scorecard_scraper.py`: batting and bowling scorecard rows.
- `player_info_scraper.py`: player profile details.
- `series_info_scraper.py`: league/series metadata.
- `retry_missing_scorecards.py`: repairs missing scorecard rows by scraping absent `match_id`s.
- `supabase_schema.sql`: table definitions and schema references.
- `requirements.txt`: required Python packages.
- `stats.html`: static reporting artifact created from scraped data.
