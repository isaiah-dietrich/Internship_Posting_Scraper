#!/usr/bin/env python3
"""
Builds data/company_allowlist.txt from three sources:
  1. Fortune 500 — Wikipedia
  2. CB Insights Unicorn List — cbinsights.com
  3. Top Consulting Firms — managementconsulted.com

Run manually or via the monthly GitHub Actions workflow.
"""

import asyncio
import os
import re

import requests
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

OUTPUT_PATH = os.path.join(os.path.dirname(__file__), "data", "company_allowlist.txt")

# Fallback list used if Management Consulted scraping fails.
# Covers MBB, Big 4, and other well-known strategy/consulting firms.
CONSULTING_FALLBACK = [
    "McKinsey & Company",
    "Boston Consulting Group",
    "BCG",
    "Bain & Company",
    "Deloitte",
    "PwC",
    "PricewaterhouseCoopers",
    "EY",
    "Ernst & Young",
    "KPMG",
    "Accenture",
    "Oliver Wyman",
    "Kearney",
    "A.T. Kearney",
    "Strategy&",
    "L.E.K. Consulting",
    "Booz Allen Hamilton",
    "Huron Consulting Group",
    "FTI Consulting",
    "AlixPartners",
    "Alvarez & Marsal",
    "Gartner",
    "IBM Consulting",
    "Capgemini",
    "ZS Associates",
    "Analysis Group",
    "Charles River Associates",
    "NERA Economic Consulting",
    "Cornerstone Research",
    "Guidehouse",
    "Protiviti",
    "West Monroe",
    "Slalom",
    "Roland Berger",
    "Arthur D. Little",
    "Simon-Kucher & Partners",
    "Mercer",
    "Willis Towers Watson",
    "WTW",
    "Aon",
    "Marsh",
    "ICF International",
    "MITRE Corporation",
    "Infosys Consulting",
    "Cognizant",
    "Wipro",
    "Tata Consultancy Services",
    "TCS",
    "HCL Technologies",
    "Publicis Sapient",
    "Navigant",
    "Stout",
    "Duff & Phelps",
    "Kroll",
    "Grant Thornton",
    "BDO",
    "RSM",
    "Ankura",
    "Berkeley Research Group",
    "CEB",
    "Gallup",
    "Kantar",
    "Ipsos",
    "IHS Markit",
    "S&P Global",
    "Wood Mackenzie",
    "Tetra Tech",
]


# ---------------------------------------------------------------------------
# Source 1: Fortune 500 (Wikipedia — static HTML)
# ---------------------------------------------------------------------------

def get_fortune_500() -> list[str]:
    print("Fetching Fortune 500 from Wikipedia ...")
    url = "https://en.wikipedia.org/wiki/Fortune_500"
    try:
        resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
        resp.raise_for_status()
    except Exception as e:
        print(f"  ERROR: {e}")
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    companies = []

    for table in soup.find_all("table", class_="wikitable"):
        for row in table.find_all("tr")[1:]:
            cells = row.find_all(["td", "th"])
            # Column layout: Rank | Name | Industry | Revenue | ...
            if len(cells) >= 2:
                name = cells[1].get_text(strip=True)
                # Strip footnote markers like [1], [A]
                name = re.sub(r"\[.*?\]", "", name).strip()
                if name:
                    companies.append(name)

    print(f"  {len(companies)} companies")
    return companies


# ---------------------------------------------------------------------------
# Source 2: CB Insights Unicorn List (JS-rendered)
# ---------------------------------------------------------------------------

async def get_unicorn_list(page) -> list[str]:
    print("Fetching CB Insights Unicorn list ...")
    url = "https://www.cbinsights.com/research-unicorn-companies"
    try:
        await page.goto(url, wait_until="networkidle", timeout=60000)
        await page.wait_for_timeout(4000)
    except Exception as e:
        print(f"  ERROR loading page: {e}")
        return []

    companies = []

    # Try table rows first
    rows = await page.query_selector_all("table tbody tr")
    if rows:
        for row in rows:
            cells = await row.query_selector_all("td")
            if cells:
                name = (await cells[0].inner_text()).strip()
                name = re.sub(r"\[.*?\]", "", name).strip()
                if name:
                    companies.append(name)
    else:
        # Fallback: any element that looks like a company name cell
        cells = await page.query_selector_all("[class*='company'], [class*='Company'], [class*='name']")
        for c in cells:
            name = (await c.inner_text()).strip()
            if name and len(name) < 80:
                companies.append(name)

    print(f"  {len(companies)} companies")
    return companies


# ---------------------------------------------------------------------------
# Source 3: Management Consulted top consulting firms (local file)
# ---------------------------------------------------------------------------

def get_consulting_firms() -> list[str]:
    """
    Parse firm names from the manually scraped managementconsulted.txt file.
    Each firm name appears on the line immediately after its rank line (#1, #2, ...).
    """
    path = os.path.join(os.path.dirname(__file__), "managementconsulted.txt")
    if not os.path.exists(path):
        print("  managementconsulted.txt not found — using fallback list")
        return CONSULTING_FALLBACK

    firms = []
    lines = open(path, encoding="utf-8").readlines()
    for i, line in enumerate(lines):
        if re.match(r"^#\d+\s*$", line.strip()) and i + 1 < len(lines):
            name = lines[i + 1].strip()
            if name:
                firms.append(name)

    print(f"  {len(firms)} firms from managementconsulted.txt")
    return firms if firms else CONSULTING_FALLBACK


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def normalize(name: str) -> str:
    return re.sub(r"\s+", " ", name).strip()


async def main() -> None:
    all_companies: set[str] = set()

    # Fortune 500 (synchronous — static HTML)
    for name in get_fortune_500():
        all_companies.add(normalize(name))

    # Consulting firms — local file (no browser needed)
    for name in get_consulting_firms():
        all_companies.add(normalize(name))

    # Always ensure fallback consulting firms are present
    for name in CONSULTING_FALLBACK:
        all_companies.add(normalize(name))

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        )
        page = await ctx.new_page()

        for name in await get_unicorn_list(page):
            all_companies.add(normalize(name))

        await browser.close()

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    sorted_companies = sorted(all_companies, key=str.lower)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(sorted_companies) + "\n")

    print(f"\nTotal: {len(sorted_companies)} companies → {OUTPUT_PATH}")


if __name__ == "__main__":
    asyncio.run(main())
