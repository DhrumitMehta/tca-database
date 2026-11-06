"""
export_dev_academy_matches.py
------------------------------
Pulls all rows from tca_db_match_info_v2 where the series_name column contains
"development league", "academy league", or "TCA U17 LEAGUE" (case-insensitive)
AND the match_date falls in 2026, then exports them to an Excel file.

Setup:
    pip install supabase python-dotenv openpyxl pandas

Environment variables (.env or shell):
    SUPABASE_URL
    SUPABASE_KEY

Usage:
    python export_dev_academy_matches.py
"""

import os
import pandas as pd
from dotenv import load_dotenv
from supabase import create_client, Client

load_dotenv()

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

TABLE = "tca_db_match_info_v2"
OUTPUT_FILE = "tca_census_matches_2026.xlsx"
PAGE_SIZE = 1000


def fetch_all_rows() -> list[dict]:
    """Paginate through the table since Supabase caps results per request."""
    all_rows = []
    start = 0
    while True:
        res = (
            supabase.table(TABLE)
            .select("*")
            .range(start, start + PAGE_SIZE - 1)
            .execute()
        )
        batch = res.data
        if not batch:
            break
        all_rows.extend(batch)
        if len(batch) < PAGE_SIZE:
            break
        start += PAGE_SIZE
    return all_rows


def main():
    print("Fetching all rows from Supabase...")
    rows = fetch_all_rows()
    print(f"Fetched {len(rows)} total rows.")

    df = pd.DataFrame(rows)

    SERIES_KEYWORDS = ["development league", "academy league", "tca u17 league"]
    pattern = "|".join(SERIES_KEYWORDS)

    series_mask = df["series_name"].str.contains(pattern, case=False, na=False, regex=True)

    # Filter: match_date is in 2026
    df["match_date"] = pd.to_datetime(df["match_date"], errors="coerce")
    year_mask = df["match_date"].dt.year == 2026

    filtered = df[series_mask & year_mask].copy()
    filtered = filtered.sort_values("match_date")

    print(f"Filtered down to {len(filtered)} rows matching criteria.")

    filtered.to_excel(OUTPUT_FILE, index=False)
    print(f"Saved to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()