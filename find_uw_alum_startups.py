#!/usr/bin/env python3
"""
Finds UW alumni who are founders/leaders at startups, for manual outreach.

Unlike networker.py's find_uw_alumni() — which starts from a known company
and filters the UW alumni directory down to people who work there — this
starts from a title keyword ("Founder", "CEO", ...) and searches across all
UW alumni, so it surfaces companies rather than requiring one already in
hand. Reuses networker.py's LinkedIn login and Apollo email lookup rather
than duplicating them.

Produces data/uw_alum_startups.json for manual review — not wired into the
daily cron. LinkedIn scraping is rate/ban-sensitive, so run this on demand
(`python find_uw_alum_startups.py`) rather than on a fixed schedule.
"""

import asyncio
import json
import os
import re

from playwright.async_api import async_playwright

from networker import APOLLO_API_KEY, find_email_apollo, linkedin_login

_BASE       = os.path.dirname(__file__)
OUTPUT_PATH = os.path.join(_BASE, "data", "uw_alum_startups.json")

# One search per term — LinkedIn's alumni title-search box takes a single
# free-text query at a time, so broader coverage means more terms, not a
# fancier query.
FOUNDER_TITLE_SEARCHES = [
    "Founder",
    "Co-Founder",
    "Founding Engineer",
    "CEO",
]

MAX_RESULTS_PER_SEARCH = 25


async def search_alumni_by_title(page, title_query: str) -> list[dict]:
    """Search the UW alumni directory by free-text title keyword (the box
    at the top of the page, distinct from the faceted 'Where they work'
    filter networker.py's find_uw_alumni() uses)."""
    try:
        await page.goto(
            "https://www.linkedin.com/school/university-of-washington/people/",
            wait_until="networkidle",
            timeout=30_000,
        )
    except Exception:
        await page.goto(
            "https://www.linkedin.com/school/university-of-washington/people/",
            timeout=30_000,
        )
    await page.wait_for_timeout(2500)

    search_input = None
    for sel in [
        'input[placeholder*="title" i]',
        'input[placeholder*="keyword" i]',
        'input[aria-label*="Search alumni" i]',
        ".org-alumni-search-bar input",
        'input[type="text"]',
    ]:
        el = page.locator(sel).first
        if await el.count() > 0:
            search_input = el
            break

    if not search_input:
        print("  LinkedIn: could not find the alumni title-search box")
        return []

    await search_input.click()
    await search_input.fill(title_query)
    await page.wait_for_timeout(1800)
    await search_input.press("Enter")
    await page.wait_for_timeout(3000)

    people: list[dict] = []
    card_selectors = [
        "li.org-alumni-directory-results__hit-card",
        ".entity-result__item",
        "[data-view-name*='search-entity-result']",
        ".reusable-search__result-container li",
        ".search-results__list li",
    ]

    for sel in card_selectors:
        cards = await page.query_selector_all(sel)
        if not cards:
            continue

        for card in cards[:MAX_RESULTS_PER_SEARCH]:
            name_el = await card.query_selector(
                ".entity-result__title-text a span[aria-hidden='true'], "
                ".actor-name, [class*='name'] a, .app-aware-link span:first-child"
            )
            title_el = await card.query_selector(
                ".entity-result__primary-subtitle, .subline-level-1, [class*='subtitle'], [class*='headline']"
            )
            link_el = await card.query_selector("a[href*='/in/']")

            name  = (await name_el.inner_text()).strip()  if name_el  else ""
            title = (await title_el.inner_text()).strip() if title_el else ""
            url   = (await link_el.get_attribute("href")) if link_el  else ""

            if name:
                people.append({
                    "name":  name,
                    "title": title,
                    "profile_url": url.split("?")[0] if url else "",
                })
        break

    return people


def parse_company(title: str) -> str:
    """Best-effort split of a headline like 'Founder at Acme Inc.' into just
    the company part."""
    m = re.search(r"\bat\s+(.+)$", title, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    m = re.search(r"@\s*(.+)$", title)
    return m.group(1).strip() if m else ""


async def main() -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(viewport={"width": 1440, "height": 900})
        page = await ctx.new_page()

        if not await linkedin_login(page):
            print("LinkedIn login failed — cannot search alumni")
            await browser.close()
            return

        all_people: dict[str, dict] = {}
        for query in FOUNDER_TITLE_SEARCHES:
            print(f"\nSearching UW alumni for title: {query!r}")
            results = await search_alumni_by_title(page, query)
            print(f"  {len(results)} result(s)")
            for person in results:
                key = person["profile_url"] or person["name"]
                if key in all_people:
                    continue
                all_people[key] = {
                    **person,
                    "company":       parse_company(person["title"]),
                    "matched_search": query,
                }

        await browser.close()

    people = list(all_people.values())
    print(f"\n{len(people)} unique UW alum founder(s)/leader(s) found")

    if APOLLO_API_KEY:
        print("Looking up emails via Apollo ...")
        for person in people:
            name_parts = person["name"].split()
            first = name_parts[0]
            last  = name_parts[-1] if len(name_parts) > 1 else ""
            person["email"] = find_email_apollo(first, last, person["company"]) or ""

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(people, f, indent=2)

    print(f"\nWrote {len(people)} record(s) -> {OUTPUT_PATH}")
    print("\nSummary:")
    for p in sorted(people, key=lambda p: p["company"].lower()):
        email_note = f" | {p['email']}" if p.get("email") else ""
        print(f"  {p['name']:30s} | {p['title']:50s} | {p['company']}{email_note}")


if __name__ == "__main__":
    asyncio.run(main())
