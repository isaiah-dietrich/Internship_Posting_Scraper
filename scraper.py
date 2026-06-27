#!/usr/bin/env python3
"""
Internship postings scraper for intern-list.com.
Scrapes 4 categories, filters by posting date and hire time, sends an HTML email digest.
"""

import asyncio
import os
import re
import smtplib
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from playwright.async_api import async_playwright, Page

CATEGORIES = {
    "Product Management": "https://www.intern-list.com/?k=pm",
    "Cybersecurity":      "https://www.intern-list.com/?k=cs",
    "Consulting":         "https://www.intern-list.com/?k=cst",
    "Business Analyst":   "https://www.intern-list.com/?k=ba",
}

RECIPIENT_EMAIL    = os.environ.get("RECIPIENT_EMAIL", "ijdietrich@wisc.edu")
GMAIL_USER         = os.environ.get("GMAIL_USER", "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
DRY_RUN            = os.environ.get("DRY_RUN", "false").lower() == "true"


# ---------------------------------------------------------------------------
# Filtering helpers
# ---------------------------------------------------------------------------

def is_within_last_day(date_str: str) -> bool:
    """Return True if the date string represents a posting from the last 24 hours."""
    s = date_str.lower().strip()
    if not s or s in ("just now", "moments ago", "a moment ago"):
        return True
    if re.match(r"\d+\s*minutes?\s*ago", s):
        return True
    m = re.match(r"(\d+)\s*hours?\s*ago", s)
    if m:
        return int(m.group(1)) <= 24
    if re.match(r"(1|a)\s*days?\s*ago", s):
        return True
    m = re.match(r"(\d+)\s*days?\s*ago", s)
    if m:
        return int(m.group(1)) < 1
    return False


def is_valid_hire_time(hire_time: str) -> bool:
    """Return True for Summer 2027 or blank/unspecified hire times."""
    ht = hire_time.strip().lower()
    blank = ht in ("", "n/a", "na", "-", "—", "not specified", "tbd")
    return blank or "summer 2027" in ht


# ---------------------------------------------------------------------------
# Playwright helpers
# ---------------------------------------------------------------------------

async def try_enable_hire_time_column(page: Page) -> None:
    """Best-effort attempt to enable the Hire Time column via Edit Columns UI."""
    try:
        btn = page.get_by_role("button", name=re.compile(r"edit columns", re.IGNORECASE))
        if not await btn.count():
            return
        await btn.click()
        await page.wait_for_timeout(700)

        hire_opt = page.get_by_text("Hire Time", exact=True).first
        if await hire_opt.count():
            await hire_opt.click()
            await page.wait_for_timeout(400)

        await page.keyboard.press("Escape")
        await page.wait_for_timeout(400)
    except Exception:
        pass


async def get_column_map(page: Page) -> dict[str, int]:
    """Return a mapping of lowercase column name -> cell index."""
    headers = await page.query_selector_all("table thead tr th, table thead tr td")
    return {(await h.inner_text()).strip().lower(): i for i, h in enumerate(headers)}


async def scrape_category(page: Page, url: str, name: str) -> list[dict]:
    """Scrape all postings from the last 24 hours for one category URL."""
    print(f"Scraping {name} ...")

    try:
        await page.goto(url, wait_until="networkidle", timeout=60000)
    except Exception:
        await page.goto(url, timeout=60000)

    await page.wait_for_timeout(2000)
    await try_enable_hire_time_column(page)

    try:
        await page.wait_for_selector("table tbody tr", timeout=15000)
    except Exception:
        print(f"  No table found for {name}")
        return []

    jobs: list[dict] = []
    seen_count = 0
    stop = False

    for _ in range(200):  # max scroll iterations
        col = await get_column_map(page)
        rows = await page.query_selector_all("table tbody tr")

        for row in rows[seen_count:]:
            cells = await row.query_selector_all("td")
            if not cells:
                continue

            async def cell(key: str, fallback: int = -1) -> str:
                idx = col.get(key, fallback)
                if idx < 0 or idx >= len(cells):
                    return ""
                return (await cells[idx].inner_text()).strip()

            date_str = await cell("date", 1)
            if not is_within_last_day(date_str):
                stop = True
                break

            hire_time = await cell("hire time")
            if not is_valid_hire_time(hire_time):
                continue

            title     = await cell("position title", 0)
            company   = await cell("company", 5)
            location  = await cell("location", 4)
            work_model = await cell("work model", 3)
            salary    = await cell("salary", 6)

            apply_link = ""
            apply_idx = col.get("apply", 2)
            if 0 <= apply_idx < len(cells):
                a_tag = await cells[apply_idx].query_selector("a")
                if a_tag:
                    apply_link = (await a_tag.get_attribute("href")) or ""

            if title:
                jobs.append({
                    "title":      title,
                    "company":    company,
                    "date":       date_str,
                    "location":   location,
                    "work_model": work_model,
                    "salary":     salary,
                    "hire_time":  hire_time,
                    "apply_link": apply_link,
                })

        seen_count = len(await page.query_selector_all("table tbody tr"))

        if stop:
            break

        prev_height = await page.evaluate("document.body.scrollHeight")
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(1500)
        new_height = await page.evaluate("document.body.scrollHeight")
        if new_height == prev_height:
            break  # No new content loaded — reached the end

    print(f"  → {len(jobs)} matching posting(s)")
    return jobs


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------

def build_html(jobs_by_cat: dict[str, list[dict]], date_str: str) -> str:
    total = sum(len(v) for v in jobs_by_cat.values())

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  body {{
    font-family: Arial, Helvetica, sans-serif;
    max-width: 960px;
    margin: 0 auto;
    padding: 28px 24px;
    color: #1a1a2e;
    background: #fff;
  }}
  h1  {{ color: #0f3460; border-bottom: 3px solid #0f3460; padding-bottom: 10px; margin-bottom: 20px; font-size: 22px; }}
  h2  {{ color: #16213e; margin: 32px 0 8px; font-size: 17px; }}
  .summary {{
    background: #eef2ff;
    border-left: 4px solid #0f3460;
    padding: 14px 18px;
    margin-bottom: 28px;
    border-radius: 4px;
    font-size: 14px;
  }}
  table {{ width: 100%; border-collapse: collapse; margin-bottom: 20px; font-size: 13px; }}
  th {{
    background: #0f3460;
    color: #fff;
    padding: 10px 14px;
    text-align: left;
    font-weight: 600;
  }}
  td {{ padding: 9px 14px; border-bottom: 1px solid #e4e8f0; vertical-align: middle; }}
  tr:nth-child(even) td {{ background: #f5f7ff; }}
  .badge {{
    display: inline-block;
    padding: 3px 9px;
    border-radius: 12px;
    font-size: 11px;
    font-weight: 600;
  }}
  .remote  {{ background: #d4f5d4; color: #1a6e1a; }}
  .onsite  {{ background: #ffd4d4; color: #8a1a1a; }}
  .hybrid  {{ background: #d4e8ff; color: #1a4a8a; }}
  a.apply-btn {{
    display: inline-block;
    background: #0f3460;
    color: #fff !important;
    text-decoration: none;
    padding: 5px 14px;
    border-radius: 4px;
    font-size: 12px;
    font-weight: 600;
    white-space: nowrap;
  }}
  .no-jobs {{ color: #888; font-style: italic; font-size: 13px; margin: 4px 0 20px; }}
  .footer  {{ color: #aaa; font-size: 11px; margin-top: 36px; border-top: 1px solid #e8e8e8; padding-top: 12px; }}
  .footer a {{ color: #aaa; }}
</style>
</head>
<body>
<h1>Internship Postings &mdash; {date_str}</h1>
<div class="summary">
  <strong>{total} new posting{"s" if total != 1 else ""}</strong> matched your filters across {len(jobs_by_cat)} categories.<br>
  <span style="color:#555">Criteria: posted within the last 24 hours &bull; Hire Time = Summer 2027 or unspecified</span>
</div>
"""

    for cat, jobs in jobs_by_cat.items():
        count_label = f'{len(jobs)} posting{"s" if len(jobs) != 1 else ""}'
        html += (
            f'<h2>{cat} '
            f'<span style="font-weight:normal;color:#888;font-size:14px">({count_label})</span>'
            f'</h2>\n'
        )

        if not jobs:
            html += '<p class="no-jobs">No postings matched the criteria for this category.</p>\n'
            continue

        html += (
            "<table>\n"
            "<thead><tr>"
            "<th>#</th>"
            "<th>Position Title</th>"
            "<th>Company</th>"
            "<th>Location</th>"
            "<th>Work Model</th>"
            "<th>Apply</th>"
            "</tr></thead>\n<tbody>\n"
        )

        for i, j in enumerate(jobs, 1):
            wm = j["work_model"].lower()
            if "remote" in wm:
                cls = "remote"
            elif "on" in wm and "site" in wm:
                cls = "onsite"
            elif "hybrid" in wm:
                cls = "hybrid"
            else:
                cls = ""

            badge = f'<span class="badge {cls}">{j["work_model"]}</span>' if cls else j["work_model"]
            apply_cell = (
                f'<a class="apply-btn" href="{j["apply_link"]}" target="_blank">Apply &rarr;</a>'
                if j["apply_link"] else "&mdash;"
            )

            html += (
                f"<tr>"
                f"<td>{i}</td>"
                f"<td><strong>{j['title']}</strong></td>"
                f"<td>{j['company']}</td>"
                f"<td>{j['location']}</td>"
                f"<td>{badge}</td>"
                f"<td>{apply_cell}</td>"
                f"</tr>\n"
            )

        html += "</tbody>\n</table>\n"

    html += (
        f'<div class="footer">'
        f'Scraped from <a href="https://intern-list.com">intern-list.com</a> &bull; {date_str}'
        f"</div>\n"
        f"</body></html>"
    )
    return html


def send_email(html: str, date_str: str) -> None:
    if DRY_RUN or not GMAIL_USER or not GMAIL_APP_PASSWORD:
        out = "email_preview.html"
        with open(out, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"Dry run — email HTML saved to {out}")
        return

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"Internship Postings: {date_str}"
    msg["From"]    = GMAIL_USER
    msg["To"]      = RECIPIENT_EMAIL
    msg.attach(MIMEText(html, "html", "utf-8"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        smtp.sendmail(GMAIL_USER, RECIPIENT_EMAIL, msg.as_string())

    print(f"Email sent to {RECIPIENT_EMAIL}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main() -> None:
    date_str = datetime.now().strftime("%B %d, %Y")
    jobs_by_cat: dict[str, list[dict]] = {}

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1440, "height": 900},
        )
        page = await ctx.new_page()

        for name, url in CATEGORIES.items():
            jobs_by_cat[name] = await scrape_category(page, url, name)

        await browser.close()

    total = sum(len(v) for v in jobs_by_cat.values())
    print(f"\nTotal matching postings: {total}")

    html = build_html(jobs_by_cat, date_str)
    send_email(html, date_str)


if __name__ == "__main__":
    asyncio.run(main())
