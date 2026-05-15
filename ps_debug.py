#!/usr/bin/env python3
"""
PowerSchool scraper debug script.
Run: POWER_USERN=xxx POWER_PASS=yyy python ps_debug.py
Dumps each step to stdout so you can see exactly where login fails.
"""
import os, sys, hashlib, re
import requests
from bs4 import BeautifulSoup

PS_BASE_URL = "https://powerschool.pcschools.us"
USER = os.environ.get("POWER_USERN", "").strip()
PASS = os.environ.get("POWER_PASS", "").strip()

if not USER or not PASS:
    print("ERROR: Set POWER_USERN and POWER_PASS env vars first.")
    sys.exit(1)

def md5(s):
    return hashlib.md5(s.encode()).hexdigest()

sess = requests.Session()
sess.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
})

# ── STEP 1: GET login page ────────────────────────────────────────────────────
print("=" * 70)
print("STEP 1: GET /public/")
r1 = sess.get(f"{PS_BASE_URL}/public/", timeout=20)
print(f"  Status : {r1.status_code}")
print(f"  URL    : {r1.url}")
print(f"  Cookies: {dict(sess.cookies)}")
print(f"  Length : {len(r1.text)} chars")

soup = BeautifulSoup(r1.text, "html.parser")
form = soup.find("form", id="LoginForm") or soup.find("form")
if not form:
    print("  ERROR: no <form> found on page!")
    print(r1.text[:1000])
    sys.exit(1)

action = (form.get("action") or "/public/").strip()
if not action.startswith("http"):
    action = PS_BASE_URL + ("" if action.startswith("/") else "/") + action
print(f"  Form action: {action}")

# Collect all inputs
payload = {}
for inp in form.find_all("input"):
    name = inp.get("name", "")
    if name:
        payload[name] = inp.get("value") or ""

print(f"  Hidden fields: {[k for k in payload if payload[k]]}")
print(f"  All field names: {sorted(payload.keys())}")

pstoken = payload.get("pstoken", "")
print(f"  pstoken = {pstoken[:60] if pstoken else '(empty)'}")

pw_hash = md5(USER.lower() + ":" + md5(PASS) + ":" + pstoken)
payload.update({
    "account":      USER,
    "ldappassword": PASS,
    "pw":           pw_hash,
    "dbpw":         pw_hash,
})

# ── STEP 2: POST login ────────────────────────────────────────────────────────
print("\n" + "=" * 70)
print("STEP 2: POST login")
print(f"  Posting to: {action}")
r2 = sess.post(action, data=payload, timeout=20, allow_redirects=True)
print(f"  Status : {r2.status_code}")
print(f"  URL    : {r2.url}")
print(f"  Cookies: {dict(sess.cookies)}")
print(f"  Length : {len(r2.text)} chars")

# Check if still on login page
still_login = (
    'name="account"' in r2.text.lower()
    or 'name="ldappassword"' in r2.text.lower()
    or 'id="fieldaccount"' in r2.text.lower()
)
print(f"  Still on login page? {still_login}")
if still_login:
    print("  LOGIN FAILED — check credentials or network")
    err_el = BeautifulSoup(r2.text, "html.parser").find(id="LoginErrorMessages")
    if err_el:
        print(f"  Error message: {err_el.get_text(strip=True)}")
    print("\n  Response preview:")
    print(r2.text[:2000])
    sys.exit(1)

print("  LOGIN SUCCEEDED")
home_url = r2.url

# ── STEP 3: Parse grades ──────────────────────────────────────────────────────
print("\n" + "=" * 70)
print(f"STEP 3: Parse grades from {home_url}")
soup3 = BeautifulSoup(r2.text, "html.parser")
tables = soup3.find_all("table")
print(f"  Tables found: {len(tables)}")

main_table = None
for i, tbl in enumerate(tables):
    links = tbl.find_all("a", href=lambda h: h and "scores.html" in (h or ""))
    if links:
        print(f"  Found grades table (table {i}, {len(links)} score links)")
        main_table = tbl
        break

if not main_table:
    print("  No grades table with scores.html links — trying guardian/home.html")
    r3 = sess.get(f"{PS_BASE_URL}/guardian/home.html", timeout=20)
    print(f"  guardian/home.html status={r3.status_code} url={r3.url}")
    soup3 = BeautifulSoup(r3.text, "html.parser")
    for i, tbl in enumerate(soup3.find_all("table")):
        links = tbl.find_all("a", href=lambda h: h and "scores.html" in (h or ""))
        if links:
            print(f"  Found grades table (table {i}, {len(links)} score links)")
            main_table = tbl
            break

if not main_table:
    print("  ERROR: no grades table found anywhere")
    print("  Page preview (first 2000 chars):")
    print(r2.text[:2000])
    sys.exit(1)

print("\n  Raw table text:")
print(main_table.get_text(" | ", strip=True)[:2000])

letter_re = re.compile(r"^[A-F][+-]?$")
pct_re    = re.compile(r"^(\d{1,3}(?:\.\d+)?)%?$")
print("\n  Parsed grades:")
for row in main_table.find_all("tr"):
    cells = row.find_all(["td", "th"])
    if len(cells) < 3 or cells[0].name == "th":
        continue
    course = cells[0].get_text(strip=True)
    teacher = cells[1].get_text(strip=True) if len(cells) > 1 else ""
    grade_letter, grade_pct = "", None
    for cell in cells[2:]:
        a = cell.find("a", href=lambda h: h and "scores.html" in (h or ""))
        if a:
            raw = a.get_text(strip=True)
            m = re.match(r"^([A-F][+-]?)\s*\((\d{1,3}(?:\.\d+)?)%?\)$", raw)
            if m:
                grade_letter, grade_pct = m.group(1), float(m.group(2))
            elif letter_re.match(raw):
                grade_letter = raw
            elif pct_re.match(raw):
                grade_pct = float(pct_re.match(raw).group(1))
            break
    if course:
        print(f"    {course!r:40s}  teacher={teacher!r:25s}  grade={grade_letter or '?'} {('('+str(grade_pct)+'%)') if grade_pct else ''}")

print("\nDone.")
