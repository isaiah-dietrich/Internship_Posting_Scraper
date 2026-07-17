"""
Management-Consulted firm internship alert.

Parses firm names from managementconsulted.txt, checks all already-scraped
jobs for matches, and sends a separate alert email when any are found.

Activated via ENABLE_MC_ALERT=true.
"""

import os
import re
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from rapidfuzz import fuzz, process


def load_mc_firms() -> list[str]:
    """Extract firm names from managementconsulted.txt.

    Firms appear on the line immediately following each '#N' ranking line.
    """
    path = os.path.join(os.path.dirname(__file__), "managementconsulted.txt")
    firms: list[str] = []
    with open(path, encoding="utf-8") as f:
        lines = f.read().splitlines()

    i = 0
    while i < len(lines):
        if re.match(r"^#\d+$", lines[i].strip()):
            j = i + 1
            while j < len(lines) and not lines[j].strip():
                j += 1
            if j < len(lines):
                name = lines[j].strip()
                if name and not name.startswith("#"):
                    firms.append(name)
        i += 1

    return firms


def _matched_firm(company: str, mc_firms: list[str], threshold: int = 80) -> str | None:
    if not company:
        return None
    result = process.extractOne(company, mc_firms, scorer=fuzz.token_sort_ratio)
    if result and result[1] >= threshold:
        return result[0]
    return None


def find_mc_matches(jobs_by_cat: dict[str, list[dict]], mc_firms: list[str]) -> list[dict]:
    """Return jobs whose company fuzzy-matches an MC firm (with category + matched_firm added)."""
    seen: set[tuple] = set()
    matches: list[dict] = []
    for cat, jobs in jobs_by_cat.items():
        for job in jobs:
            matched = _matched_firm(job["company"], mc_firms)
            if not matched:
                continue
            key = (job["company"], job["title"])
            if key in seen:
                continue
            seen.add(key)
            matches.append({**job, "category": cat, "matched_firm": matched})
    return matches


def build_mc_alert_html(matches: list[dict], date_str: str) -> str:
    by_firm: dict[str, list[dict]] = {}
    for m in matches:
        by_firm.setdefault(m["matched_firm"], []).append(m)

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
    color: #1a1a1a;
    background: #fff;
  }}
  h1 {{ color: #8b0000; border-bottom: 2px solid #8b0000; padding-bottom: 6px; margin-bottom: 12px; font-size: 15px; }}
  h2 {{ color: #1a1a1a; margin: 20px 0 4px; font-size: 12px; }}
  .banner {{
    background: #fff3f3;
    border-left: 4px solid #8b0000;
    padding: 8px 12px;
    margin-bottom: 16px;
    border-radius: 3px;
    font-size: 10px;
  }}
  .table-wrap {{ margin-bottom: 14px; }}
  table {{
    border-collapse: collapse;
    font-size: 6px;
    table-layout: fixed;
    width: 588px;
  }}
  th {{
    background: #8b0000; color: #fff;
    padding: 2px 3px; text-align: left; font-weight: 600;
    white-space: nowrap; overflow: hidden;
  }}
  td {{
    padding: 1px 3px; border-bottom: 1px solid #f0d4d4;
    vertical-align: middle;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }}
  tr:nth-child(even) td {{ background: #fff8f8; }}
  .col-num      {{ width: 16px;  }}
  .col-title    {{ width: 165px; }}
  .col-cat      {{ width: 80px;  }}
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
    background: #8b0000;
    color: #fff !important;
    text-decoration: none;
    padding: 1px 4px;
    border-radius: 2px;
    font-size: 4px;
    font-weight: 600;
    white-space: nowrap;
  }}
  .footer {{ color: #aaa; font-size: 11px; margin-top: 36px; border-top: 1px solid #e8e8e8; padding-top: 12px; }}
  .footer a {{ color: #aaa; }}
</style>
</head>
<body>
<h1>MC Firm Alert &mdash; {date_str}</h1>
<div class="banner">
  <strong>{len(matches)} posting{"s" if len(matches) != 1 else ""} from {len(by_firm)} top consulting firm{"s" if len(by_firm) != 1 else ""}.</strong><br>
  <span style="color:#555">Firms matched from the Management Consulted top-firm list.</span>
</div>
"""

    for firm, jobs in by_firm.items():
        count_label = f'{len(jobs)} posting{"s" if len(jobs) != 1 else ""}'
        html += (
            f'<h2>{firm} '
            f'<span style="font-weight:normal;color:#888;font-size:11px">({count_label})</span>'
            f'</h2>\n'
        )

        html += '<div class="table-wrap">\n'
        html += (
            "<table>\n<thead><tr>"
            '<th class="col-num">#</th>'
            '<th class="col-title">Position Title</th>'
            '<th class="col-cat">Category</th>'
            '<th class="col-location">Location</th>'
            '<th class="col-model">Work Model</th>'
            '<th class="col-salary">Salary</th>'
            '<th class="col-hire">Hire Time</th>'
            '<th class="col-apply">Apply</th>'
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
                f"<tr>"
                f'<td class="col-num">{i}</td>'
                f'<td class="col-title"><strong>{j["title"]}</strong></td>'
                f'<td class="col-cat">{j["category"]}</td>'
                f'<td class="col-location">{j["location"]}</td>'
                f'<td class="col-model">{badge}</td>'
                f'<td class="col-salary">{j["salary"] or "&mdash;"}</td>'
                f'<td class="col-hire">{j["hire_time"] or "&mdash;"}</td>'
                f'<td class="col-apply">{apply_cell}</td>'
                f"</tr>\n"
            )

        html += "</tbody>\n</table>\n</div>\n"

    firms_listed = ", ".join(by_firm.keys())
    html += (
        f'<div class="footer">'
        f'Firms matched: {firms_listed}<br>'
        f'Scraped from <a href="https://intern-list.com">intern-list.com</a> via jobright.ai &bull; {date_str}'
        f"</div>\n</body></html>"
    )
    return html


def send_mc_alert(
    matches: list[dict],
    date_str: str,
    gmail_user: str,
    gmail_password: str,
    recipient: str,
    dry_run: bool,
) -> None:
    if not matches:
        return

    html = build_mc_alert_html(matches, date_str)
    firm_names = sorted({m["matched_firm"] for m in matches})
    subject = f"MC Firm Alert ({len(matches)} posting{'s' if len(matches) != 1 else ''}): {', '.join(firm_names)}"

    if dry_run or not gmail_user or not gmail_password:
        out = "mc_alert_preview.html"
        with open(out, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"\nMC alert dry run — HTML saved to {out}")
        return

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = gmail_user
    msg["To"] = recipient
    msg.attach(MIMEText(html, "html", "utf-8"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(gmail_user, gmail_password)
        smtp.sendmail(gmail_user, recipient, msg.as_string())

    print(f"MC firm alert sent to {recipient} — {len(matches)} match(es) from: {', '.join(firm_names)}")
