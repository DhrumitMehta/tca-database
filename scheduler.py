"""
scheduler.py
------------
Runs the CricClubs scraper on a schedule using APScheduler.
Use this if you're running on a always-on server (e.g. a VPS or Raspberry Pi).

Alternatively, for serverless / cloud-based scheduling, see the
GitHub Actions workflow in .github/workflows/scrape.yml.

Usage:
    python scheduler.py

Schedule options (set via env vars or edit below):
    SCRAPE_SCHEDULE = "daily"   → runs every day at 03:00 local time
    SCRAPE_SCHEDULE = "weekly"  → runs every Monday at 03:00 local time
"""

import os
import logging
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from scraper import run_scraper

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

SCHEDULE = os.environ.get("SCRAPE_SCHEDULE", "daily").lower()


def job():
    log.info("Scheduler triggered — starting scrape …")
    run_scraper()
    log.info("Scrape job complete.")


if __name__ == "__main__":
    scheduler = BlockingScheduler()

    if SCHEDULE == "weekly":
        trigger = CronTrigger(day_of_week="mon", hour=3, minute=0)
        log.info("Scheduled: every Monday at 03:00")
    else:  # default: daily
        trigger = CronTrigger(hour=3, minute=0)
        log.info("Scheduled: every day at 03:00")

    scheduler.add_job(job, trigger)

    log.info("Scheduler running. Press Ctrl+C to exit.")
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("Scheduler stopped.")
