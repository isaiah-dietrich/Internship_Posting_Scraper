#!/usr/bin/env python3
"""
Internship postings scraper for intern-list.com.
The site loads job data into an Airtable iframe when a category button is clicked.
This scraper clicks each category, waits for the iframe, then scrapes within it.
"""

import asyncio
import os
import re
import smtplib
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from playwright.async_api import async_playwright, Page, Frame

# Category name -> (page URL, short-link attribute value on the trigger button)
CATEGORIES = {
    "Product Management": ("https://www.intern-list.com/?k=pm", "pm"),
    "Cybersecurity":      ("https://www.intern-list.com/?k=cs", "cs"),
    "Consulting":         ("https://www.intern-list.com/?k=cst", "cst"),
    "Business Analyst":   ("https://www.intern-list.com/?k=ba", "ba"),
}

RECIPIENT_EMAIL    = os.environ.get("RECIPIENT_EMAIL", "ijdietrich@wisc.edu")
GMAIL_USER         = os.environ.get("GMAIL_USER", "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
DRY_RUN            = os.environ.get("DRY_RUN", "false").lower() == "true"
DEBUG              = os.environ.get("DEBUG", "false").lower() == "true"

# Airtable row selectors to try (most specific first)
AIRTABLE_ROW_SELECTORS = [
    "table tbody tr",
    ".ant-table-row",
    "[data-rowindex]",
    "[role='row']:not([aria-rowindex='1'])",
    "tr",
]


# ---------------------------------------------------------------------------
# Filtering helpers
# ---------------------------------------------------------------------------

def is_within_last_day(date_str: str) -> bool:
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
    ht = hire_time.strip().lower()
    blank = ht in ("", "n/a", "na", "-", "—", "not specified", "tbd")
    match = (
        "summer 2027" in ht
        or "2027-summer" in ht
        or ht == "2027"
    )
    return blank or match


# ---------------------------------------------------------------------------
# Stealth / anti-bot
# ---------------------------------------------------------------------------

STEALTH_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3] });
Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
window.chrome = { runtime: {} };
"""


# ---------------------------------------------------------------------------
# Iframe handling
# ---------------------------------------------------------------------------

async def get_airtable_frame(page: Page, short_link: str):
    """
    Return the content Frame of the Airtable iframe on the page.
    If the URL parameter didn't trigger auto-load, clicks the category button first.
    """
    # Give the page time to auto-load the iframe based on the URL ?k= param
    await page.wait_for_timeout(4000)

    iframe_el = await page.query_selector("iframe[src*='airtable.com']")

    if not iframe_el:
        # Try clicking the matching category trigger button
        trigger = page.locator(f"[short-link='{short_link}']").first
        if await trigger.count():
            await trigger.click()
            print(f"  Clicked category trigger for short-link={short_link!r}")
            await page.wait_for_timeout(5000)
            iframe_el = await page.query_selector("iframe[src*='airtable.com']")

    if not iframe_el:
        # Last resort: any iframe on the page
        all_iframes = await page.query_selector_all("iframe")
        print(f"  No Airtable iframe found. Total iframes on page: {len(all_iframes)}")
        for el in all_iframes:
            src = await el.get_attribute("src") or "(no src)"
            print(f"    iframe src: {src[:120]}")
        if DEBUG:
            await page.screenshot(path=f"debug_{short_link}_page.png", full_page=False)
            body = await page.inner_html("body")
            print(f"\n--- PAGE BODY HTML (first 4000 chars) ---\n{body[:4000]}\n---\n")
        return None

    src = await iframe_el.get_attribute("src") or ""
    print(f"  Airtable iframe: {src[:100]}")

    frame = await iframe_el.content_frame()
    return frame


# ---------------------------------------------------------------------------
# Airtable frame scraping
# ---------------------------------------------------------------------------

async def find_airtable_row_selector(frame: Frame) -> str | None:
    """Wait for Airtable to load and return the first working row selector."""
    try:
        await frame.wait_for_load_state("domcontentloaded", timeout=20000)
    except Exception:
        pass
    await frame.wait_for_timeout(3000)

    for sel in AIRTABLE_ROW_SELECTORS:
        try:
            count = await frame.locator(sel).count()
            if count > 0:
                print(f"  Airtable row selector: {sel!r} ({count} rows)")
                return sel
        except Exception:
            continue
    return None


async def get_airtable_column_map(frame: Frame) -> dict[str, int]:
    """Build column-name → index map from the Airtable header row."""
    header_selectors = [
        "table thead tr th",
        "[role='columnheader']",
        "[aria-rowindex='1'] [role='cell']",
        "thead td",
    ]
    for sel in header_selectors:
        headers = await frame.query_selector_all(sel)
        if headers:
            return {(await h.inner_text()).strip().lower(): i for i, h in enumerate(headers)}
    return {}


async def scrape_airtable_frame(frame: Frame, name: str) -> list[dict]:
    """Scrape job rows from within the Airtable iframe."""

    if DEBUG:
        body = await frame.inner_html("body")
        print(f"\n--- AIRTABLE IFRAME HTML (first 4000 chars) ---\n{body[:4000]}\n---\n")
        await frame.screenshot(path=f"debug_{name.lower().replace(' ', '_')}_iframe.png")

    row_sel = await find_airtable_row_selector(frame)
    if not row_sel:
        print(f"  No rows found in Airtable iframe for {name}")
        return []

    # Try to enable Hire Time column via any "Edit Columns" / column controls
    try:
        edit_btn = frame.get_by_role("button", name=re.compile(r"edit columns|fields|columns", re.IGNORECASE))
        if await edit_btn.count():
            await edit_btn.first.click()
            await frame.wait_for_timeout(600)
            hire_opt = frame.get_by_text("Hire Time", exact=True).first
            if await hire_opt.count():
                await hire_opt.click()
                await frame.wait_for_timeout(400)
            await frame.keyboard.press("Escape")
            await frame.wait_for_timeout(400)
    except Exception:
        pass

    jobs: list[dict] = []
    seen_count = 0
    stop = False

    for scroll_iter in range(300):
        col = await get_airtable_column_map(frame)
        rows = await frame.query_selector_all(row_sel)

        if DEBUG and scroll_iter == 0 and rows:
            first_html = await rows[0].inner_html()
            print(f"\n--- FIRST AIRTABLE ROW HTML ---\n{first_html[:2000]}\n---\n")
            col_map_display = dict(list(col.items())[:15])
            print(f"Column map: {col_map_display}")

        for row in rows[seen_count:]:
            cells = await row.query_selector_all("td")
            if not cells:
                cells = await row.query_selector_all("[role='cell'], [role='gridcell']")
            if not cells:
                cells = await row.query_selector_all(":scope > *")
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

            title      = await cell("position title", 0)
            company    = await cell("company", 5)
            location   = await cell("location", 4)
            work_model = await cell("work model", 3)
            salary     = await cell("salary", 6)

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

        seen_count = len(await frame.query_selector_all(row_sel))

        if stop:
            break

        # Scroll within the Airtable iframe to load more rows
        prev_height = await frame.evaluate("document.body.scrollHeight")
        await frame.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await frame.wait_for_timeout(1500)
        new_height = await frame.evaluate("document.body.scrollHeight")
        if new_height == prev_height:
            break

    print(f"  → {len(jobs)} matching posting(s)")
    return jobs


# ---------------------------------------------------------------------------
# Category scraper
# ---------------------------------------------------------------------------

async def scrape_category(page: Page, url: str, name: str, short_link: str) -> list[dict]:
    print(f"\nScraping {name} ...")

    try:
        await page.goto(url, wait_until="networkidle", timeout=60000)
    except Exception:
        await page.goto(url, timeout=60000)

    frame = await get_airtable_frame(page, short_link)
    if frame is None:
        return []

    return await scrape_airtable_frame(frame, name)


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
  th {{ background: #0f3460; color: #fff; padding: 10px 14px; text-align: left; font-weight: 600; }}
  td {{ padding: 9px 14px; border-bottom: 1px solid #e4e8f0; vertical-align: middle; }}
  tr:nth-child(even) td {{ background: #f5f7ff; }}
  .badge {{ display: inline-block; padding: 3px 9px; border-radius: 12px; font-size: 11px; font-weight: 600; }}
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
            "<table>\n<thead><tr>"
            "<th>#</th><th>Position Title</th><th>Company</th>"
            "<th>Location</th><th>Work Model</th><th>Apply</th>"
            "</tr></thead>\n<tbody>\n"
        )

        for i, j in enumerate(jobs, 1):
            wm = j["work_model"].lower()
            cls = (
                "remote" if "remote" in wm else
                "onsite" if "on" in wm and "site" in wm else
                "hybrid" if "hybrid" in wm else ""
            )
            badge = f'<span class="badge {cls}">{j["work_model"]}</span>' if cls else j["work_model"]
            apply_cell = (
                f'<a class="apply-btn" href="{j["apply_link"]}" target="_blank">Apply &rarr;</a>'
                if j["apply_link"] else "&mdash;"
            )
            html += (
                f"<tr><td>{i}</td><td><strong>{j['title']}</strong></td>"
                f"<td>{j['company']}</td><td>{j['location']}</td>"
                f"<td>{badge}</td><td>{apply_cell}</td></tr>\n"
            )

        html += "</tbody>\n</table>\n"

    html += (
        f'<div class="footer">'
        f'Scraped from <a href="https://intern-list.com">intern-list.com</a> &bull; {date_str}'
        f"</div>\n</body></html>"
    )
    return html


def send_email(html: str, date_str: str) -> None:
    if DRY_RUN or not GMAIL_USER or not GMAIL_APP_PASSWORD:
        out = "email_preview.html"
        with open(out, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"\nDry run — email HTML saved to {out}")
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
        await ctx.add_init_script(STEALTH_SCRIPT)
        page = await ctx.new_page()

        for name, (url, short_link) in CATEGORIES.items():
            jobs_by_cat[name] = await scrape_category(page, url, name, short_link)

        await browser.close()

    total = sum(len(v) for v in jobs_by_cat.values())
    print(f"\nTotal matching postings: {total}")

    html = build_html(jobs_by_cat, date_str)
    send_email(html, date_str)


if __name__ == "__main__":
    asyncio.run(main())
