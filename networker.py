#!/usr/bin/env python3
"""
Networking pipeline: for each matched job, finds a UW alum at the company,
discovers their email via Apollo, drafts a message via Claude, and saves it
as a Gmail draft for human review before sending.
"""

import asyncio
import imaplib
import json
import os
import re
import time
import email as email_lib
import email.mime.multipart
import email.mime.text

import requests

LINKEDIN_EMAIL     = os.environ.get("LINKEDIN_EMAIL", "")
LINKEDIN_PASSWORD  = os.environ.get("LINKEDIN_PASSWORD", "")
LINKEDIN_COOKIES   = os.environ.get("LINKEDIN_COOKIES", "")   # JSON cookie array for CI
APOLLO_API_KEY     = os.environ.get("APOLLO_API_KEY", "")
ANTHROPIC_API_KEY  = os.environ.get("ANTHROPIC_API_KEY", "")
GMAIL_USER         = os.environ.get("GMAIL_USER", "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
MAX_OUTREACH       = int(os.environ.get("MAX_OUTREACH_PER_RUN", "3"))

_BASE          = os.path.dirname(__file__)
MESSAGES_FILE  = os.path.join(_BASE, "data", "networking_messages.txt")
CONTACTED_LOG  = os.path.join(_BASE, "data", "contacted_log.json")

# UW's LinkedIn school entity ID (used in alumni search URL)
UW_SCHOOL_ID = "166652"

# Keywords used to score alumni title relevance per job category
DEPT_KEYWORDS: dict[str, list[str]] = {
    "Product Management": [
        "product manager", "product management", "pm", "program manager",
        "product owner", "product lead",
    ],
    "Consulting": [
        "consultant", "consulting", "strategy", "advisory",
        "associate", "management consultant",
    ],
    "Cybersecurity": [
        "security", "cyber", "information security", "risk",
        "compliance", "soc analyst", "penetration",
    ],
    "Business Analyst": [
        "analyst", "business analyst", "data analyst",
        "operations analyst", "financial analyst", "bi analyst",
    ],
}


# ─── LinkedIn ─────────────────────────────────────────────────────────────────

async def linkedin_login(page) -> bool:
    """Log in to LinkedIn.  Tries stored cookies first, then username/password."""
    if LINKEDIN_COOKIES:
        try:
            cookies = json.loads(LINKEDIN_COOKIES)
            await page.context.add_cookies(cookies)
            await page.goto(
                "https://www.linkedin.com/feed/",
                wait_until="domcontentloaded",
                timeout=20_000,
            )
            await page.wait_for_timeout(2000)
            if "feed" in page.url or "mynetwork" in page.url:
                print("  LinkedIn: session restored from cookies")
                return True
        except Exception as e:
            print(f"  LinkedIn cookie restore failed: {e}")

    if not (LINKEDIN_EMAIL and LINKEDIN_PASSWORD):
        print("  LinkedIn: no credentials provided")
        return False

    try:
        await page.goto(
            "https://www.linkedin.com/login",
            wait_until="domcontentloaded",
            timeout=20_000,
        )
        await page.wait_for_timeout(1500)
        await page.fill("#username", LINKEDIN_EMAIL)
        await page.fill("#password", LINKEDIN_PASSWORD)
        await page.click('[type="submit"]')
        await page.wait_for_timeout(4000)

        url = page.url
        if "feed" in url or "mynetwork" in url:
            print("  LinkedIn: logged in with credentials")
            return True
        if "checkpoint" in url or "challenge" in url or "verification" in url:
            print("  LinkedIn: 2FA / CAPTCHA challenge — cannot proceed headlessly")
            print("  Tip: run locally once, export cookies, store as LINKEDIN_COOKIES secret")
            return False
        if "login" in url:
            print("  LinkedIn: login rejected — check LINKEDIN_EMAIL / LINKEDIN_PASSWORD")
            return False
        return True
    except Exception as e:
        print(f"  LinkedIn login error: {e}")
        return False


async def find_uw_alumni(page, company: str, category: str) -> list[dict]:
    """Return alumni cards from the UW LinkedIn alumni page filtered by company."""
    try:
        await page.goto(
            f"https://www.linkedin.com/school/university-of-washington/people/",
            wait_until="networkidle",
            timeout=30_000,
        )
    except Exception:
        await page.goto(
            "https://www.linkedin.com/school/university-of-washington/people/",
            timeout=30_000,
        )
    await page.wait_for_timeout(2500)

    # Find the "Where they work" filter input
    company_input = None
    for sel in [
        'input[placeholder*="company" i]',
        'input[aria-label*="company" i]',
        'input[placeholder*="work" i]',
        '[id*="company"] input',
        'input[id*="company" i]',
    ]:
        el = page.locator(sel).first
        if await el.count() > 0:
            company_input = el
            break

    if not company_input:
        print(f"  LinkedIn: could not find company filter — alumni search unavailable")
        return []

    await company_input.click()
    await company_input.fill(company)
    await page.wait_for_timeout(1800)

    # Accept the first autocomplete suggestion or press Enter
    dropdown_option = page.locator('[role="listbox"] [role="option"]').first
    if await dropdown_option.count() > 0:
        await dropdown_option.click()
    else:
        await company_input.press("Enter")

    await page.wait_for_timeout(3000)

    # Extract result cards
    alumni: list[dict] = []
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

        for card in cards[:10]:
            name_el  = await card.query_selector(
                ".entity-result__title-text a span[aria-hidden='true'], "
                ".actor-name, [class*='name'] a, .app-aware-link span:first-child"
            )
            title_el = await card.query_selector(
                ".entity-result__primary-subtitle, .subline-level-1, [class*='subtitle'], [class*='headline']"
            )
            link_el  = await card.query_selector("a[href*='/in/']")

            name  = (await name_el.inner_text()).strip()  if name_el  else ""
            title = (await title_el.inner_text()).strip() if title_el else ""
            url   = (await link_el.get_attribute("href")) if link_el  else ""

            if name:
                alumni.append({
                    "name":  name,
                    "title": title,
                    "profile_url": url.split("?")[0] if url else "",
                })
        break

    return alumni


def rank_alumni(alumni: list[dict], category: str) -> dict | None:
    """Score alumni by job-title keyword overlap; return the best match."""
    keywords = DEPT_KEYWORDS.get(category, [])

    def score(person: dict) -> int:
        t = person["title"].lower()
        return sum(1 for kw in keywords if kw in t)

    ranked = sorted(alumni, key=score, reverse=True)
    return ranked[0] if ranked else None


# ─── Apollo email finder ──────────────────────────────────────────────────────

def find_email_apollo(first_name: str, last_name: str, company: str) -> str | None:
    if not APOLLO_API_KEY:
        return None
    try:
        resp = requests.post(
            "https://api.apollo.io/v1/people/match",
            headers={"Content-Type": "application/json", "Cache-Control": "no-cache"},
            json={
                "first_name": first_name,
                "last_name":  last_name,
                "organization_name": company,
                "api_key": APOLLO_API_KEY,
            },
            timeout=15,
        )
        if resp.status_code == 200:
            person = resp.json().get("person") or {}
            email  = person.get("email") or ""
            if "@" in email:
                return email
        else:
            print(f"  Apollo: HTTP {resp.status_code}")
    except Exception as e:
        print(f"  Apollo error: {e}")
    return None


# ─── Message dataset ──────────────────────────────────────────────────────────

def load_message_examples() -> str:
    if not os.path.exists(MESSAGES_FILE):
        return "(no examples provided)"
    with open(MESSAGES_FILE, encoding="utf-8") as f:
        return f.read().strip() or "(no examples provided)"


# ─── Claude draft generation ──────────────────────────────────────────────────

def draft_message_with_claude(
    alumni: dict, job: dict, category: str, examples: str
) -> tuple[str, str]:
    """Return (subject, body) using Claude to generate the message."""
    from anthropic import Anthropic

    name_parts = alumni["name"].split()
    first_name = name_parts[0] if name_parts else alumni["name"]

    client = Anthropic(api_key=ANTHROPIC_API_KEY)
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=600,
        messages=[{
            "role": "user",
            "content": f"""You are writing a personalized networking email on behalf of Isaiah Dietrich, a UW student seeking a {category} internship.

Study these examples of Isaiah's past networking messages carefully to match his exact voice, tone, and structure:
---
{examples}
---

Now write a new email to:
- Recipient first name: {first_name}
- Their title: {alumni['title']}
- Company: {job['company']}
- Role Isaiah is pursuing: {job['title']} ({category})

Rules:
- Match Isaiah's voice from the examples — do not deviate from his style
- Under 175 words total
- One clear ask: a referral or a 15-minute call
- Do not say "I found you on LinkedIn" or reference how you found them
- Mention the specific role and company naturally
- Warm, professional, not salesy

Reply in exactly this format (nothing else):
SUBJECT: [subject line]

[email body]""",
        }],
    )

    text = response.content[0].text.strip()
    # Split on first blank line after SUBJECT: line
    m = re.match(r"SUBJECT:\s*(.+?)\n\n(.*)", text, re.DOTALL)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    # Fallback: treat first line as subject
    lines = text.split("\n", 1)
    subject = lines[0].replace("SUBJECT:", "").strip()
    body    = lines[1].strip() if len(lines) > 1 else text
    return subject, body


