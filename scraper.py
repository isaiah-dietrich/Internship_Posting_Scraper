#!/usr/bin/env python3
"""
Internship postings scraper.
Navigates directly to jobright.ai embed URLs (the underlying data source for intern-list.com),
filters by posting date and hire time, and sends an HTML email digest.
"""

import asyncio
import json
import os
import re
import smtplib
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from playwright.async_api import async_playwright, Page
from rapidfuzz import process, fuzz

CATEGORIES = {
    "Product Management": "https://jobright.ai/minisites-jobs/intern/us/product_management",
    "Cybersecurity":      "https://jobright.ai/minisites-jobs/intern/us/cyber_security",
    "Consulting":         "https://jobright.ai/minisites-jobs/intern/us/consulting",
    "Business Analyst":   "https://jobright.ai/minisites-jobs/intern/us/business_analyst",
}

RECIPIENT_EMAIL    = os.environ.get("RECIPIENT_EMAIL", "ijdietrich@wisc.edu")
GMAIL_USER         = os.environ.get("GMAIL_USER", "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
DRY_RUN            = os.environ.get("DRY_RUN", "false").lower() == "true"
DEBUG              = os.environ.get("DEBUG", "false").lower() == "true"
ENABLE_NETWORKER   = os.environ.get("ENABLE_NETWORKER", "false").lower() == "true"
ENABLE_MC_ALERT    = os.environ.get("ENABLE_MC_ALERT", "false").lower() == "true"

# ---------------------------------------------------------------------------
# Company allowlist (fuzzy matching)
# ---------------------------------------------------------------------------

def _load_allowlist() -> list[str]:
    path = os.path.join(os.path.dirname(__file__), "data", "company_allowlist.txt")
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return [ln.strip() for ln in f if ln.strip()]

COMPANY_ALLOWLIST = _load_allowlist()
_ALLOWLIST_ACTIVE = bool(COMPANY_ALLOWLIST)

if _ALLOWLIST_ACTIVE:
    print(f"Company allowlist loaded: {len(COMPANY_ALLOWLIST)} companies")
else:
    print("Company allowlist not found — company filter disabled")

# ---------------------------------------------------------------------------
# Sent-jobs log (cross-run dedup)
# ---------------------------------------------------------------------------
#
# jobright.ai's "posted X ago" label is the only signal we have for freshness,
# and it isn't reliable enough on its own: a posting sitting right at a "1 day
# ago" boundary (or one whose label lags) can keep passing is_within_last_day()
# for more than one run, which sends the same posting again in a later day's
# email. To guarantee "each posting appears at most once," we keep a small
# persisted log of postings we've already emailed and skip anything in it,
# independent of what the site's timestamp says.

SENT_JOBS_PATH = os.path.join(os.path.dirname(__file__), "data", "sent_jobs.json")
SENT_JOBS_RETENTION_DAYS = 5  # prune entries older than this so the log doesn't grow forever


def job_key(job: dict) -> str:
    """Stable identity for a posting, independent of scrape-to-scrape noise
    (e.g. tracking params in apply links)."""
    return "|".join([
        job["company"].strip().lower(),
        job["title"].strip().lower(),
        job["location"].strip().lower(),
    ])


def load_sent_jobs() -> dict[str, str]:
    if not os.path.exists(SENT_JOBS_PATH):
        return {}
    try:
        with open(SENT_JOBS_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def save_sent_jobs(sent: dict[str, str]) -> None:
    os.makedirs(os.path.dirname(SENT_JOBS_PATH), exist_ok=True)
    with open(SENT_JOBS_PATH, "w", encoding="utf-8") as f:
        json.dump(sent, f, indent=2, sort_keys=True)


def prune_sent_jobs(sent: dict[str, str], today: "datetime.date", retention_days: int = SENT_JOBS_RETENTION_DAYS) -> dict[str, str]:
    cutoff = today - timedelta(days=retention_days)
    pruned = {}
    for key, seen_date_str in sent.items():
        try:
            seen_date = datetime.strptime(seen_date_str, "%Y-%m-%d").date()
        except ValueError:
            continue
        if seen_date >= cutoff:
            pruned[key] = seen_date_str
    return pruned


# ---------------------------------------------------------------------------
# MBA / Grad-program filter
# ---------------------------------------------------------------------------
#
# Word-boundary regex so "Undergraduate"/"Undergrad" are NOT caught (the
# boundary check fails inside those words since "grad"/"graduate" isn't
# preceded by a non-word character), only standalone "MBA" / "Grad" /
# "Graduate" mentions are.

GRAD_MBA_RE = re.compile(r"\bmba\b|\bgrad(?:uate)?s?\b", re.IGNORECASE)


def mentions_mba_or_grad(title: str, qualifications: str = "") -> bool:
    """True if the posting targets MBA / graduate-program candidates."""
    return bool(GRAD_MBA_RE.search(f"{title} {qualifications}"))


# Ordered list of row selectors to try
ROW_SELECTORS = [
    "table tbody tr",
    "[role='row']:not([role='columnheader'])",
    "[role='rowgroup'] [role='row']",
    ".ant-table-row",
    "[class*='jobCard']",
    "[class*='job-card']",
    "[class*='JobCard']",
    "[class*='job-item']",
    "[class*='JobItem']",
    "[class*='job-row']",
    "[class*='listItem']",
    "[class*='list-item']",
    "li[class*='job']",
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


def is_approved_company(company: str, threshold: int = 82) -> bool:
    """Return True if the company fuzzy-matches anything in the allowlist.
    If the allowlist file doesn't exist, every company passes."""
    if not _ALLOWLIST_ACTIVE or not company:
        return not _ALLOWLIST_ACTIVE
    result = process.extractOne(company, COMPANY_ALLOWLIST, scorer=fuzz.token_sort_ratio)
    return result is not None and result[1] >= threshold


def is_not_remote(work_model: str) -> bool:
    """Returns False for purely remote jobs."""
    return "remote" not in work_model.strip().lower()


def meets_salary_threshold(salary: str) -> bool:
    """
    Thresholds: $30/hr | $5,000/mo | $1,250/wk | $60,000/yr
    N/A / blank → True (include).  Unpaid → False.
    """
    s = salary.strip().lower()
    if not s or s in ("n/a", "na", "-", "—", "tbd", "not specified"):
        return True
    if "unpaid" in s:
        return False

    nums = [float(n.replace(",", "")) for n in re.findall(r"[\d,]+(?:\.\d+)?", s)]
    if not nums:
        return True

    max_val = max(nums)

    is_hourly  = bool(re.search(r"/hr|/hour|\bhour\b|\bhourly\b", s))
    is_monthly = bool(re.search(r"/mo(?:nth)?|\bmonth\b|\bmonthly\b", s))
    is_weekly  = bool(re.search(r"/wk|/week|\bweek\b|\bweekly\b", s))
    is_annual  = bool(re.search(r"/yr|/year|\byear\b|\bannual\b", s))

    if is_hourly:
        return max_val >= 30
    elif is_monthly:
        return max_val >= 5_000
    elif is_weekly:
        return max_val >= 1_250
    elif is_annual:
        return max_val >= 60_000
    else:
        # Guess from magnitude
        if max_val < 500:
            return max_val >= 30       # looks hourly
        elif max_val < 8_000:
            return max_val >= 5_000    # looks monthly
        else:
            return max_val >= 60_000   # looks annual


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
# Playwright helpers
# ---------------------------------------------------------------------------

STEALTH_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'plugins',   { get: () => [1, 2, 3] });
Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
window.chrome = { runtime: {} };
"""


async def find_row_selector(page: Page) -> str | None:
    for sel in ROW_SELECTORS:
        try:
            count = await page.locator(sel).count()
            if count > 0:
                print(f"  Row selector: {sel!r} ({count} elements)")
                return sel
        except Exception:
            continue
    return None


async def get_column_map(page: Page) -> dict[str, int]:
    """Return lowercase column-name → index from any header variant."""
    for sel in [
        "table thead tr th",
        "table thead tr td",
        "[role='columnheader']",
        "[aria-rowindex='1'] [role='cell']",
    ]:
        headers = await page.query_selector_all(sel)
        if headers:
            return {(await h.inner_text()).strip().lower(): i for i, h in enumerate(headers)}
    return {}


async def get_row_cells(row) -> list:
    cells = await row.query_selector_all("td")
    if cells:
        return cells
    cells = await row.query_selector_all("[role='cell'], [role='gridcell']")
    if cells:
        return cells
    cells = await row.query_selector_all(":scope > div, :scope > a")
    if cells:
        return cells
    return await row.query_selector_all(":scope > *")


# ---------------------------------------------------------------------------
# Scraper
# ---------------------------------------------------------------------------

async def scrape_category(page: Page, url: str, name: str) -> list[dict]:
    print(f"\nScraping {name} ...")

    try:
        await page.goto(url, wait_until="networkidle", timeout=60000)
    except Exception:
        await page.goto(url, timeout=60000)

    await page.wait_for_timeout(3000)

    row_sel = await find_row_selector(page)

    if not row_sel:
        print(f"  No rows found — trying extended wait ...")
        await page.wait_for_timeout(5000)
        row_sel = await find_row_selector(page)

    if not row_sel:
        print(f"  Still no rows found for {name}")
        if DEBUG:
            body = await page.inner_html("body")
            print(f"\n--- PAGE BODY HTML (first 4000 chars) ---\n{body[:4000]}\n---\n")
            await page.screenshot(path=f"debug_{name.lower().replace(' ', '_')}.png", full_page=False)
        return []

    jobs: list[dict] = []
    seen_count = 0
    stop = False

    for scroll_iter in range(300):
        col = await get_column_map(page)
        rows = await page.query_selector_all(row_sel)

        if DEBUG and scroll_iter == 0 and rows:
            first_html = await rows[0].inner_html()
            print(f"\n--- FIRST ROW HTML ---\n{first_html[:2000]}\n---\n")
            print(f"Column map: {dict(list(col.items())[:15])}")

        for row in rows[seen_count:]:
            cells = await get_row_cells(row)
            if not cells:
                continue

            async def cell(key: str, fallback: int = -1) -> str:
                idx = col.get(key, fallback)
                if idx < 0 or idx >= len(cells):
                    return ""
                return (await cells[idx].inner_text()).strip()

            date_str = await cell("date", 2)
            if not is_within_last_day(date_str):
                stop = True
                break

            work_model = await cell("work model", 4)
            if not is_not_remote(work_model):
                continue

            salary  = await cell("salary", 7)
            company = await cell("company", 6)
            if not (meets_salary_threshold(salary) or is_approved_company(company)):
                continue

            hire_time = await cell("hire time", 8)
            if not is_valid_hire_time(hire_time):
                continue

            title          = await cell("position title", 1)
            qualifications = await cell("qualifications", 12)
            if mentions_mba_or_grad(title, qualifications):
                continue

            location         = await cell("location", 5)
            graduate_time    = await cell("graduate time", 9)
            company_industry = await cell("company industry", 10)
            company_size     = await cell("company size", 11)

            apply_link = ""
            apply_idx = col.get("apply", 3)
            if 0 <= apply_idx < len(cells):
                a_tag = await cells[apply_idx].query_selector("a")
                if a_tag:
                    apply_link = (await a_tag.get_attribute("href")) or ""

            if title:
                jobs.append({
                    "title":            title,
                    "company":          company,
                    "date":             date_str,
                    "location":         location,
                    "work_model":       work_model,
                    "salary":           salary,
                    "hire_time":        hire_time,
                    "graduate_time":    graduate_time,
                    "company_industry": company_industry,
                    "company_size":     company_size,
                    "qualifications":   qualifications,
                    "apply_link":       apply_link,
                })

        seen_count = len(await page.query_selector_all(row_sel))

        if stop:
            break

        prev_height = await page.evaluate("document.body.scrollHeight")
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(1500)
        new_height = await page.evaluate("document.body.scrollHeight")
        if new_height == prev_height:
            break

    print(f"  → {len(jobs)} matching posting(s)")
    return jobs


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------

def salary_sort_key(job: dict) -> float:
    """Extract the highest number from a salary string for descending sort. N/A → -1."""
    nums = re.findall(r'[\d]+', job["salary"].replace(",", ""))
    return max((float(n) for n in nums), default=-1)


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
    max-width: 620px;
    margin: 0 auto;
    padding: 16px;
    color: #1a1a2e;
    background: #fff;
  }}
  h1  {{ color: #0f3460; border-bottom: 2px solid #0f3460; padding-bottom: 6px; margin-bottom: 12px; font-size: 15px; }}
  h2  {{ color: #16213e; margin: 20px 0 4px; font-size: 12px; }}
  .summary {{
    background: #eef2ff;
    border-left: 3px solid #0f3460;
    padding: 8px 12px;
    margin-bottom: 16px;
    border-radius: 3px;
    font-size: 10px;
  }}
  .table-wrap {{ margin-bottom: 14px; }}
  table {{
    border-collapse: collapse;
    font-size: 5px;
    table-layout: fixed;
    width: 588px;
  }}
  th {{
    background: #0f3460; color: #fff;
    padding: 2px 3px; text-align: left; font-weight: 600;
    white-space: nowrap; overflow: hidden;
  }}
  td {{
    padding: 1px 3px; border-bottom: 1px solid #e4e8f0;
    vertical-align: middle;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }}
  tr:nth-child(even) td {{ background: #f5f7ff; }}
  .col-num      {{ width: 16px;  }}
  .col-title    {{ width: 150px; }}
  .col-company  {{ width: 90px;  }}
  .col-location {{ width: 90px;  }}
  .col-model    {{ width: 55px;  }}
  .col-salary   {{ width: 75px;  }}
  .col-hire     {{ width: 57px;  }}
  .col-apply    {{ width: 50px;  }}
  .badge {{ display: inline-block; padding: 1px 3px; border-radius: 4px; font-size: 4px; font-weight: 600; }}
  .remote  {{ background: #d4f5d4; color: #1a6e1a; }}
  .onsite  {{ background: #ffd4d4; color: #8a1a1a; }}
  .hybrid  {{ background: #d4e8ff; color: #1a4a8a; }}
  a.apply-btn {{
    display: inline-block;
    background: #0f3460;
    color: #fff !important;
    text-decoration: none;
    padding: 1px 4px;
    border-radius: 2px;
    font-size: 4px;
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
  <span style="color:#555">Criteria: posted within last 24h &bull; Hire Time = Summer 2027 or unspecified &bull; On-site / Hybrid only &bull; Salary &ge; $30/hr (or approved company) &bull; Approved companies only &bull; No MBA/Grad postings &bull; No repeats from prior emails</span>
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

        sorted_jobs = sorted(jobs, key=salary_sort_key, reverse=True)

        html += '<div class="table-wrap">\n'
        html += (
            "<table>\n<thead><tr>"
            '<th class="col-num">#</th>'
            '<th class="col-title">Position Title</th>'
            '<th class="col-company">Company</th>'
            '<th class="col-location">Location</th>'
            '<th class="col-model">Work Model</th>'
            '<th class="col-salary">Salary</th>'
            '<th class="col-hire">Hire Time</th>'
            '<th class="col-apply">Apply</th>'
            "</tr></thead>\n<tbody>\n"
        )

        for i, j in enumerate(sorted_jobs, 1):
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
                f"<tr>"
                f'<td class="col-num">{i}</td>'
                f'<td class="col-title"><strong>{j["title"]}</strong></td>'
                f'<td class="col-company">{j["company"]}</td>'
                f'<td class="col-location">{j["location"]}</td>'
                f'<td class="col-model">{badge}</td>'
                f'<td class="col-salary">{j["salary"] or "&mdash;"}</td>'
                f'<td class="col-hire">{j["hire_time"] or "&mdash;"}</td>'
                f'<td class="col-apply">{apply_cell}</td>'
                f"</tr>\n"
            )

        html += "</tbody>\n</table>\n</div>\n"

    html += (
        f'<div class="footer">'
        f'Scraped from <a href="https://intern-list.com">intern-list.com</a> via jobright.ai &bull; {date_str}'
        f"</div>\n</body></html>"
    )
    return html


def send_email(html: str, date_str: str) -> bool:
    """Returns True if a real email was sent, False if it only wrote a local preview."""
    if DRY_RUN or not GMAIL_USER or not GMAIL_APP_PASSWORD:
        out = "email_preview.html"
        with open(out, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"\nDry run — email HTML saved to {out}")
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"Internship Postings: {date_str}"
    msg["From"]    = GMAIL_USER
    msg["To"]      = RECIPIENT_EMAIL
    msg.attach(MIMEText(html, "html", "utf-8"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        smtp.sendmail(GMAIL_USER, RECIPIENT_EMAIL, msg.as_string())

    print(f"Email sent to {RECIPIENT_EMAIL}")
    return True


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main() -> None:
    now = datetime.now()
    date_str = now.strftime("%B %d, %Y")
    today_iso = now.strftime("%Y-%m-%d")

    sent_jobs = prune_sent_jobs(load_sent_jobs(), now.date())

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

        for name, url in CATEGORIES.items():
            scraped = await scrape_category(page, url, name)
            deduped = [j for j in scraped if job_key(j) not in sent_jobs]
            skipped = len(scraped) - len(deduped)
            if skipped:
                print(f"  ({skipped} already emailed in a previous run — skipped)")
            jobs_by_cat[name] = deduped

        if ENABLE_NETWORKER and sum(len(v) for v in jobs_by_cat.values()) > 0:
            from networker import run_networker_for_jobs
            await run_networker_for_jobs(jobs_by_cat, page)

        await browser.close()

    if ENABLE_MC_ALERT:
        from mc_alert import find_mc_matches, load_mc_firms, send_mc_alert
        mc_firms = load_mc_firms()
        print(f"\nMC alert: checking {len(mc_firms)} firms ...")
        mc_matches = find_mc_matches(jobs_by_cat, mc_firms)
        if mc_matches:
            send_mc_alert(mc_matches, date_str, GMAIL_USER, GMAIL_APP_PASSWORD, RECIPIENT_EMAIL, DRY_RUN)
        else:
            print("MC alert: no matches found")

    total = sum(len(v) for v in jobs_by_cat.values())
    print(f"\nTotal matching postings: {total}")

    html = build_html(jobs_by_cat, date_str)
    email_sent = send_email(html, date_str)

    if email_sent:
        for jobs in jobs_by_cat.values():
            for j in jobs:
                sent_jobs[job_key(j)] = today_iso
        save_sent_jobs(sent_jobs)


if __name__ == "__main__":
    asyncio.run(main())
