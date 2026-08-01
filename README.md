# Internship Postings Scraper

An automated pipeline that scrapes internship listings, filters them down to the ones worth seeing, and delivers a clean HTML digest by email every morning — no manual job-board checking required.

## What it does

- **Scrapes** internship postings across several configurable job categories, navigating dynamically-rendered listing pages with headless Chromium.
- **Filters** results down with a small rules pipeline: posting recency, target hire period, and a keyword filter that excludes MBA/graduate-level postings.
- **Deduplicates** aggressively — both across days (so a posting you already saw doesn't show up again just because a site's "posted X ago" timestamp is imprecise) and across categories (so the same listing doesn't appear twice when a job fits more than one category).
- **Delivers** a formatted HTML email summarizing new matches, grouped by category and sorted by compensation.
- **Runs unattended** on a daily schedule via GitHub Actions, with self-healing around daylight saving time changes and scheduling jitter, and persists its own dedup state back to the repo between runs.

## Optional add-on modules

- A **management-consulting firm alert** that separately flags postings from a curated list of consulting firms.
- A **networking assistant** (disabled by default) that can identify a relevant alumni contact at a matched company and draft a personalized outreach message for manual review — nothing sends automatically.

## Tech stack

- **Python 3** / `asyncio`
- **Playwright** for headless browser automation and scraping
- **GitHub Actions** for scheduled, serverless execution (cron + CI)
- **SMTP (Gmail)** for email delivery
- **RapidFuzz** for fuzzy text matching
- **Anthropic API** for AI-drafted outreach messages (optional module)

## Project structure

```
scraper.py      # core scrape → filter → dedup → email pipeline
mc_alert.py     # optional consulting-firm match alert
networker.py    # optional alumni-outreach drafting assistant
data/           # persisted state (dedup log, message templates)
.github/        # scheduled workflow definition
```

## Why I built this

I was manually re-checking job boards every day and repeatedly seeing the same stale or irrelevant postings. This started as a personal automation script and turned into a small end-to-end project touching browser automation, data filtering, scheduled cloud execution, and API integration.