# ─── Gmail draft ──────────────────────────────────────────────────────────────

def create_gmail_draft(to_name: str, to_email: str, subject: str, body: str) -> None:
    msg = email_lib.mime.multipart.MIMEMultipart()
    msg["From"]    = GMAIL_USER
    msg["To"]      = f"{to_name} <{to_email}>" if to_email else to_name
    msg["Subject"] = subject
    msg.attach(email_lib.mime.text.MIMEText(body, "plain", "utf-8"))

    with imaplib.IMAP4_SSL("imap.gmail.com") as mail:
        mail.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        mail.append(
            '"[Gmail]/Drafts"',
            "\\Draft",
            imaplib.Time2Internaldate(time.time()),
            msg.as_bytes(),
        )
    dest = to_email if to_email else "(no email found — fill in manually)"
    print(f"  Gmail draft saved: '{subject}' → {dest}")


# ─── Contact log ──────────────────────────────────────────────────────────────

def load_contacted_log() -> list[dict]:
    if not os.path.exists(CONTACTED_LOG):
        return []
    try:
        with open(CONTACTED_LOG, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def save_contacted_log(log: list[dict]) -> None:
    os.makedirs(os.path.dirname(CONTACTED_LOG), exist_ok=True)
    with open(CONTACTED_LOG, "w", encoding="utf-8") as f:
        json.dump(log, f, indent=2)


def already_contacted(company: str, log: list[dict]) -> bool:
    return any(
        entry.get("company", "").lower() == company.lower()
        for entry in log
    )


# ─── Salary sort key (mirrors scraper.py) ────────────────────────────────────

def _salary_sort_key(job: dict) -> float:
    nums = re.findall(r"[\d]+", job.get("salary", "").replace(",", ""))
    return max((float(n) for n in nums), default=-1)


# ─── Main orchestrator ────────────────────────────────────────────────────────

async def run_networker_for_jobs(jobs_by_cat: dict, page) -> None:
    """
    For up to MAX_OUTREACH matched jobs, find a UW alum, get their email,
    draft a message via Claude, and save it as a Gmail draft.
    """
    if not ANTHROPIC_API_KEY:
        print("\nNetworker: ANTHROPIC_API_KEY not set — skipping")
        return
    if not (LINKEDIN_EMAIL or LINKEDIN_COOKIES):
        print("\nNetworker: LinkedIn credentials not set — skipping alumni search")
        return

    examples  = load_message_examples()
    log       = load_contacted_log()

    # Gather unique companies from all categories, best salary first
    candidates: list[tuple[str, dict]] = []
    seen: set[str] = set()
    for cat, jobs in jobs_by_cat.items():
        for job in sorted(jobs, key=_salary_sort_key, reverse=True):
            company = job.get("company", "").strip()
            if company and company not in seen:
                seen.add(company)
                if not already_contacted(company, log):
                    candidates.append((cat, job))

    if not candidates:
        print("\nNetworker: no new companies to reach out to")
        return

    print(f"\nNetworker: found {len(candidates)} candidate companies — processing up to {MAX_OUTREACH}")

    # Login to LinkedIn once
    if not await linkedin_login(page):
        print("Networker: LinkedIn login failed — skipping networking pipeline")
        return

    processed = 0
    for category, job in candidates:
        if processed >= MAX_OUTREACH:
            break

        company = job["company"]
        print(f"\n── {company} ({category}: {job['title']}) ──")

        # 1. Find UW alumni
        alumni_list = await find_uw_alumni(page, company, category)
        print(f"  Alumni found: {len(alumni_list)}")

        if not alumni_list:
            print("  Skipping — no alumni results")
            continue

        alum = rank_alumni(alumni_list, category)
        if not alum:
            continue
        print(f"  Top match: {alum['name']} — {alum['title']}")

        # 2. Find email via Apollo
        name_parts = alum["name"].split()
        first, last = name_parts[0], (name_parts[-1] if len(name_parts) > 1 else "")
        email_addr = find_email_apollo(first, last, company)
        if email_addr:
            print(f"  Email: {email_addr}")
        else:
            print("  Email: not found via Apollo — draft TO will be blank")

        # 3. Draft message
        try:
            subject, body = draft_message_with_claude(alum, job, category, examples)
        except Exception as e:
            print(f"  Claude draft failed: {e}")
            continue

        # 4. Save Gmail draft
        if GMAIL_USER and GMAIL_APP_PASSWORD:
            try:
                create_gmail_draft(alum["name"], email_addr or "", subject, body)
            except Exception as e:
                print(f"  Gmail draft error: {e}")
        else:
            print(f"  GMAIL_USER/GMAIL_APP_PASSWORD not set — printing draft:")
            print(f"  TO: {alum['name']} {email_addr or '(no email)'}")
            print(f"  SUBJECT: {subject}")
            print(body)

        # 5. Log contact
        log.append({
            "name":        alum["name"],
            "title":       alum["title"],
            "company":     company,
            "job_title":   job["title"],
            "email":       email_addr or "",
            "date":        time.strftime("%Y-%m-%d"),
            "profile_url": alum.get("profile_url", ""),
        })
        processed += 1

    save_contacted_log(log)
    print(f"\nNetworker: done — {processed} draft(s) created")
