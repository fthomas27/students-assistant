import os
import time
import logging
import threading
import socket
import json
import ipaddress
import hashlib
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo

import psycopg2
import psycopg2.extras
from psycopg2 import sql as pgsql
import requests
from functools import wraps
from flask import Flask, request, jsonify, render_template, session, redirect
from werkzeug.middleware.proxy_fix import ProxyFix
from icalendar import Calendar
import recurring_ical_events
from apscheduler.schedulers.background import BackgroundScheduler
import anthropic

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1, x_prefix=1)
app.secret_key = os.environ.get("SECRET_KEY", "finn-dashboard-secret-change-me")
app.permanent_session_lifetime = timedelta(days=30)

app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SECURE=True,
    SESSION_COOKIE_SAMESITE='Strict',
    PREFERRED_URL_SCHEME='https'
)

APP_PASSWORD = os.environ.get("APP_PASSWORD", "finn2025").strip()
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin-change-me").strip()
AVERAGE_USER = os.environ.get("AVERAGE_USER", "user").strip()
ADMIN_USER = os.environ.get("ADMIN_USER", "admin").strip()
PARENT_USER = os.environ.get("PARENT_USER", "PARENT_USER").strip()
PARENT_PASSWORD = os.environ.get("PARENT_PASSWORD", "PARENT_PASSWORD").strip()

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

# Default timezone - will be overridden by config if available
_TZ_DEFAULT = ZoneInfo("America/Denver")

def get_tz():
    """Get configured timezone from config, default to America/Denver (Mountain Time)."""
    try:
        cfg = get_config()
        tz_str = cfg.get("timezone", "America/Denver")
        return ZoneInfo(tz_str)
    except Exception:
        return _TZ_DEFAULT

# For backward compatibility, initialize with default
TZ = _TZ_DEFAULT

_api_usage_cache = {"tokens_used": 0, "tokens_limit": 1000000, "last_updated": None}

def track_api_usage(response):
    """Extract and track API usage from Claude API response."""
    global _api_usage_cache
    try:
        if hasattr(response, 'usage'):
            tokens = response.usage.input_tokens + response.usage.output_tokens
            _api_usage_cache["tokens_used"] = _api_usage_cache.get("tokens_used", 0) + tokens
            _api_usage_cache["last_updated"] = datetime.now(TZ)
            log.debug(f"Tracked {tokens} tokens. Total: {_api_usage_cache['tokens_used']}")
    except Exception as e:
        log.warning(f"Error tracking API usage: {e}")

_briefing_lock = threading.Lock()


@app.before_request
def require_auth():
    path = request.path.rstrip('/')
    if path in ('/login', '/logout', '/admin', '/parent'):
        return None
    if path in ('/api/lockdown-status', '/api/test-lockdown-status', '/api/test-security-code', '/api/test-admin-password'):
        return None
    if path.startswith('/api/admin/'):
        return None
    if path.startswith('/api/parent/'):
        if not session.get("parent_authenticated"):
            return jsonify({"error": "Not authenticated"}), 401
        return None
    if not session.get("authenticated"):
        if path.startswith('/api/'):
            return jsonify({"error": "Not authenticated"}), 401
        return redirect("/login")

_workout_lock = threading.Lock()
_plan_lock = threading.Lock()

# Workout rotation: advances each time a plan is generated (not by calendar day).
WORKOUT_FOCUS_CYCLE = [
    ("back", "Back"),
    ("biceps_triceps", "Biceps & Triceps"),
    ("core_cardio", "Core / Cardio"),
    ("legs", "Legs"),
    ("shoulders", "Shoulders"),
]

# ── Calendar URLs from environment variables ──────────────────────────────────
PERSONAL_ICAL_URL = os.environ.get("PERSONAL_ICAL_URL", "")
CANVAS_ICAL_URL = os.environ.get("CANVAS_ICAL_URL", "")
SPORTS_ICAL_URL = os.environ.get("SPORTS_ICAL_URL", "")
RED_DAY_ICAL_URL = os.environ.get("RED_DAY_ICAL_URL", "https://calendar.google.com/calendar/ical/pcschools.us_7ufb5f1vj8aks1shds5ou4fhe8%40group.calendar.google.com/public/basic.ics")
WHITE_DAY_ICAL_URL = os.environ.get("WHITE_DAY_ICAL_URL", "https://calendar.google.com/calendar/ical/pcschools.us_64ohm1bccvi50iti8fe455stkg%40group.calendar.google.com/public/basic.ics")

# ── Default values ─────────────────────────────────────────────────────────────
DEFAULT_ESTIMATE_MINS = 30

# ── Park City School District 2025-2026 Bell Schedule ────────────────────────
# Red Day = shorter (A-block), White Day = longer (B-block), alternating each school day
# First day of school: 2025-08-18 (Red day)
SCHOOL_YEAR_START = date(2025, 8, 18)
SCHOOL_YEAR_END = date(2026, 6, 5)

# All dates with no school (students)
_ns_ranges = [
    (date(2025, 8, 7), date(2025, 8, 15)),   # Teacher work days before school
    (date(2025, 9, 1), date(2025, 9, 1)),    # Labor Day
    (date(2025, 9, 23), date(2025, 9, 23)),  # Rosh Hashanah
    (date(2025, 10, 2), date(2025, 10, 3)),  # Yom Kippur + Fall Break
    (date(2025, 11, 7), date(2025, 11, 7)),  # Prof Development
    (date(2025, 11, 26), date(2025, 11, 28)),# Thanksgiving
    (date(2025, 12, 22), date(2026, 1, 2)),  # Winter Break
    (date(2026, 1, 19), date(2026, 1, 19)),  # MLK Day
    (date(2026, 2, 16), date(2026, 2, 20)),  # Presidents Day + February Break
    (date(2026, 3, 20), date(2026, 3, 20)),  # Prof Development
    (date(2026, 4, 13), date(2026, 4, 17)),  # Teacher Comp + Spring Break
    (date(2026, 5, 22), date(2026, 5, 22)),  # Make Up Snow Day
    (date(2026, 5, 25), date(2026, 5, 25)),  # Memorial Day
]
NO_SCHOOL_DATES = set()
for _s, _e in _ns_ranges:
    _cur = _s
    while _cur <= _e:
        NO_SCHOOL_DATES.add(_cur)
        _cur += timedelta(days=1)


def is_school_day(d):
    """Return True if d is a regular school day (weekday, not holiday, within school year)."""
    if d < SCHOOL_YEAR_START or d > SCHOOL_YEAR_END:
        return False
    if d.weekday() >= 5:  # Saturday/Sunday
        return False
    return d not in NO_SCHOOL_DATES


def _build_day_type_cache():
    cache = {}
    cur = SCHOOL_YEAR_START
    count = 0
    while cur <= SCHOOL_YEAR_END:
        if is_school_day(cur):
            cache[cur] = "red" if count % 2 == 0 else "white"
            count += 1
        else:
            cache[cur] = None
        cur += timedelta(days=1)
    return cache

_DAY_TYPE_CACHE = _build_day_type_cache()


def get_day_type(d):
    """Return 'red', 'white', or None for non-school days. O(1) lookup."""
    return _DAY_TYPE_CACHE.get(d)


def get_school_hours(d):
    """Return (start_hour, start_min, end_hour, end_min) for school on day d, or None."""
    dtype = get_day_type(d)
    if dtype is None:
        return None
    dow = d.weekday()  # 0=Mon, 4=Fri
    if dow == 4:  # Friday
        return (7, 30, 10, 25) if dtype == "red" else (7, 30, 11, 30)
    else:  # Mon-Thu
        return (7, 30, 11, 53) if dtype == "red" else (7, 30, 14, 25)


def get_day_calendar_url(d):
    """Return the appropriate day calendar URL (red or white) based on the day type."""
    dtype = get_day_type(d)
    day_urls = {
        "red": RED_DAY_ICAL_URL,
        "white": WHITE_DAY_ICAL_URL,
    }
    return day_urls.get(dtype)


def fetch_day_calendar_events(d, days_ahead=30):
    """Fetch calendar events from the appropriate day-specific calendar.

    Args:
        d: date object to determine red/white day
        days_ahead: number of days to fetch events for

    Returns:
        list of event dictionaries with source set to 'redday' or 'whiteday', or empty list if unavailable
    """
    day_type = get_day_type(d)
    day_cal_url = get_day_calendar_url(d)
    events = []

    if day_cal_url:
        cal = fetch_ical(day_cal_url)
        if cal:
            for e in parse_calendar_events(cal, days_ahead=days_ahead):
                e["source"] = f"{day_type}day"
                events.append(e)

    return events


def get_db():
    url = os.environ.get("DATABASE_URL", "")
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    return psycopg2.connect(url, cursor_factory=psycopg2.extras.RealDictCursor)


def init_db():
    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
CREATE TABLE IF NOT EXISTS config (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
)""")

    cur.execute("""
CREATE TABLE IF NOT EXISTS completions (
    id SERIAL PRIMARY KEY,
    completed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    assignment_title TEXT NOT NULL,
    class_name TEXT NOT NULL DEFAULT '',
    duration_minutes REAL NOT NULL DEFAULT 0,
    estimate_minutes REAL NOT NULL DEFAULT 0,
    timed BOOLEAN NOT NULL DEFAULT TRUE
)""")

    cur.execute("""
CREATE TABLE IF NOT EXISTS assignment_estimates (
    uid TEXT PRIMARY KEY,
    minutes REAL NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
)""")

    cur.execute("""
CREATE TABLE IF NOT EXISTS timer_state (
    id INT PRIMARY KEY DEFAULT 1,
    assignment_uid TEXT NOT NULL DEFAULT '',
    assignment_title TEXT NOT NULL DEFAULT '',
    class_name TEXT NOT NULL DEFAULT '',
    estimate_minutes REAL NOT NULL DEFAULT 30,
    started_at TIMESTAMPTZ,
    paused_at TIMESTAMPTZ,
    accumulated_seconds REAL NOT NULL DEFAULT 0,
    active BOOLEAN NOT NULL DEFAULT FALSE
)""")

    cur.execute("""
CREATE TABLE IF NOT EXISTS briefing_cache (
    id INT PRIMARY KEY DEFAULT 1,
    generated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    content TEXT NOT NULL DEFAULT ''
)""")

    cur.execute("""
CREATE TABLE IF NOT EXISTS debrief_cache (
    id INT PRIMARY KEY DEFAULT 1,
    generated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    content TEXT NOT NULL DEFAULT ''
)""")

    cur.execute("""
CREATE TABLE IF NOT EXISTS tasks (
    id SERIAL PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    title TEXT NOT NULL,
    notes TEXT NOT NULL DEFAULT '',
    urgency TEXT NOT NULL DEFAULT 'low',
    completed BOOLEAN NOT NULL DEFAULT FALSE,
    completed_at TIMESTAMPTZ,
    due_date DATE,
    created_by_parent BOOLEAN NOT NULL DEFAULT FALSE
)""")

    # Add created_by_parent column if it doesn't exist (migration)
    try:
        cur.execute("ALTER TABLE tasks ADD COLUMN created_by_parent BOOLEAN NOT NULL DEFAULT FALSE")
    except psycopg2.Error:
        pass  # Column already exists

    cur.execute("""
CREATE TABLE IF NOT EXISTS projects (
    id SERIAL PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active',
    lead TEXT NOT NULL DEFAULT '',
    members TEXT NOT NULL DEFAULT '',
    last_checkin TIMESTAMPTZ,
    checkin_interval_days INT NOT NULL DEFAULT 7,
    completion_pct INT NOT NULL DEFAULT 0
)""")

    cur.execute("""
CREATE TABLE IF NOT EXISTS project_notes (
    id SERIAL PRIMARY KEY,
    project_id INT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    content TEXT NOT NULL
)""")

    cur.execute("""
CREATE TABLE IF NOT EXISTS project_tasks (
    id SERIAL PRIMARY KEY,
    project_id INT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    title TEXT NOT NULL,
    notes TEXT NOT NULL DEFAULT '',
    assignee TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    due_date DATE
)""")

    cur.execute("""
CREATE TABLE IF NOT EXISTS recurring_tasks (
    id SERIAL PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    title TEXT NOT NULL,
    notes TEXT NOT NULL DEFAULT '',
    urgency TEXT NOT NULL DEFAULT 'low',
    recurrence TEXT NOT NULL,
    last_created_at TIMESTAMPTZ,
    active BOOLEAN NOT NULL DEFAULT TRUE
)""")

    cur.execute("""
CREATE TABLE IF NOT EXISTS workout_state (
    id INT PRIMARY KEY DEFAULT 1,
    last_focus_index INT NOT NULL DEFAULT -1
)""")

    cur.execute("""
CREATE TABLE IF NOT EXISTS workout_logs (
    id SERIAL PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    focus_key TEXT NOT NULL,
    focus_label TEXT NOT NULL,
    intensity INT NOT NULL,
    location TEXT NOT NULL,
    plan_content TEXT NOT NULL,
    user_notes TEXT NOT NULL DEFAULT '',
    perceived_difficulty INT
)""")

    cur.execute("""
CREATE TABLE IF NOT EXISTS daily_plans (
    id SERIAL PRIMARY KEY,
    plan_date DATE NOT NULL,
    generated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    needs_update BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(plan_date)
)""")

    cur.execute("""
CREATE TABLE IF NOT EXISTS daily_plan_items (
    id SERIAL PRIMARY KEY,
    plan_id INTEGER NOT NULL REFERENCES daily_plans(id) ON DELETE CASCADE,
    item_type VARCHAR(20) NOT NULL,
    item_id VARCHAR(255),
    item_title VARCHAR(500) NOT NULL,
    scheduled_start_time TIME NOT NULL,
    scheduled_end_time TIME NOT NULL,
    estimated_minutes INTEGER,
    order_index INTEGER,
    completed BOOLEAN NOT NULL DEFAULT FALSE,
    completed_at TIMESTAMPTZ,
    user_edited BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
)""")

    cur.execute("""
CREATE TABLE IF NOT EXISTS login_attempts (
    id SERIAL PRIMARY KEY,
    ip_address TEXT NOT NULL,
    success BOOLEAN NOT NULL,
    attempted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    user_agent TEXT
)""")

    cur.execute("""
CREATE TABLE IF NOT EXISTS login_lockouts (
    ip_address TEXT PRIMARY KEY,
    locked_until TIMESTAMPTZ NOT NULL,
    failure_count INT NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
)""")

    cur.execute("""
CREATE TABLE IF NOT EXISTS lockdown_state (
    id INT PRIMARY KEY DEFAULT 1,
    is_locked_down BOOLEAN NOT NULL DEFAULT FALSE,
    activated_at TIMESTAMPTZ,
    activated_by TEXT,
    CHECK (id = 1)
)""")

    cur.execute("""
CREATE TABLE IF NOT EXISTS blocked_ips (
    id SERIAL PRIMARY KEY,
    ip_address TEXT UNIQUE NOT NULL,
    ip_name TEXT DEFAULT '',
    blocked_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    blocked_by TEXT NOT NULL DEFAULT 'admin',
    reason TEXT DEFAULT ''
)""")

    # Add ip_name column if it doesn't exist (for existing databases)
    try:
        cur.execute("ALTER TABLE blocked_ips ADD COLUMN ip_name TEXT DEFAULT ''")
    except:
        pass

    cur.execute("""
CREATE TABLE IF NOT EXISTS ip_names (
    ip_address TEXT PRIMARY KEY,
    ip_name TEXT NOT NULL,
    tracked_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
)""")

    # Migrate existing IP names from blocked_ips table to preserve them
    try:
        cur.execute("""
INSERT INTO ip_names (ip_address, ip_name)
SELECT ip_address, ip_name FROM blocked_ips
WHERE ip_name IS NOT NULL AND ip_name != ''
ON CONFLICT (ip_address) DO NOTHING""")
    except:
        pass

    defaults = {
        "name": "Jarvis",
        "morning_briefing_time": "07:00",
        "timer_cutoff_multiplier": "2.0",
        "anthropic_api_key": "",
        "weekly_recap_advisor": "Mr. Goldberg",
        "formal_signoff_name": "Finley Thomas",
    }
    for k, v in defaults.items():
        cur.execute("""
INSERT INTO config (key, value) VALUES (%s, %s)
ON CONFLICT (key) DO NOTHING""", (k, v))

    cur.execute("INSERT INTO timer_state (id) VALUES (1) ON CONFLICT (id) DO NOTHING")
    cur.execute("INSERT INTO briefing_cache (id, content) VALUES (1, '') ON CONFLICT (id) DO NOTHING")
    cur.execute("INSERT INTO debrief_cache (id, content) VALUES (1, '') ON CONFLICT (id) DO NOTHING")
    cur.execute("INSERT INTO workout_state (id, last_focus_index) VALUES (1, -1) ON CONFLICT (id) DO NOTHING")
    cur.execute("INSERT INTO lockdown_state (id, is_locked_down) VALUES (1, FALSE) ON CONFLICT (id) DO NOTHING")

    # Create indexes for frequently queried columns
    cur.execute("CREATE INDEX IF NOT EXISTS idx_completions_assignment_title ON completions(assignment_title)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_tasks_completed ON tasks(completed, created_at DESC)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_projects_status ON projects(status)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_project_tasks_assignee_status ON project_tasks(assignee, status)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_tasks_due_date ON tasks(due_date)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_completions_completed_at ON completions(completed_at DESC)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_daily_plans_date ON daily_plans(plan_date)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_daily_plan_items_plan_id ON daily_plan_items(plan_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_daily_plan_items_completed ON daily_plan_items(completed)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_login_attempts_ip ON login_attempts(ip_address, attempted_at DESC)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_login_lockouts_ip ON login_lockouts(ip_address)")

    conn.commit()
    cur.close()
    conn.close()
    log.info("Database initialized.")


_config_cache = None
_config_cache_ts = 0.0
_config_cache_lock = threading.Lock()
CONFIG_CACHE_TTL = 30  # seconds


def get_config():
    global _config_cache, _config_cache_ts
    with _config_cache_lock:
        if _config_cache is not None and (time.monotonic() - _config_cache_ts) < CONFIG_CACHE_TTL:
            return _config_cache
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT key, value FROM config")
    rows = cur.fetchall()
    cur.close()
    conn.close()
    result = {r["key"]: r["value"] for r in rows}
    with _config_cache_lock:
        _config_cache = result
        _config_cache_ts = time.monotonic()
    return result


def set_config(updates):
    global _config_cache
    conn = get_db()
    cur = conn.cursor()
    for k, v in updates.items():
        cur.execute("""
INSERT INTO config (key, value) VALUES (%s, %s)
ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value""", (k, str(v)))
    conn.commit()
    cur.close()
    conn.close()
    with _config_cache_lock:
        _config_cache = None  # invalidate


_ical_cache = {}  # url -> (monotonic_time, Calendar)
_ical_cache_lock = threading.Lock()
_ical_inflight = {}  # url -> threading.Event for request coalescing
ICAL_CACHE_TTL = 300  # 5 minutes


def fetch_ical(url):
    if not url:
        return None
    if url.startswith("webcal://"):
        url = "https://" + url[9:]
    now = time.monotonic()

    with _ical_cache_lock:
        # Check cache first
        if url in _ical_cache:
            cached_at, cached_cal = _ical_cache[url]
            if now - cached_at < ICAL_CACHE_TTL:
                return cached_cal

        # Check if another thread is already fetching this URL
        if url in _ical_inflight:
            event = _ical_inflight[url]
        else:
            event = None

    # If another thread is fetching, wait for it (do this outside the lock to avoid deadlock)
    if event is not None:
        log.info(f"iCal: waiting for another thread to fetch {url}")
        event.wait(timeout=20)
        with _ical_cache_lock:
            if url in _ical_cache:
                cached_at, cached_cal = _ical_cache[url]
                return cached_cal
        return None

    # Mark this URL as being fetched
    new_event = threading.Event()
    with _ical_cache_lock:
        # Double-check another thread didn't start in the meantime
        if url in _ical_inflight:
            # Another thread started fetching, wait for it instead
            event = _ical_inflight[url]
        else:
            _ical_inflight[url] = new_event
            event = None

    # If we found another thread was fetching, wait for it
    if event is not None:
        log.info(f"iCal: another thread started fetching {url}, waiting...")
        event.wait(timeout=20)
        with _ical_cache_lock:
            if url in _ical_cache:
                cached_at, cached_cal = _ical_cache[url]
                return cached_cal
        return None

    # We own the fetch now
    try:
        log.info(f"iCal: fetching {url}")
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        cal = Calendar.from_ical(resp.content)
        with _ical_cache_lock:
            _ical_cache[url] = (time.monotonic(), cal)
        new_event.set()  # Signal other waiting threads
        log.info(f"iCal: successfully cached {url}")
        return cal
    except Exception as e:
        log.warning("iCal fetch failed for %s: %s", url, e)
        new_event.set()  # Signal other waiting threads even on failure
        # Return stale cache on failure rather than None
        with _ical_cache_lock:
            if url in _ical_cache:
                cached_at, cached_cal = _ical_cache[url]
                log.info(f"iCal: returning stale cache for {url} after fetch error")
                return cached_cal
        return None
    finally:
        with _ical_cache_lock:
            _ical_inflight.pop(url, None)  # Clean up the inflight marker


def parse_canvas_assignments(cal):
    assignments = []
    now_utc = datetime.utcnow().replace(tzinfo=ZoneInfo("UTC"))
    cutoff = now_utc + timedelta(days=14)
    for component in cal.walk():
        if component.name != "VEVENT":
            continue
        uid = str(component.get("UID", ""))
        summary = str(component.get("SUMMARY", "Untitled"))
        description = str(component.get("DESCRIPTION", ""))
        teacher = str(component.get("ORGANIZER", ""))
        due_dt = component.get("DTSTART") or component.get("DUE")
        if due_dt is None:
            continue
        due_val = due_dt.dt
        if isinstance(due_val, date) and not isinstance(due_val, datetime):
            due_val = datetime(due_val.year, due_val.month, due_val.day, 23, 59, 0, tzinfo=ZoneInfo("UTC"))
        if due_val.tzinfo is None:
            due_val = due_val.replace(tzinfo=ZoneInfo("UTC"))
        if due_val < now_utc or due_val > cutoff:
            continue
        class_name = ""
        title = summary
        if " - " in summary:
            parts = summary.rsplit(" - ", 1)
            title = parts[0].strip()
            class_name = parts[1].strip()
        delta = due_val - now_utc
        if delta.total_seconds() < 86400:
            urgency = "high"
        elif delta.total_seconds() < 259200:
            urgency = "medium"
        else:
            urgency = "low"
        assignments.append({
            "uid": uid,
            "title": title,
            "class_name": class_name,
            "description": description[:1000],
            "teacher": teacher,
            "due_iso": due_val.astimezone(TZ).isoformat(),
            "due_display": due_val.astimezone(TZ).strftime("%a %b %-d at %-I:%M %p"),
            "urgency": urgency
        })
    assignments.sort(key=lambda x: x["due_iso"])
    return assignments


def parse_calendar_events(cal, days_ahead=30):
    events = []
    now_local = datetime.now(TZ)
    today_start = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    range_end = today_start + timedelta(days=days_ahead)
    try:
        components = recurring_ical_events.of(cal).between(today_start, range_end)
    except Exception as e:
        log.warning("recurring_ical_events failed, falling back: %s", e)
        components = [c for c in cal.walk() if c.name == "VEVENT"]
    for component in components:
        if component.name != "VEVENT":
            continue
        summary = str(component.get("SUMMARY", "Untitled"))
        location = str(component.get("LOCATION", ""))
        description = str(component.get("DESCRIPTION", ""))[:500]
        start_dt = component.get("DTSTART")
        end_dt = component.get("DTEND")
        if start_dt is None:
            continue
        start_val = start_dt.dt
        all_day = isinstance(start_val, date) and not isinstance(start_val, datetime)
        if all_day:
            start_val = datetime(start_val.year, start_val.month, start_val.day, 0, 0, 0, tzinfo=TZ)
        if start_val.tzinfo is None:
            start_val = start_val.replace(tzinfo=TZ)
        start_local = start_val.astimezone(TZ)
        end_local = None
        if end_dt:
            end_val = end_dt.dt
            if isinstance(end_val, date) and not isinstance(end_val, datetime):
                end_val = datetime(end_val.year, end_val.month, end_val.day, 23, 59, 0, tzinfo=TZ)
            if end_val.tzinfo is None:
                end_val = end_val.replace(tzinfo=TZ)
            end_local = end_val.astimezone(TZ)
        events.append({
            "title": summary,
            "location": location,
            "notes": description,
            "start_display": "All Day" if all_day else start_local.strftime("%-I:%M %p"),
            "end_display": end_local.strftime("%-I:%M %p") if end_local and not all_day else "",
            "start_iso": start_local.isoformat(),
            "end_iso": end_local.isoformat() if end_local else "",
            "date": start_local.strftime("%Y-%m-%d"),
            "all_day": all_day
        })
    events.sort(key=lambda x: x["start_iso"])
    return events


KEYWORD_ESTIMATES = {
    "essay": 45, "paper": 45, "write": 45, "writing": 45,
    "worksheet": 30, "problems": 30, "exercises": 30,
    "reading": 25, "read": 25, "chapter": 25,
    "vocab": 15, "vocabulary": 15, "flashcard": 15,
    "quiz": 20, "test": 20
}


def get_class_average(class_name):
    if not class_name:
        return None
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
SELECT AVG(duration_minutes) as avg FROM (
    SELECT duration_minutes FROM completions
    WHERE class_name = %s AND timed = TRUE AND duration_minutes > 0
    ORDER BY completed_at DESC LIMIT 20
) sub""", (class_name,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    if row and row["avg"] is not None:
        return round(float(row["avg"]), 1)
    return None


def get_class_averages_batch(class_names):
    """Batch query for multiple class averages - avoids N+1 queries."""
    if not class_names:
        return {}
    conn = get_db()
    cur = conn.cursor()
    # Get all class averages in a single query
    cur.execute("""
SELECT class_name, AVG(duration_minutes) as avg FROM (
    SELECT class_name, duration_minutes, ROW_NUMBER() OVER (PARTITION BY class_name ORDER BY completed_at DESC) as rn
    FROM completions
    WHERE class_name = ANY(%s) AND timed = TRUE AND duration_minutes > 0
) sub WHERE rn <= 20
GROUP BY class_name""", (list(class_names),))
    result = {}
    for row in cur.fetchall():
        if row["avg"] is not None:
            result[row["class_name"]] = round(float(row["avg"]), 1)
    cur.close()
    conn.close()
    return result


def estimate_assignment(title, class_name, class_avg_cache=None):
    if class_avg_cache and class_name in class_avg_cache:
        avg = class_avg_cache[class_name]
        if avg:
            return avg
    elif not class_avg_cache:
        avg = get_class_average(class_name)
        if avg:
            return avg
    title_lower = title.lower()
    for kw, mins in KEYWORD_ESTIMATES.items():
        if kw in title_lower:
            return float(mins)
    return 30.0


def _assignment_due_date_local(a):
    di = a.get("due_iso") or ""
    if di.endswith("Z"):
        di = di[:-1] + "+00:00"
    return datetime.fromisoformat(di).astimezone(TZ).date()


def _is_quiz_or_test_title(title):
    t = (title or "").lower()
    return "quiz" in t or "test" in t


def _is_big_work_assignment(a):
    est = estimate_assignment(a.get("title", ""), a.get("class_name", ""))
    if est >= 45:
        return True
    blob = ((a.get("title") or "") + " " + (a.get("class_name") or "")).lower()
    for kw in ("paper", "essay", "project", "presentation", "research", "portfolio"):
        if kw in blob:
            return True
    return False


def generate_briefing(force=False):
    with _briefing_lock:
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            log.warning("Morning briefing: ANTHROPIC_API_KEY not set")
            return
        cfg = get_config()
        name = cfg.get("name", "Jarvis")
        if not force:
            conn = get_db()
            cur = conn.cursor()
            cur.execute("SELECT generated_at FROM briefing_cache WHERE id = 1")
            row = cur.fetchone()
            cur.close()
            conn.close()
            if row and row["generated_at"]:
                age = datetime.now(TZ) - row["generated_at"].astimezone(TZ)
                if age.total_seconds() < 3600:
                    return

        assignments = []
        cal = fetch_ical(CANVAS_ICAL_URL)
        if cal:
            assignments = parse_canvas_assignments(cal)

        events = []
        cal2 = fetch_ical(PERSONAL_ICAL_URL)
        if cal2:
            events = list(parse_calendar_events(cal2, days_ahead=1))
        today = datetime.now(TZ).date()
        events.extend(fetch_day_calendar_events(today, days_ahead=1))

        # Get completed assignment titles (ever) so we don't flag them
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT DISTINCT assignment_title FROM completions")
        completed_titles = set(r["assignment_title"] for r in cur.fetchall())
        assignments = [a for a in assignments if a["title"] not in completed_titles]

        # Get tasks
        cur.execute("SELECT title, urgency FROM tasks WHERE completed = FALSE ORDER BY urgency DESC, created_at ASC LIMIT 5")
        tasks = [dict(r) for r in cur.fetchall()]

        # Get stale projects
        cur.execute("""
SELECT title, last_checkin, checkin_interval_days FROM projects
WHERE status = 'active' AND (last_checkin IS NULL OR
    NOW() - last_checkin > make_interval(days => checkin_interval_days))
LIMIT 3""")
        stale_projects = [dict(r) for r in cur.fetchall()]
        cur.close()
        conn.close()

        now_local = datetime.now(TZ)
        now_str = now_local.strftime("%A, %B %-d, %Y at %-I:%M %p")
        today = now_local.date()

        asgn_sorted = sorted(assignments, key=lambda a: a.get("due_iso", ""))

        # Batch load all class averages to avoid N+1 queries
        class_names = set(a.get("class_name") for a in asgn_sorted if a.get("class_name"))
        class_avg_cache = get_class_averages_batch(class_names)

        def _fmt_asgn(a):
            est = int(estimate_assignment(a["title"], a["class_name"], class_avg_cache=class_avg_cache))
            return "%s (%s) due %s, ~%d min, urgency %s" % (
                a["title"], a["class_name"], a["due_display"], est, a.get("urgency", "medium"))

        overdue = [a for a in asgn_sorted if _assignment_due_date_local(a) < today]
        due_today = [a for a in asgn_sorted if _assignment_due_date_local(a) == today]
        due_tmr = [a for a in asgn_sorted if _assignment_due_date_local(a) == today + timedelta(days=1)]
        due_2d = [a for a in asgn_sorted if _assignment_due_date_local(a) == today + timedelta(days=2)]

        overdue_work = [_fmt_asgn(a) for a in overdue if not _is_quiz_or_test_title(a["title"])]
        today_work = [_fmt_asgn(a) for a in due_today if not _is_quiz_or_test_title(a["title"])]
        today_qt = [a for a in due_today if _is_quiz_or_test_title(a["title"])]
        overdue_qt = [a for a in overdue if _is_quiz_or_test_title(a["title"])]

        good_time_lines = [_fmt_asgn(a) for a in due_tmr]
        for a in due_2d:
            if a.get("urgency") == "high":
                good_time_lines.append(_fmt_asgn(a))

        listed_titles = set()
        for group in (overdue, due_today, due_tmr):
            for a in group:
                listed_titles.add(a["title"])
        big_longterm = [
            _fmt_asgn(a) for a in asgn_sorted
            if _is_big_work_assignment(a) and _assignment_due_date_local(a) > today
            and a["title"] not in listed_titles
        ][:6]

        lines_overdue_work = "\n".join("- " + x for x in overdue_work) or "- None."
        lines_today_work = "\n".join("- " + x for x in today_work) or "- None."
        lines_good_time = "\n".join("- " + x for x in good_time_lines) or "- None."
        lines_big = "\n".join("- " + x for x in big_longterm) or "- None."

        qt_for_schedule = []
        for a in today_qt:
            c = (a.get("class_name") or "").strip() or "class"
            qt_for_schedule.append("⚠️ Quiz/test today in %s: %s" % (c, a["title"]))
        for a in overdue_qt:
            c = (a.get("class_name") or "").strip() or "class"
            qt_for_schedule.append("⚠️ Quiz/test overdue in %s: %s" % (c, a["title"]))
        quiz_test_block = "\n".join("- " + x for x in qt_for_schedule) or "- None (no quizzes/tests due today in the list)."

        week_end = today + timedelta(days=7)
        upcoming_qt_study = [
            a for a in asgn_sorted
            if _is_quiz_or_test_title(a["title"]) and today <= _assignment_due_date_local(a) <= week_end
        ]
        upcoming_qt_study.sort(key=lambda x: x.get("due_iso", ""))
        lines_qt_study = "\n".join(
            "- %s (%s) — due %s" % (a["title"], (a.get("class_name") or "").strip() or "class", a["due_display"])
            for a in upcoming_qt_study
        ) or "- None."

        events_text = "\n".join([
            "- %s%s at %s" % (e["title"], " [SPORTS]" if e.get("source") == "sports" else "", e["start_display"])
            for e in events
        ]) or "- No calendar events today."
        tasks_text = "\n".join(["- [%s] %s" % (t["urgency"], t["title"]) for t in tasks]) or "- No pending tasks."
        stale_text = "\n".join(["- %s (overdue check-in)" % p["title"] for p in stale_projects]) or "- None."

        # Get school schedule for today to recommend homework time
        school_hrs = get_school_hours(today)
        dtype = get_day_type(today)
        if school_hrs:
            _, _, eh, em = school_hrs
            end_ampm = "AM" if eh < 12 else "PM"
            school_end_str = "%d:%02d %s" % (eh % 12 or 12, em, end_ampm)
            schedule_note = "Today is a %s day. School ends at %s." % (dtype.title(), school_end_str)
        elif datetime.now(TZ).weekday() >= 5:
            schedule_note = "Today is a weekend — no school."
        else:
            schedule_note = "No school today."

        prompt = (
            "You are Jarvis, the exceptionally capable and sophisticated AI assistant of a high school student in Park City, Utah. "
            "You communicate with intelligence, wit, and refined professionalism. Address the student with respect. "
            "Provide analytical insights while maintaining an air of composed competence.\n\n"
            "Current time: %s\n"
            "School schedule note: %s\n\n"
            "REFERENCE — Overdue work (NOT quiz/test — never put quizzes/tests in Needs section):\n%s\n\n"
            "REFERENCE — Due today work (NOT quiz/test):\n%s\n\n"
            "REFERENCE — Quizzes/tests (for Schedule section ONLY, use EXACT warning lines below as bullets):\n%s\n\n"
            "REFERENCE — Quizzes/tests in the next 7 days including today (for study/review suggestions ONLY under "
            "\"If you have time\"; not as homework to turn in):\n%s\n\n"
            "REFERENCE — Good to do if time (due tomorrow or high-urgency in 2 days):\n%s\n\n"
            "REFERENCE — Larger / longer homework (papers, projects, big estimates, not already listed above):\n%s\n\n"
            "Today's calendar events:\n%s\n\n"
            "Pending tasks:\n%s\n\n"
            "Projects needing check-in:\n%s\n\n"
            "Compose a sophisticated daily briefing using EXACTLY these four markdown sections with ## headings (spell each heading exactly):\n\n"
            "## Priorities for Today:\n"
            "• Present the most critical priorities first. Combine OVERDUE WORK and DUE-TODAY WORK from the reference (not quiz/test).\n"
            "• If both reference lists are None/empty for work, write one bullet: All assigned work is currently current. An excellent position.\n"
            "• You may mention urgent tasks from Pending tasks if strategically relevant.\n\n"
            "## Secondary Objectives:\n"
            "• Bullets from the 'Good to do if time' reference; optional prep or lighter work that builds momentum.\n"
            "• **If the upcoming-quizzes reference is not \"- None.\":** add a bullet for each listed quiz/test "
            "recommending **preparation or review** for it today (e.g. \"A focused review of **Class** materials would be prudent before the quiz\"). "
            "Sooner due dates warrant more emphasis. For a quiz **today**, suggest a brief, focused review if time permits — retain the ⚠️ indicator under Schedule.\n"
            "• If the only items would be study bullets and you added those, you may omit filler text. "
            "If there are no secondary items and no upcoming quizzes, one bullet: All secondary objectives completed or not applicable.\n\n"
            "## Schedule:\n"
            "• First bullets: today's calendar events (paraphrase from Today's calendar events) with appropriate context.\n"
            "• Then add EVERY line from REFERENCE Quizzes/tests exactly as given (each ⚠️ line is its own bullet).\n"
            "• If no events and no quiz lines, one bullet: No scheduled calendar entries or assessments flagged.\n\n"
            "## Upcoming Commitments:\n"
            "• Bullets from REFERENCE Larger/longer; major assignments, projects, and significant work not already covered above.\n"
            "• If none, one bullet: No additional major commitments flagged.\n\n"
            "Guidelines: NEVER categorize a quiz/test as routine homework under ## Priorities for Today. "
            "Assessments **due today or overdue** appear exclusively under ## Schedule as the provided ⚠️ indicators. "
            "Under ## Secondary Objectives, you **may** (and should, when applicable) add **preparation** "
            "bullets for assessments within the next 7 days — never misrepresent these as homework to submit. "
            "Use **bold** for assignment names where helpful. Maintain a professional tone throughout. No introductory paragraph. Deliver these four sections only."
        ) % (
            name, now_str, schedule_note,
            lines_overdue_work, lines_today_work, quiz_test_block,
            lines_qt_study,
            lines_good_time, lines_big,
            events_text, tasks_text, stale_text,
        )

        try:
            client = anthropic.Anthropic(api_key=api_key)
            message = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=900,
                messages=[{"role": "user", "content": prompt}]
            )
            track_api_usage(message)
            content = message.content[0].text if message.content else "Have a great day!"
        except Exception as e:
            log.error("Anthropic API error: %s", e)
            content = "Could not generate briefing. Check your API key in Settings."

        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
INSERT INTO briefing_cache (id, generated_at, content) VALUES (1, NOW(), %s)
ON CONFLICT (id) DO UPDATE SET generated_at = NOW(), content = EXCLUDED.content""", (content,))
        conn.commit()
        cur.close()
        conn.close()


scheduler = BackgroundScheduler(timezone=TZ)


def generate_evening_debrief():
    """Generate a 7 PM evening debrief summarizing the day."""
    with _briefing_lock:
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            log.warning("Evening debrief: ANTHROPIC_API_KEY not set")
            return
        cfg = get_config()
        name = cfg.get("name", "Jarvis")
        conn = get_db()
        cur = conn.cursor()
        today_start = datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
        cur.execute("""
SELECT assignment_title, class_name, duration_minutes, timed
FROM completions WHERE completed_at >= %s ORDER BY completed_at DESC""", (today_start,))
        done_today = [dict(r) for r in cur.fetchall()]
        cur.execute("SELECT title, urgency FROM tasks WHERE completed = FALSE ORDER BY urgency DESC LIMIT 10")
        pending_tasks = [dict(r) for r in cur.fetchall()]
        cur.close()
        conn.close()

        # Calculate productivity metrics
        total_minutes = sum(d["duration_minutes"] for d in done_today)
        total_hours = total_minutes / 60.0
        item_count = len(done_today)

        # Build time breakdown by class
        class_time = {}
        for item in done_today:
            class_name = item["class_name"]
            duration = item["duration_minutes"]
            class_time[class_name] = class_time.get(class_name, 0) + duration

        # Build formatted text sections
        done_text = "\n".join(["- %s (%s) — %.0f min" % (d["assignment_title"], d["class_name"], d["duration_minutes"]) for d in done_today]) or "Nothing completed today."

        # Time breakdown by class
        time_breakdown = "\n".join(["- %s: %.1f hours" % (cls, mins/60.0) for cls, mins in sorted(class_time.items(), key=lambda x: x[1], reverse=True)])

        # Metrics section
        metrics_text = "Items completed: %d | Total time: %.1f hours" % (item_count, total_hours)

        cal = fetch_ical(CANVAS_ICAL_URL)
        remaining_asgn = []
        if cal:
            all_asgn = parse_canvas_assignments(cal)
            done_titles = {d["assignment_title"] for d in done_today}
            remaining_asgn = [a for a in all_asgn if a["title"] not in done_titles]

        remaining_text = "\n".join(["- %s (%s, due %s)" % (a["title"], a["class_name"], a["due_display"]) for a in remaining_asgn[:6]]) or "None."
        tasks_text = "\n".join(["- [%s] %s" % (t["urgency"], t["title"]) for t in pending_tasks]) or "None."
        now_str = datetime.now(TZ).strftime("%A, %B %-d at %-I:%M %p")

        prompt = (
            "You are Jarvis, a distinguished AI assistant providing an evening debrief to a high school student in Park City, Utah.\n"
            "Current time: %s (evening debrief)\n\n"
            "TODAY'S ACCOMPLISHMENTS:\n%s\n\n"
            "PRODUCTIVITY METRICS:\n%s\n\n"
            "TIME BREAKDOWN BY CLASS:\n%s\n\n"
            "STILL DUE (not completed):\n%s\n\n"
            "PENDING TASKS:\n%s\n\n"
            "Deliver a sophisticated evening debrief using ONLY bullet points (commence each with •). Structure as follows:\n"
            "- A concise synthesis of today's accomplishments (reference items and metrics above with analytical perspective)\n"
            "- Remaining obligations requiring attention\n"
            "- Strategic Outlook for Tomorrow (a measured forecast of forthcoming priorities and opportunities)\n\n"
            "Maintain a refined, insightful tone. Offer constructive observations balanced with professional encouragement. "
            "Dispense with introductory pleasantries—proceed directly to substance."
        ) % (name, now_str, done_text, metrics_text, time_breakdown, remaining_text, tasks_text)

        try:
            client = anthropic.Anthropic(api_key=api_key)
            message = client.messages.create(model="claude-sonnet-4-6", max_tokens=600,
                                             messages=[{"role": "user", "content": prompt}])
            track_api_usage(message)
            content = message.content[0].text if message.content else "Good evening!"
        except Exception as e:
            log.error("Evening debrief API error: %s", e)
            return
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
INSERT INTO debrief_cache (id, generated_at, content) VALUES (1, NOW(), %s)
ON CONFLICT (id) DO UPDATE SET generated_at = NOW(), content = EXCLUDED.content""", (content,))
        conn.commit()
        cur.close()
        conn.close()
        log.info("Evening debrief generated.")


def schedule_briefing():
    cfg = get_config()
    t = cfg.get("morning_briefing_time", "07:00")
    try:
        hour, minute = int(t.split(":")[0]), int(t.split(":")[1])
    except Exception:
        hour, minute = 7, 0
    scheduler.remove_all_jobs()
    scheduler.add_job(generate_briefing, "cron", hour=hour, minute=minute,
                      id="morning_briefing", replace_existing=True)
    # Evening debrief at 6:30 PM
    scheduler.add_job(generate_evening_debrief, "cron", hour=18, minute=30,
                      id="evening_debrief", replace_existing=True)
    # Process recurring tasks daily at midnight
    scheduler.add_job(_process_recurring_tasks, "cron", hour=0, minute=0,
                      id="process_recurring_tasks", replace_existing=True)
    log.info("Briefing scheduled for %02d:%02d Mountain", hour, minute)
    log.info("Evening debrief scheduled for 18:30 Mountain")
    log.info("Recurring tasks processor scheduled for 00:00 Mountain")


# ── Security Functions ──────────────────────────────────────────────────────────

_login_lock = threading.Lock()

def get_client_ip():
    """Get validated client IP address. ProxyFix middleware handles reverse proxy headers."""
    ip = request.remote_addr or None
    if ip:
        try:
            ipaddress.ip_address(ip)
            return ip
        except ValueError:
            log.warning(f"Invalid IP format detected from request: {ip}")
    # If IP is invalid/missing, use hash of request context to avoid shared rate limit state
    user_agent = request.headers.get('User-Agent', '')
    origin = request.headers.get('Origin', request.headers.get('Referer', ''))
    context = f"{user_agent}|{origin}|{request.remote_addr}"
    return f"unknown-{hashlib.sha256(context.encode()).hexdigest()[:12]}"

def is_ip_locked(ip_addr):
    """Check if IP is currently locked out."""
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT locked_until FROM login_lockouts WHERE ip_address = %s", (ip_addr,))
        row = cur.fetchone()
        cur.close()
        conn.close()
        if row and row["locked_until"] > datetime.now(TZ):
            return True
    except Exception as e:
        log.warning(f"Error checking lockout status: {e}")
    return False

def record_login_attempt(ip_addr, success):
    """Record login attempt and update lockout status."""
    try:
        with _login_lock:
            conn = get_db()
            cur = conn.cursor()

            cur.execute("""
INSERT INTO login_attempts (ip_address, success, user_agent)
VALUES (%s, %s, %s)""", (ip_addr, success, request.headers.get('User-Agent', '')[:500]))

            if not success:
                cur.execute("""
SELECT COALESCE(failure_count, 0) + 1 as new_count, locked_until
FROM login_lockouts WHERE ip_address = %s""", (ip_addr,))
                row = cur.fetchone()
                new_count = row["new_count"] if row else 1

                if new_count >= 5:
                    lockout_duration = timedelta(minutes=15 * (2 ** (new_count - 5)))
                    locked_until = datetime.now(TZ) + lockout_duration
                    cur.execute("""
INSERT INTO login_lockouts (ip_address, locked_until, failure_count)
VALUES (%s, %s, %s)
ON CONFLICT (ip_address) DO UPDATE SET locked_until = %s, failure_count = %s""",
                        (ip_addr, locked_until, new_count, locked_until, new_count))
                    conn.commit()
                    cur.close()
                    conn.close()
                    return {"locked": True, "minutes_remaining": 15}
            else:
                cur.execute("DELETE FROM login_lockouts WHERE ip_address = %s", (ip_addr,))

            conn.commit()
            cur.close()
            conn.close()
    except Exception as e:
        log.warning(f"Error recording login attempt: {e}")

    return {"locked": False, "minutes_remaining": 0}

def get_lockout_info(ip_addr):
    """Get remaining lockout time for IP."""
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT locked_until FROM login_lockouts WHERE ip_address = %s", (ip_addr,))
        row = cur.fetchone()
        cur.close()
        conn.close()
        if row and row["locked_until"] > datetime.now(TZ):
            remaining = (row["locked_until"] - datetime.now(TZ)).total_seconds() / 60
            return int(remaining) + 1
    except Exception as e:
        log.warning(f"Error getting lockout info: {e}")
    return 0

def is_ip_blocked(ip_addr):
    """Check if IP address is in the blocklist."""
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT id FROM blocked_ips WHERE ip_address = %s", (ip_addr,))
        row = cur.fetchone()
        cur.close()
        conn.close()
        return row is not None
    except Exception as e:
        log.warning(f"Error checking if IP is blocked: {e}")
    return False

def get_blocked_ips():
    """Get list of all blocked IPs."""
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT ip_address, ip_name, blocked_at, reason FROM blocked_ips ORDER BY blocked_at DESC")
        rows = cur.fetchall()
        cur.close()
        conn.close()
        return rows or []
    except Exception as e:
        log.warning(f"Error getting blocked IPs: {e}")
    return []

def block_ip(ip_addr, reason="", ip_name=""):
    """Add IP to blocklist or update existing IP name. Only accepts real IPv4/IPv6 addresses."""
    try:
        ipaddress.ip_address(ip_addr)
    except ValueError:
        log.warning(f"Cannot block IP: {ip_addr} is not a valid IPv4/IPv6 address")
        return False

    try:
        conn = get_db()
        cur = conn.cursor()
        # If IP already exists and only ip_name is being updated, preserve the reason
        if ip_name and not reason:
            cur.execute("""
INSERT INTO blocked_ips (ip_address, ip_name, blocked_by, reason)
VALUES (%s, %s, %s, %s)
ON CONFLICT (ip_address) DO UPDATE SET ip_name = %s""", (ip_addr, ip_name, "admin", reason, ip_name))
        else:
            cur.execute("""
INSERT INTO blocked_ips (ip_address, ip_name, blocked_by, reason)
VALUES (%s, %s, %s, %s)
ON CONFLICT (ip_address) DO UPDATE SET ip_name = %s, reason = %s""", (ip_addr, ip_name, "admin", reason, ip_name, reason))
        conn.commit()
        cur.close()
        conn.close()
        log.info(f"Blocked IP: {ip_addr}")
        return True
    except Exception as e:
        log.warning(f"Error blocking IP: {e}")
    return False

def unblock_ip(ip_addr):
    """Remove IP from blocklist."""
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("DELETE FROM blocked_ips WHERE ip_address = %s", (ip_addr,))
        conn.commit()
        cur.close()
        conn.close()
        log.info(f"Unblocked IP: {ip_addr}")
        return True
    except Exception as e:
        log.warning(f"Error unblocking IP: {e}")
    return False

def track_ip_name(ip_addr, ip_name=""):
    """Track/name an IP for monitoring without blocking it. Uses separate ip_names table."""
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
INSERT INTO ip_names (ip_address, ip_name)
VALUES (%s, %s)
ON CONFLICT (ip_address) DO UPDATE SET ip_name = EXCLUDED.ip_name, tracked_at = NOW()""",
                    (ip_addr, ip_name))
        conn.commit()
        cur.close()
        conn.close()
        log.info(f"Tracked IP name: {ip_addr} -> {ip_name}")
        return True
    except Exception as e:
        log.warning(f"Error tracking IP name: {e}")
    return False

def is_app_locked_down():
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT is_locked_down FROM lockdown_state WHERE id = 1")
        row = cur.fetchone()
        cur.close()
        conn.close()
        return row and row["is_locked_down"]
    except Exception as e:
        log.warning(f"Error checking lockdown state: {e}")
    return False

def activate_lockdown(ip_addr):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
UPDATE lockdown_state SET is_locked_down = TRUE, activated_at = NOW(), activated_by = %s
WHERE id = 1""", (ip_addr,))
        conn.commit()
        cur.close()
        conn.close()
        return True
    except Exception as e:
        log.exception(f"Error activating lockdown: {e}")
    return False

def deactivate_lockdown():
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("UPDATE lockdown_state SET is_locked_down = FALSE WHERE id = 1")
        conn.commit()
        cur.close()
        conn.close()
        return True
    except Exception as e:
        log.exception(f"Error deactivating lockdown: {e}")
    return False

# ── Routes ───────────────────────────────────────────────────────────────────

@app.route("/parent", methods=["GET"])
def parent_portal():
    """Parent portal for creating and monitoring student tasks."""
    if not session.get("parent_authenticated"):
        return redirect("/login")
    return render_template("parent.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        if session.get("authenticated"):
            return redirect("/")
        return render_template("login.html")

    ip_addr = get_client_ip()

    if is_ip_locked(ip_addr):
        remaining_mins = get_lockout_info(ip_addr)
        return jsonify({
            "error": f"Too many failed attempts. Try again in {remaining_mins} minute(s).",
            "lockout": True,
            "minutes_remaining": remaining_mins
        }), 429

    data = request.get_json(force=True) or {}
    username = data.get("username", "").strip()
    password = data.get("password")
    security_code = data.get("security_code")
    is_locked_down = is_app_locked_down()
    ip_is_blocked = is_ip_blocked(ip_addr)

    if not username:
        return jsonify({"error": "Username required"}), 400

    if ip_is_blocked and not security_code:
        return jsonify({
            "error": "This IP address is blocked. Please provide security code to access.",
            "ip_blocked": True,
            "message": "System requires security code for this IP"
        }), 202

    if password and not security_code:
        is_admin = username == ADMIN_USER
        is_parent = username == PARENT_USER

        if is_admin:
            expected_password = ADMIN_PASSWORD
        elif is_parent:
            expected_password = PARENT_PASSWORD
        else:
            expected_password = APP_PASSWORD

        if password.strip() == expected_password:
            if is_locked_down:
                return jsonify({
                    "is_locked_down": True,
                    "message": "System in lockdown. Please provide security code."
                }), 202

            record_login_attempt(ip_addr, True)
            session.permanent = True
            if is_admin:
                session["admin_authenticated"] = True
            elif is_parent:
                session["parent_authenticated"] = True
            else:
                session["authenticated"] = True
            session.modified = True

            if is_admin:
                redirect_url = "/admin"
            elif is_parent:
                redirect_url = "/parent"
            else:
                redirect_url = "/"
            return jsonify({"status": "ok", "redirect": redirect_url})
        else:
            lockout_info = record_login_attempt(ip_addr, False)
            if lockout_info["locked"]:
                return jsonify({
                    "error": f"Too many failed attempts. Locked for {lockout_info['minutes_remaining']} minute(s).",
                    "lockout": True,
                    "minutes_remaining": lockout_info["minutes_remaining"]
                }), 429
            return jsonify({"error": "Invalid username or password"}), 401

    if password and security_code:
        security_code_env = os.environ.get("SECURITY_CODE", "").strip()
        if not security_code_env:
            log.error("SECURITY_CODE environment variable not set. Cannot process security code.")
            return jsonify({"error": "Security code not configured"}), 500

        is_admin = username == ADMIN_USER
        is_parent = username == PARENT_USER

        if is_admin:
            expected_password = ADMIN_PASSWORD
        elif is_parent:
            expected_password = PARENT_PASSWORD
        else:
            expected_password = APP_PASSWORD

        # Allow login with security code if:
        # 1. System is in lockdown, OR
        # 2. IP is blocked
        if is_locked_down or ip_is_blocked:
            sc_hash = hashlib.sha256(security_code.strip().encode()).hexdigest()[:8]
            env_hash = hashlib.sha256(security_code_env.encode()).hexdigest()[:8]
            log.warning(f"Login security code attempt: username={username}, is_admin={is_admin}, is_parent={is_parent}, ip_blocked={ip_is_blocked}, locked_down={is_locked_down}, received_hash={sc_hash}, env_hash={env_hash}")
            if password.strip() == expected_password and security_code.strip() == security_code_env:
                record_login_attempt(ip_addr, True)
                session.permanent = True
                if is_admin:
                    session["admin_authenticated"] = True
                elif is_parent:
                    session["parent_authenticated"] = True
                else:
                    session["authenticated"] = True
                session.modified = True

                if is_admin:
                    redirect_url = "/admin"
                elif is_parent:
                    redirect_url = "/parent"
                else:
                    redirect_url = "/"
                return jsonify({"status": "ok", "redirect": redirect_url})
            else:
                lockout_info = record_login_attempt(ip_addr, False)
                if lockout_info["locked"]:
                    return jsonify({
                        "error": f"Too many failed attempts. Locked for {lockout_info['minutes_remaining']} minute(s).",
                        "lockout": True,
                        "minutes_remaining": lockout_info["minutes_remaining"]
                    }), 429
                return jsonify({"error": "Invalid username, password, or security code"}), 401
        else:
            return jsonify({"error": "Security code not required"}), 400

    return jsonify({"error": "Missing username or password"}), 400


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")


@app.route("/admin", methods=["GET", "POST"])
def admin():
    if request.method == "GET":
        if session.get("admin_authenticated"):
            return render_template("admin.html")
        return render_template("admin_login.html")

    ip_addr = get_client_ip()
    data = request.get_json(force=True) or {}
    log.info(f"Admin login attempt from {ip_addr}, received data keys: {list(data.keys())}")

    if is_ip_locked(ip_addr):
        remaining_mins = get_lockout_info(ip_addr)
        return jsonify({
            "error": f"Too many failed attempts. Try again in {remaining_mins} minute(s).",
            "lockout": True
        }), 429

    password = data.get("password")
    security_code = data.get("security_code")
    is_locked_down = is_app_locked_down()

    log.info(f"Admin login: password={bool(password)}, security_code={bool(security_code)}, locked_down={is_locked_down}")

    if password and not security_code:
        pwd_hash = hashlib.sha256(password.encode()).hexdigest()[:8]
        admin_hash = hashlib.sha256(ADMIN_PASSWORD.encode()).hexdigest()[:8]
        log.info(f"Login attempt: password_hash={pwd_hash}, admin_hash={admin_hash}, match={password.strip() == ADMIN_PASSWORD}")

        # Check if admin password
        if password.strip() == ADMIN_PASSWORD:
            if is_locked_down:
                return jsonify({
                    "is_locked_down": True,
                    "message": "System in lockdown. Please provide security code."
                }), 202

            record_login_attempt(ip_addr, True)
            session.permanent = True
            session["admin_authenticated"] = True
            session.modified = True
            return jsonify({"status": "ok", "redirect": "/admin"})

        # Check if app password
        if password.strip() == APP_PASSWORD:
            if is_locked_down:
                return jsonify({
                    "is_locked_down": True,
                    "message": "System in lockdown. Please provide security code."
                }), 202

            record_login_attempt(ip_addr, True)
            session.permanent = True
            session["authenticated"] = True
            session.modified = True
            return jsonify({"status": "ok", "redirect": "/"})

        # Neither password matched
        lockout_info = record_login_attempt(ip_addr, False)
        if lockout_info["locked"]:
            return jsonify({
                "error": f"Too many failed attempts. Locked for {lockout_info['minutes_remaining']} minute(s).",
                "lockout": True,
                "minutes_remaining": lockout_info["minutes_remaining"]
            }), 429
        return jsonify({"error": "Wrong password"}), 401

    # Handle security code for both app and admin
    if password and security_code:
        if is_locked_down:
            security_code_env = os.environ.get("SECURITY_CODE", "").strip()
            if not security_code_env:
                log.error("SECURITY_CODE environment variable not set. Cannot process security code.")
                return jsonify({"error": "Security code not configured"}), 500

            sc_hash = hashlib.sha256(security_code.strip().encode()).hexdigest()[:8]
            env_hash = hashlib.sha256(security_code_env.encode()).hexdigest()[:8]
            log.warning(f"Security code attempt: received_hash={sc_hash}, env_hash={env_hash}, pwd_match={password.strip() == ADMIN_PASSWORD or password.strip() == APP_PASSWORD}")

            # Check admin password with security code
            if password.strip() == ADMIN_PASSWORD and security_code.strip() == security_code_env:
                record_login_attempt(ip_addr, True)
                session.permanent = True
                session["admin_authenticated"] = True
                session.modified = True
                return jsonify({"status": "ok", "redirect": "/admin"})

            # Check app password with security code
            if password.strip() == APP_PASSWORD and security_code.strip() == security_code_env:
                record_login_attempt(ip_addr, True)
                session.permanent = True
                session["authenticated"] = True
                session.modified = True
                return jsonify({"status": "ok", "redirect": "/"})

            # Neither matched
            lockout_info = record_login_attempt(ip_addr, False)
            if lockout_info["locked"]:
                return jsonify({
                    "error": f"Too many failed attempts. Locked for {lockout_info['minutes_remaining']} minute(s).",
                    "lockout": True,
                    "minutes_remaining": lockout_info["minutes_remaining"]
                }), 429
            return jsonify({"error": "Wrong password or security code"}), 401
        else:
            return jsonify({"error": "Security code not required"}), 400

    return jsonify({"error": "Missing password"}), 400


@app.route("/api/admin/login-attempts")
def api_admin_login_attempts():
    if not session.get("admin_authenticated"):
        return jsonify({"error": "Not authenticated"}), 401

    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
SELECT la.ip_address, la.success, la.attempted_at, la.user_agent, COALESCE(iname.ip_name, bi.ip_name, '') as ip_name
FROM login_attempts la
LEFT JOIN ip_names iname ON la.ip_address = iname.ip_address
LEFT JOIN blocked_ips bi ON la.ip_address = bi.ip_address
ORDER BY la.attempted_at DESC LIMIT 200""")
        rows = [dict(r) for r in cur.fetchall()]
        cur.close()
        conn.close()

        for r in rows:
            r["attempted_at"] = r["attempted_at"].isoformat() if r["attempted_at"] else None

        return jsonify({"attempts": rows})
    except Exception as e:
        log.exception("Error fetching login attempts")
        return jsonify({"error": str(e)}), 500


@app.route("/api/admin/suspicious-activity")
def api_admin_suspicious_activity():
    if not session.get("admin_authenticated"):
        return jsonify({"error": "Not authenticated"}), 401

    try:
        conn = get_db()
        cur = conn.cursor()

        cur.execute("""
SELECT ip_address, COUNT(*) as failure_count, MAX(attempted_at) as last_attempt
FROM login_attempts WHERE success = FALSE AND attempted_at > NOW() - INTERVAL '24 hours'
GROUP BY ip_address ORDER BY failure_count DESC""")
        suspicious_ips = [dict(r) for r in cur.fetchall()]

        cur.execute("""
SELECT ip_address, locked_until, failure_count, created_at
FROM login_lockouts ORDER BY created_at DESC LIMIT 50""")
        lockouts = [dict(r) for r in cur.fetchall()]

        cur.close()
        conn.close()

        for ip in suspicious_ips:
            ip["last_attempt"] = ip["last_attempt"].isoformat() if ip["last_attempt"] else None

        for lo in lockouts:
            lo["locked_until"] = lo["locked_until"].isoformat() if lo["locked_until"] else None
            lo["created_at"] = lo["created_at"].isoformat() if lo["created_at"] else None

        return jsonify({
            "suspicious_ips": suspicious_ips,
            "active_lockouts": lockouts
        })
    except Exception as e:
        log.exception("Error fetching suspicious activity")
        return jsonify({"error": str(e)}), 500


@app.route("/api/admin/claude-usage")
def api_admin_claude_usage():
    if not session.get("admin_authenticated"):
        return jsonify({"error": "Not authenticated"}), 401

    try:
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            return jsonify({
                "tokens_used": 0,
                "tokens_limit": 1000000,
                "percent_used": 0,
                "status": "No API key configured"
            })

        global _api_usage_cache
        _api_usage_cache["last_updated"] = datetime.now(TZ)

        tokens_used = _api_usage_cache.get("tokens_used", 0)
        tokens_limit = _api_usage_cache.get("tokens_limit", 1000000)
        percent_used = round((tokens_used / tokens_limit * 100), 2) if tokens_limit > 0 else 0

        return jsonify({
            "tokens_used": tokens_used,
            "tokens_limit": tokens_limit,
            "tokens_remaining": tokens_limit - tokens_used,
            "percent_used": percent_used,
            "percent_remaining": 100 - percent_used,
            "last_updated": _api_usage_cache["last_updated"].isoformat(),
            "note": "Usage tracking requires integration with actual API calls in the application"
        })
    except Exception as e:
        log.exception("Error fetching Claude usage")
        return jsonify({"error": str(e), "tokens_used": 0, "tokens_limit": 1000000}), 500


@app.route("/api/lockdown-status")
def api_lockdown_status():
    is_locked = is_app_locked_down()
    return jsonify({"is_locked_down": is_locked})


def is_localhost():
    """Check if request is from localhost (127.0.0.1 or ::1)"""
    ip = get_client_ip()
    return ip in ('127.0.0.1', '::1', 'localhost')


@app.route("/api/test-admin-password")
def api_test_admin_password():
    """Debug endpoint - localhost only - shows if ADMIN_PASSWORD is set"""
    if not is_localhost():
        return jsonify({"error": "Debug endpoints only available on localhost"}), 403

    if ADMIN_PASSWORD == "admin-change-me":
        return jsonify({"status": "USING_DEFAULT", "message": "ADMIN_PASSWORD not set in environment, using default"})
    else:
        return jsonify({"status": "SET_FROM_ENV", "length": len(ADMIN_PASSWORD), "message": "ADMIN_PASSWORD is set from environment variable"})


@app.route("/api/test-security-code")
def api_test_security_code():
    """Debug endpoint - localhost only - shows if SECURITY_CODE is set"""
    if not is_localhost():
        return jsonify({"error": "Debug endpoints only available on localhost"}), 403

    security_code = os.environ.get("SECURITY_CODE", "")
    if not security_code:
        return jsonify({"status": "NOT_SET", "message": "SECURITY_CODE environment variable not set"})
    else:
        return jsonify({"status": "SET_FROM_ENV", "length": len(security_code), "message": "SECURITY_CODE is set from environment variable"})


@app.route("/api/test-lockdown-status")
def api_test_lockdown_status():
    """Debug endpoint - localhost only - shows current lockdown state"""
    if not is_localhost():
        return jsonify({"error": "Debug endpoints only available on localhost"}), 403

    is_locked = is_app_locked_down()
    return jsonify({"is_locked_down": is_locked, "message": f"System is {'LOCKED DOWN' if is_locked else 'NORMAL'}"})



@app.route("/api/admin/lockdown", methods=["POST"])
def api_admin_lockdown():
    if not session.get("admin_authenticated"):
        return jsonify({"error": "Not authenticated"}), 401

    try:
        ip_addr = get_client_ip()
        current_state = is_app_locked_down()

        if current_state:
            deactivate_lockdown()
            new_state = False
        else:
            activate_lockdown(ip_addr)
            new_state = True

        return jsonify({
            "is_locked_down": new_state,
            "message": "Lockdown activated" if new_state else "Lockdown deactivated"
        })
    except Exception as e:
        log.exception("Error toggling lockdown")
        return jsonify({"error": str(e)}), 500


@app.route("/api/admin/blocked-ips")
def api_admin_blocked_ips():
    if not session.get("admin_authenticated"):
        return jsonify({"error": "Not authenticated"}), 401

    try:
        blocked = get_blocked_ips()
        return jsonify({
            "blocked_ips": [{"ip": row["ip_address"], "name": row["ip_name"], "blocked_at": row["blocked_at"].isoformat() if row["blocked_at"] else None, "reason": row["reason"]} for row in blocked]
        })
    except Exception as e:
        log.exception("Error getting blocked IPs")
        return jsonify({"error": str(e)}), 500


@app.route("/api/admin/block-ip", methods=["POST"])
def api_admin_block_ip():
    if not session.get("admin_authenticated"):
        return jsonify({"error": "Not authenticated"}), 401

    try:
        data = request.get_json(force=True) or {}
        ip_addr = data.get("ip_address", "").strip()
        reason = data.get("reason", "").strip()
        ip_name = data.get("ip_name", "").strip()

        if not ip_addr:
            return jsonify({"error": "IP address required"}), 400

        # Validate IP format before attempting to block
        try:
            ipaddress.ip_address(ip_addr)
        except ValueError:
            return jsonify({"error": f"Invalid IP address: {ip_addr}. Must be a valid IPv4 or IPv6 address. Cannot block temporary/hashed IP addresses."}), 400

        if block_ip(ip_addr, reason, ip_name):
            return jsonify({"status": "blocked", "ip": ip_addr})
        else:
            return jsonify({"error": "Failed to block IP"}), 500
    except Exception as e:
        log.exception("Error blocking IP")
        return jsonify({"error": str(e)}), 500


@app.route("/api/admin/unblock-ip", methods=["POST"])
def api_admin_unblock_ip():
    if not session.get("admin_authenticated"):
        return jsonify({"error": "Not authenticated"}), 401

    try:
        data = request.get_json(force=True) or {}
        ip_addr = data.get("ip_address", "").strip()

        if not ip_addr:
            return jsonify({"error": "IP address required"}), 400

        if unblock_ip(ip_addr):
            return jsonify({"status": "unblocked", "ip": ip_addr})
        else:
            return jsonify({"error": "Failed to unblock IP"}), 500
    except Exception as e:
        log.exception("Error unblocking IP")
        return jsonify({"error": str(e)}), 500


@app.route("/api/admin/track-ip-name", methods=["POST"])
def api_admin_track_ip_name():
    """Track/name an IP for monitoring without blocking it."""
    if not session.get("admin_authenticated"):
        return jsonify({"error": "Not authenticated"}), 401

    try:
        data = request.get_json(force=True) or {}
        ip_addr = data.get("ip_address", "").strip()
        ip_name = data.get("ip_name", "").strip()

        if not ip_addr:
            return jsonify({"error": "IP address required"}), 400

        if track_ip_name(ip_addr, ip_name):
            return jsonify({"status": "tracked", "ip": ip_addr, "name": ip_name})
        else:
            return jsonify({"error": "Failed to track IP name"}), 500
    except Exception as e:
        log.exception("Error tracking IP name")
        return jsonify({"error": str(e)}), 500


@app.route("/api/csrf-token")
def api_csrf_token():
    """Get CSRF token for form submissions"""
    import secrets
    if 'csrf_token' not in session:
        session['csrf_token'] = secrets.token_hex(32)
    return jsonify({"csrf_token": session.get('csrf_token')})


@app.route("/")
def index():
    return render_template("index.html", tz=str(get_tz()))


@app.route("/api/assignments")
def api_assignments():
    start = time.time()
    try:
        t1 = time.time()
        cal = fetch_ical(CANVAS_ICAL_URL)
        log.info(f"/api/assignments: fetch_ical took {time.time()-t1:.2f}s")
        if cal is None:
            return jsonify({"assignments": [], "error": "Failed to fetch Canvas calendar."})
        t2 = time.time()
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT assignment_title FROM completions")
        completed_titles = set(r["assignment_title"] for r in cur.fetchall())
        cur.execute("SELECT uid, minutes FROM assignment_estimates")
        custom_estimates = {r["uid"]: r["minutes"] for r in cur.fetchall()}
        cur.close()
        conn.close()
        log.info(f"/api/assignments: db query took {time.time()-t2:.2f}s")
        t3 = time.time()
        assignments = parse_canvas_assignments(cal)
        result = []
        for a in assignments:
            if a["title"] in completed_titles:
                continue
            uid = a.get("uid", "")
            if uid in custom_estimates:
                a["estimate_minutes"] = custom_estimates[uid]
                a["estimate_custom"] = True
            else:
                a["estimate_minutes"] = estimate_assignment(a["title"], a["class_name"])
                a["estimate_custom"] = False
            result.append(a)
        log.info(f"/api/assignments: estimate took {time.time()-t3:.2f}s for {len(result)} assignments")
        cfg = get_config()
        log.info(f"/api/assignments: total took {time.time()-start:.2f}s")
        return jsonify({"assignments": result, "timezone": cfg.get("timezone", "America/Denver")})
    except Exception as e:
        log.exception(f"/api/assignments failed after {time.time()-start:.2f}s: {e}")
        return jsonify({"assignments": [], "error": "Internal server error fetching assignments."}), 500


@app.route("/api/assignments/<uid>/estimate", methods=["POST"])
def api_set_estimate(uid):
    data = request.get_json(force=True) or {}
    try:
        minutes = float(data.get("minutes", 30))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid minutes value"}), 400
    minutes = max(1.0, min(minutes, 600.0))
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
INSERT INTO assignment_estimates (uid, minutes, updated_at)
VALUES (%s, %s, NOW())
ON CONFLICT (uid) DO UPDATE SET minutes = EXCLUDED.minutes, updated_at = NOW()
""", (uid, minutes))
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({"status": "ok", "minutes": minutes})


@app.route("/api/calendar")
def api_calendar():
    start = time.time()
    days = int(request.args.get("days", 30))
    events = []
    today = datetime.now(TZ).date()

    def fetch_source(name, url, parser):
        """Helper to fetch one source with timeout protection."""
        if not url:
            return []
        try:
            t = time.time()
            cal = fetch_ical(url)
            elapsed = time.time() - t
            if elapsed > 8:
                log.warning(f"/api/calendar: {name} fetch took {elapsed:.2f}s (slow)")
            else:
                log.info(f"/api/calendar: {name} took {elapsed:.2f}s")
            if not cal:
                return []
            return parser(cal, days)
        except Exception as e:
            log.warning(f"/api/calendar: {name} failed: {e}")
            return []

    # Parse results from fetch_source to build events list
    try:
        personal_events = fetch_source("personal", PERSONAL_ICAL_URL,
                                      lambda cal, d: [dict(e, source="personal") for e in parse_calendar_events(cal, days_ahead=d)])
        events.extend(personal_events)
    except Exception as e:
        log.warning(f"/api/calendar: personal parse failed: {e}")

    try:
        sports_events = fetch_source("sports", SPORTS_ICAL_URL,
                                    lambda cal, d: [dict(e, source="sports") for e in parse_calendar_events(cal, days_ahead=d)])
        events.extend(sports_events)
    except Exception as e:
        log.warning(f"/api/calendar: sports parse failed: {e}")

    try:
        if CANVAS_ICAL_URL:
            t = time.time()
            cal = fetch_ical(CANVAS_ICAL_URL)
            elapsed = time.time() - t
            if elapsed > 8:
                log.warning(f"/api/calendar: canvas fetch took {elapsed:.2f}s (slow)")
            else:
                log.info(f"/api/calendar: canvas took {elapsed:.2f}s")
            if cal:
                for a in parse_canvas_assignments(cal):
                    events.append({
                        "title": a["title"],
                        "start_display": a["due_display"],
                        "end_display": "",
                        "start_iso": a["due_iso"],
                        "date": a["due_iso"][:10],
                        "all_day": False,
                        "source": "canvas",
                        "urgency": a["urgency"],
                        "class_name": a["class_name"]
                    })
    except Exception as e:
        log.warning(f"/api/calendar: canvas failed: {e}")

    try:
        day_events = fetch_day_calendar_events(today, days_ahead=days)
        events.extend(day_events)
        log.info(f"/api/calendar: day calendar added {len(day_events)} events")
    except Exception as e:
        log.warning(f"/api/calendar: day calendar failed: {e}")

    events.sort(key=lambda x: x.get("start_iso", ""))
    log.info(f"/api/calendar: total took {time.time()-start:.2f}s with {len(events)} events")
    return jsonify({"events": events})


@app.route("/api/diagnostic")
def api_diagnostic():
    """Diagnostic endpoint to check if debrief can be generated."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    has_db = True
    has_debrief = False
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT content FROM debrief_cache WHERE id = 1")
        row = cur.fetchone()
        has_debrief = bool(row and row["content"])
        cur.close()
        conn.close()
    except Exception as e:
        has_db = False

    return jsonify({
        "api_key_set": bool(api_key),
        "database_connected": has_db,
        "debrief_generated": has_debrief,
        "current_time": datetime.now(TZ).isoformat()
    })

@app.route("/api/briefing")
def api_briefing():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT content, generated_at FROM briefing_cache WHERE id = 1")
    row = cur.fetchone()
    cur.execute("SELECT content, generated_at FROM debrief_cache WHERE id = 1")
    row_d = cur.fetchone()
    cur.close()
    conn.close()
    debrief = ""
    debrief_at = None
    if row_d:
        debrief = row_d["content"] or ""
        if row_d["generated_at"]:
            debrief_at = row_d["generated_at"].isoformat()
    if row and row["content"]:
        return jsonify({
            "briefing": row["content"],
            "generated_at": row["generated_at"].isoformat() if row["generated_at"] else None,
            "debrief": debrief,
            "debrief_generated_at": debrief_at,
        })
    return jsonify({
        "briefing": "Generating your briefing...",
        "generated_at": None,
        "debrief": debrief,
        "debrief_generated_at": debrief_at,
    })


@app.route("/api/briefing/refresh", methods=["POST"])
def api_briefing_refresh():
    threading.Thread(target=generate_briefing, kwargs={"force": True}, daemon=True).start()
    return jsonify({"status": "refreshing"})

@app.route("/api/debrief/generate", methods=["GET", "POST"])
def api_debrief_generate():
    """Manual trigger to generate debrief."""
    threading.Thread(target=generate_evening_debrief, daemon=True).start()
    return jsonify({"status": "generating", "message": "Debrief generation started"})


def _workout_history_block(cur):
    cur.execute("""
SELECT created_at, focus_label, intensity, location, user_notes, perceived_difficulty
FROM workout_logs ORDER BY created_at DESC LIMIT 12""")
    rows = cur.fetchall()
    if not rows:
        return "No prior logged workouts yet — this is their first tracked session."
    lines = []
    for r in reversed(rows):
        ts = r["created_at"].strftime("%Y-%m-%d") if r["created_at"] else ""
        felt = ""
        if r.get("perceived_difficulty") is not None:
            felt = " | after: felt %s/10" % r["perceived_difficulty"]
        notes = (r.get("user_notes") or "").strip()
        note_part = (" | athlete note: " + notes[:180]) if notes else ""
        loc = "home gym" if r["location"] == "home" else "rec gym"
        lines.append(
            "- %s: %s | intensity %s/10 | %s%s%s"
            % (ts, r["focus_label"], r["intensity"], loc, felt, note_part)
        )
    return "\n".join(lines)


@app.route("/api/workout", methods=["GET"])
def api_workout_get():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT last_focus_index FROM workout_state WHERE id = 1")
    row = cur.fetchone()
    if not row:
        cur.execute("INSERT INTO workout_state (id, last_focus_index) VALUES (1, -1)")
        conn.commit()
        last_i = -1
    else:
        last_i = int(row["last_focus_index"])
    n = len(WORKOUT_FOCUS_CYCLE)
    next_i = (last_i + 1) % n
    key, label = WORKOUT_FOCUS_CYCLE[next_i]
    cur.execute("""
SELECT id, created_at, focus_label, intensity, location,
       LEFT(plan_content, 160) as preview, user_notes, perceived_difficulty
FROM workout_logs ORDER BY created_at DESC LIMIT 20""")
    logs = []
    for r in cur.fetchall():
        logs.append({
            "id": r["id"],
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            "focus_label": r["focus_label"],
            "intensity": r["intensity"],
            "location": r["location"],
            "preview": (r["preview"] or "").strip(),
            "user_notes": r.get("user_notes") or "",
            "perceived_difficulty": r["perceived_difficulty"],
        })
    cur.close()
    conn.close()
    return jsonify({
        "next_focus_index": next_i,
        "next_focus_key": key,
        "next_focus_label": label,
        "rotation": [{"key": a[0], "label": a[1]} for a in WORKOUT_FOCUS_CYCLE],
        "recent_logs": logs,
    })


@app.route("/api/workout/generate", methods=["POST"])
def api_workout_generate():
    data = request.get_json(force=True) or {}
    try:
        intensity = int(data.get("intensity", 5))
    except (TypeError, ValueError):
        intensity = 5
    intensity = max(1, min(10, intensity))
    location = str(data.get("location", "home")).strip().lower()
    if location not in ("home", "rec"):
        location = "home"

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return jsonify({"error": "Add your Anthropic API key in Railway environment to generate workouts."}), 500

    with _workout_lock:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT last_focus_index FROM workout_state WHERE id = 1 FOR UPDATE")
        row = cur.fetchone()
        if not row:
            cur.execute(
                "INSERT INTO workout_state (id, last_focus_index) VALUES (1, -1) ON CONFLICT (id) DO NOTHING"
            )
            cur.execute("SELECT last_focus_index FROM workout_state WHERE id = 1 FOR UPDATE")
            row = cur.fetchone()
        last_i = int(row["last_focus_index"])
        next_i = (last_i + 1) % len(WORKOUT_FOCUS_CYCLE)
        focus_key, focus_label = WORKOUT_FOCUS_CYCLE[next_i]
        history_text = _workout_history_block(cur)

        now_local = datetime.now(TZ)
        name = get_config().get("name", "Jarvis")
        if location == "home":
            equip = (
                "HOME GYM: dumbbells only up to 35 lb each, plus bodyweight. "
                "No barbell rack, no heavy machines, no cable stack unless you describe a bodyweight or DB substitute. "
                "Be creative with unilateral work, tempo, and density."
            )
        else:
            equip = (
                "REC GYM (full gym): barbells, squat rack, cables, machines, dumbbells beyond 35 lb, all standard equipment."
            )

        user_prompt = (
            "Athlete name: %s.\n"
            "Today: %s.\n"
            "Today's rotation focus (must be the primary emphasis of this session): **%s**.\n"
            "Target difficulty: **%d / 10** (1 = very easy recovery, 5 = moderate, 8–10 = very demanding).\n"
            "Training location: **%s**.\n\n"
            "Equipment rules: %s\n\n"
            "Recent history — learn from these (honor notes, vary exercises if they repeat complaints, match intensity trends):\n%s\n\n"
            "Write ONE complete workout for today. Include warm-up, main lifts/accessories appropriate to the focus, "
            "optional finisher if intensity ≥ 6, and cool-down. Use **bold** for exercise names. "
            "Give sets, reps or time, rest, and one short form cue per main movement. "
            "Do not skip the rotation focus — secondary work should support it."
        ) % (
            name,
            now_local.strftime("%A, %B %-d, %Y"),
            focus_label,
            intensity,
            "Home gym (≤35 lb dumbbells + bodyweight)" if location == "home" else "Rec / full gym",
            equip,
            history_text,
        )

        try:
            client = anthropic.Anthropic(api_key=api_key)
            message = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=2000,
                messages=[{"role": "user", "content": user_prompt}],
                system=(
                    "You are Jarvis, a distinguished and methodical strength and conditioning specialist attending to a diligent high school athlete. "
                    "Your programs are meticulously tailored to the available equipment and the athlete's demonstrated capacity. "
                    "Safety remains paramount—you will never prescribe reckless or excessive loading. "
                    "Should the training history indicate previous injury, fatigue, or overexertion, you shall adjust proactively and with sophistication. "
                    "Your recommendations reflect professional precision and unwavering attention to sustainable progression."
                ),
            )
            track_api_usage(message)
            content = message.content[0].text if message.content else ""
        except Exception as e:
            log.error("Workout generate API error: %s", e)
            cur.close()
            conn.close()
            return jsonify({"error": "Could not generate workout. Check API key and try again."}), 500

        cur.execute("""
INSERT INTO workout_logs (focus_key, focus_label, intensity, location, plan_content)
VALUES (%s, %s, %s, %s, %s) RETURNING id""",
                    (focus_key, focus_label, intensity, location, content))
        log_id = cur.fetchone()["id"]
        cur.execute("UPDATE workout_state SET last_focus_index=%s WHERE id=1", (next_i,))
        conn.commit()
        cur.close()
        conn.close()

    return jsonify({
        "plan": content,
        "log_id": log_id,
        "focus_key": focus_key,
        "focus_label": focus_label,
        "intensity": intensity,
        "location": location,
    })


@app.route("/api/workout/log/<int:log_id>", methods=["PATCH"])
def api_workout_log_patch(log_id):
    data = request.get_json(force=True) or {}
    if "user_notes" not in data and "perceived_difficulty" not in data:
        return jsonify({"error": "Nothing to update"}), 400
    conn = get_db()
    cur = conn.cursor()
    if "user_notes" in data:
        cur.execute(
            "UPDATE workout_logs SET user_notes=%s WHERE id=%s",
            (str(data.get("user_notes") or "")[:2000], log_id),
        )
    if "perceived_difficulty" in data:
        pd_raw = data.get("perceived_difficulty")
        if pd_raw is None:
            cur.execute("UPDATE workout_logs SET perceived_difficulty=NULL WHERE id=%s", (log_id,))
        else:
            try:
                pd = max(1, min(10, int(pd_raw)))
                cur.execute(
                    "UPDATE workout_logs SET perceived_difficulty=%s WHERE id=%s",
                    (pd, log_id),
                )
            except (TypeError, ValueError):
                pass
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({"status": "ok"})


@app.route("/api/workout/log-custom", methods=["POST"])
def api_workout_log_custom():
    data = request.get_json(force=True) or {}
    user_description = str(data.get("description", "")).strip()
    if not user_description:
        return jsonify({"error": "Workout description required"}), 400

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return jsonify({"error": "Add your Anthropic API key in Railway environment to log workouts."}), 500

    try:
        client = anthropic.Anthropic(api_key=api_key)
        categorize_prompt = (
            "The athlete has documented the following training session:\n\n"
            '"%s"\n\n'
            "Provide a sophisticated analysis and organize this workout appropriately. Your response should be structured markdown with:\n"
            "1. A concise executive summary of the training session (1-2 lines, analytical tone)\n"
            "2. Exercises executed (enumerate them with sets, repetitions, and technical details as provided)\n"
            "3. Intensity assessment (1-10 scale derived from the session description and effort indicators)\n"
            "4. Primary training stimulus (e.g., Back, Legs, Biceps & Triceps, Core / Cardio, Shoulders, or Other)\n\n"
            "Structure your response with ## headers for each section. Maintain a professional, encouraging tone that acknowledges the athlete's effort."
        ) % user_description

        message = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1000,
            messages=[{"role": "user", "content": categorize_prompt}],
        )
        track_api_usage(message)
        formatted_content = message.content[0].text if message.content else ""
    except Exception as e:
        log.error("Workout log custom API error: %s", e)
        return jsonify({"error": "Could not categorize workout. Try again."}), 500

    # Extract focus from the formatted response (simple heuristic)
    response_lower = formatted_content.lower()
    focus_key = "other"
    focus_label = "Other"
    if "back" in response_lower:
        focus_key, focus_label = "back", "Back"
    elif "biceps" in response_lower or "triceps" in response_lower:
        focus_key, focus_label = "biceps_triceps", "Biceps & Triceps"
    elif "core" in response_lower or "cardio" in response_lower:
        focus_key, focus_label = "core_cardio", "Core / Cardio"
    elif "leg" in response_lower:
        focus_key, focus_label = "legs", "Legs"
    elif "shoulder" in response_lower:
        focus_key, focus_label = "shoulders", "Shoulders"

    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
INSERT INTO workout_logs (focus_key, focus_label, intensity, location, plan_content, user_notes)
VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
                ("custom", focus_label, 5, "custom", formatted_content, user_description))
    log_id = cur.fetchone()["id"]
    conn.commit()
    cur.close()
    conn.close()

    return jsonify({
        "plan": formatted_content,
        "log_id": log_id,
        "focus_label": focus_label,
        "status": "logged"
    })


@app.route("/api/workout/regenerate", methods=["POST"])
def api_workout_regenerate():
    data = request.get_json(force=True) or {}
    log_id = int(data.get("log_id", 0))
    if log_id <= 0:
        return jsonify({"error": "log_id required"}), 400

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return jsonify({"error": "Add your Anthropic API key in Railway environment to regenerate workouts."}), 500

    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT focus_key, focus_label, intensity, location, user_notes FROM workout_logs WHERE id=%s", (log_id,))
    row = cur.fetchone()
    if not row:
        cur.close()
        conn.close()
        return jsonify({"error": "Workout not found"}), 404

    focus_key, focus_label, intensity, location, user_notes = row["focus_key"], row["focus_label"], row["intensity"], row["location"], row["user_notes"]

    # Get history for context
    history_text = _workout_history_block(cur)

    # Generate new workout with same focus but different exercises
    now_local = datetime.now(TZ)
    name = get_config().get("name", "Jarvis")

    if location == "home":
        equip = (
            "HOME GYM: dumbbells only up to 35 lb each, plus bodyweight. "
            "No barbell rack, no heavy machines, no cable stack unless you describe a bodyweight or DB substitute. "
            "Be creative with unilateral work, tempo, and density."
        )
    else:
        equip = (
            "REC GYM (full gym): barbells, squat rack, cables, machines, dumbbells beyond 35 lb, all standard equipment."
        )

    user_prompt = (
        "Athlete name: %s.\n"
        "Today: %s.\n"
        "Today's rotation focus (must be the primary emphasis of this session): **%s**.\n"
        "Target difficulty: **%d / 10** (1 = very easy recovery, 5 = moderate, 8–10 = very demanding).\n"
        "Training location: **%s**.\n\n"
        "Equipment rules: %s\n\n"
        "Recent history — learn from these (honor notes, vary exercises if they repeat complaints, match intensity trends):\n%s\n\n"
        "Write ONE complete workout for today. Include warm-up, main lifts/accessories appropriate to the focus, "
        "optional finisher if intensity ≥ 6, and cool-down. Use **bold** for exercise names. "
        "Give sets, reps or time, rest, and one short form cue per main movement. "
        "Do not skip the rotation focus — secondary work should support it. "
        "Make this DIFFERENT from the previous attempt — use different exercises, rep ranges, or exercise order."
    ) % (
        name,
        now_local.strftime("%A, %B %-d, %Y"),
        focus_label,
        intensity,
        "Home gym (≤35 lb dumbbells + bodyweight)" if location == "home" else "Rec / full gym",
        equip,
        history_text,
    )

    try:
        client = anthropic.Anthropic(api_key=api_key)
        message = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2000,
            messages=[{"role": "user", "content": user_prompt}],
            system=(
                "You are Jarvis, a distinguished and methodical strength and conditioning specialist attending to a diligent high school athlete. "
                "Your programs are meticulously tailored to the available equipment and the athlete's demonstrated capacity. "
                "Safety remains paramount—you will never prescribe reckless or excessive loading. "
                "Should the training history indicate previous injury, fatigue, or overexertion, you shall adjust proactively and with sophistication. "
                "Your recommendations reflect professional precision and unwavering attention to sustainable progression."
            ),
        )
        track_api_usage(message)
        content = message.content[0].text if message.content else ""
    except Exception as e:
        log.error("Workout regenerate API error: %s", e)
        cur.close()
        conn.close()
        return jsonify({"error": "Could not regenerate workout. Check API key and try again."}), 500

    # Update the workout log with new content
    cur.execute(
        "UPDATE workout_logs SET plan_content=%s WHERE id=%s",
        (content, log_id)
    )
    conn.commit()
    cur.close()
    conn.close()

    return jsonify({
        "plan": content,
        "log_id": log_id,
        "status": "regenerated"
    })


@app.route("/api/complete", methods=["POST"])
def api_complete():
    data = request.get_json(force=True) or {}
    title = str(data.get("title", ""))[:300]
    class_name = str(data.get("class_name", ""))[:100]
    estimate = float(data.get("estimate_minutes", 30))
    if not title:
        return jsonify({"error": "title required"}), 400
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
INSERT INTO completions (assignment_title, class_name, duration_minutes, estimate_minutes, timed)
VALUES (%s, %s, 0, %s, FALSE)""", (title, class_name, estimate))
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({"status": "ok"})


@app.route("/api/completions/today")
def api_completions_today():
    today_start = datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
SELECT assignment_title, class_name, duration_minutes, estimate_minutes, timed, completed_at
FROM completions WHERE completed_at >= %s ORDER BY completed_at DESC""", (today_start,))
    rows = [dict(r) for r in cur.fetchall()]
    cur.close()
    conn.close()
    for r in rows:
        r["completed_at"] = r["completed_at"].isoformat()
    return jsonify({"completions": rows})


@app.route("/api/uncomplete", methods=["POST"])
def api_uncomplete():
    """Remove a completion record to 'undo' marking an assignment as done.

    The time logged (duration_minutes) is preserved in the database but the
    assignment will reappear in the active assignments list.
    """
    data = request.get_json(force=True) or {}
    title = str(data.get("title", ""))[:300]
    class_name = str(data.get("class_name", ""))[:100]

    if not title:
        return jsonify({"error": "title required"}), 400

    try:
        conn = get_db()
        cur = conn.cursor()

        # Delete the most recent completion record for this assignment from today
        today_start = datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
        cur.execute("""
DELETE FROM completions
WHERE assignment_title = %s
  AND class_name = %s
  AND completed_at >= %s
ORDER BY completed_at DESC
LIMIT 1""", (title, class_name, today_start))

        conn.commit()
        cur.close()
        conn.close()

        return jsonify({"status": "ok"})
    except Exception as e:
        log.exception("Error uncompleting assignment")
        return jsonify({"error": str(e)}), 500


@app.route("/api/availability")
def api_availability():
    """Return today's school schedule and free time windows."""
    today = datetime.now(TZ).date()
    now_local = datetime.now(TZ)
    dtype = get_day_type(today)
    school_hours = get_school_hours(today)

    # Build busy blocks for today (school + personal events)
    busy = []
    if school_hours:
        sh, sm, eh, em = school_hours
        busy.append({
            "start": now_local.replace(hour=sh, minute=sm, second=0, microsecond=0),
            "end": now_local.replace(hour=eh, minute=em, second=0, microsecond=0),
            "label": "School (%s day)" % dtype.title()
        })

    # Personal calendar events today
    try:
        cal = fetch_ical(PERSONAL_ICAL_URL)
        if cal:
            for e in parse_calendar_events(cal, days_ahead=1):
                if e["date"] == today.isoformat() and not e.get("all_day"):
                    try:
                        es = datetime.fromisoformat(e["start_iso"])
                        ee_str = e.get("end_iso") or e["start_iso"]
                        ee = datetime.fromisoformat(ee_str)
                        if es.tzinfo is None:
                            es = es.replace(tzinfo=TZ)
                        if ee.tzinfo is None:
                            ee = ee.replace(tzinfo=TZ)
                        busy.append({"start": es, "end": ee, "label": e["title"]})
                    except Exception:
                        pass
    except Exception:
        pass

    # Sort and merge busy blocks
    busy.sort(key=lambda x: x["start"])
    merged = []
    for b in busy:
        if merged and b["start"] <= merged[-1]["end"]:
            merged[-1]["end"] = max(merged[-1]["end"], b["end"])
            merged[-1]["label"] += " + " + b["label"]
        else:
            merged.append(dict(b))

    # Find free windows from now until 10 PM
    day_end = now_local.replace(hour=22, minute=0, second=0, microsecond=0)
    free = []
    cursor = now_local.replace(second=0, microsecond=0)
    for b in merged:
        if b["end"] <= cursor:
            continue
        if b["start"] > cursor:
            mins = int((b["start"] - cursor).total_seconds() / 60)
            if mins >= 15:
                free.append({
                    "start": cursor.strftime("%-I:%M %p"),
                    "end": b["start"].strftime("%-I:%M %p"),
                    "minutes": mins
                })
        cursor = max(cursor, b["end"])
    if cursor < day_end:
        mins = int((day_end - cursor).total_seconds() / 60)
        if mins >= 15:
            free.append({
                "start": cursor.strftime("%-I:%M %p"),
                "end": "10:00 PM",
                "minutes": mins
            })

    # School hours display
    school_display = None
    if school_hours:
        sh, sm, eh, em = school_hours
        school_display = "%d:%02d AM – %d:%02d %s" % (
            sh % 12 or 12, sm,
            eh % 12 or 12, em,
            "AM" if eh < 12 else "PM"
        )

    # Pick recommended homework window: first free window ≥ 45 min after school/3pm
    min_start_hour = 14  # don't recommend before 2 PM
    if school_hours:
        _, _, eh, em = school_hours
        min_start_hour = max(eh, 14)
    recommended = None
    for w in free:
        # parse start time to compare hour
        try:
            win_start = merged[0]["end"] if merged else now_local
            # Use the cursor logic: compare to min_start_hour
            # Re-derive the window start as a datetime for comparison
            parts = w["start"].replace(" AM", "").replace(" PM", "").split(":")
            h, m = int(parts[0]), int(parts[1])
            if "PM" in w["start"] and h != 12:
                h += 12
            elif "AM" in w["start"] and h == 12:
                h = 0
            if h >= min_start_hour and w["minutes"] >= 45:
                recommended = w
                break
        except Exception:
            pass
    if recommended is None:
        # Fall back to any window ≥ 30 min
        for w in free:
            if w["minutes"] >= 30:
                recommended = w
                break

    return jsonify({
        "date": today.isoformat(),
        "day_type": dtype,
        "school_hours": school_display,
        "is_school_day": dtype is not None,
        "free_windows": free,
        "total_free_minutes": sum(w["minutes"] for w in free),
        "recommended_homework_time": recommended
    })


@app.route("/api/day-type", methods=["GET"])
def api_day_type():
    """Return the day type (red, white, or non-school) for a given date."""
    date_str = request.args.get("date")
    if not date_str:
        d = datetime.now(TZ).date()
    else:
        try:
            d = datetime.fromisoformat(date_str).date()
        except (ValueError, TypeError):
            return jsonify({"error": "Invalid date format. Use YYYY-MM-DD."}), 400

    dtype = get_day_type(d)
    color = "red" if dtype == "red" else "white" if dtype == "white" else None
    is_school_day = dtype is not None

    return jsonify({
        "date": d.isoformat(),
        "day_type": color,
        "is_school_day": is_school_day,
        "display": f"{d.strftime('%A, %B %-d, %Y')} is a {color} day" if color else f"{d.strftime('%A, %B %-d, %Y')} (no school)"
    })


@app.route("/api/stats")
def api_stats():
    conn = get_db()
    cur = conn.cursor()
    week_start = datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    week_start -= timedelta(days=week_start.weekday())
    cur.execute("SELECT SUM(duration_minutes) as total FROM completions WHERE completed_at >= %s AND timed=TRUE", (week_start,))
    week_row = cur.fetchone()
    weekly_minutes = float(week_row["total"] or 0)
    cur.execute("""
SELECT class_name, AVG(duration_minutes) as avg, COUNT(*) as cnt
FROM completions WHERE timed=TRUE AND duration_minutes>0 AND class_name!=''
GROUP BY class_name ORDER BY avg DESC LIMIT 10""")
    by_class = [{"class_name": r["class_name"], "avg_minutes": round(float(r["avg"]), 1), "count": r["cnt"]} for r in cur.fetchall()]
    cur.execute("""
SELECT AVG(ABS(duration_minutes - estimate_minutes) / NULLIF(estimate_minutes, 0)) as err
FROM completions WHERE timed=TRUE AND estimate_minutes>0 AND duration_minutes>0""")
    acc_row = cur.fetchone()
    accuracy_pct = None
    if acc_row and acc_row["err"] is not None:
        accuracy_pct = round((1.0 - min(float(acc_row["err"]), 1.0)) * 100, 1)
    cur.execute("""
SELECT DISTINCT DATE(completed_at AT TIME ZONE 'America/Denver') as day
FROM completions ORDER BY day DESC LIMIT 30""")
    streak_days = [r["day"] for r in cur.fetchall()]
    streak = 0
    check = date.today()
    for d in streak_days:
        if d == check:
            streak += 1
            check -= timedelta(days=1)
        elif d == check - timedelta(days=1):
            check -= timedelta(days=1)
        else:
            break
    cur.close()
    conn.close()
    return jsonify({"weekly_minutes": round(weekly_minutes, 1), "by_class": by_class,
                    "estimate_accuracy_pct": accuracy_pct, "streak_days": streak})


# ── Tasks ────────────────────────────────────────────────────────────────────

@app.route("/api/tasks", methods=["GET"])
def api_tasks_get():
    start = time.time()
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
SELECT id, title, notes, urgency, completed, completed_at, due_date, created_at,
       NULL as project_id, NULL as project_title
FROM tasks ORDER BY completed ASC,
    CASE urgency WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END ASC,
    created_at ASC""")
        rows = [dict(r) for r in cur.fetchall()]
        # Also include project tasks assigned to "Me"
        cur.execute("""
SELECT pt.id, pt.title, pt.notes, 'medium' as urgency,
       (pt.status = 'done') as completed, NULL as completed_at, pt.due_date,
       pt.created_at, pt.project_id, p.title as project_title
FROM project_tasks pt
JOIN projects p ON p.id = pt.project_id
WHERE p.status = 'active' AND LOWER(pt.assignee) IN ('me', 'finn')
ORDER BY pt.created_at ASC""")
        proj_rows = [dict(r) for r in cur.fetchall()]
        cur.close()
        conn.close()
        for r in rows:
            if r["completed_at"]:
                r["completed_at"] = r["completed_at"].isoformat()
            if r["due_date"]:
                r["due_date"] = str(r["due_date"])
            r["created_at"] = r["created_at"].isoformat()
            r["source"] = "task"
        for r in proj_rows:
            if r["due_date"]:
                r["due_date"] = str(r["due_date"])
            r["created_at"] = r["created_at"].isoformat()
            r["source"] = "project_task"
        log.info(f"/api/tasks: returned {len(rows)} tasks + {len(proj_rows)} project tasks in {time.time()-start:.2f}s")
        return jsonify({"tasks": rows + proj_rows})
    except Exception as e:
        log.exception(f"/api/tasks failed after {time.time()-start:.2f}s: {e}")
        return jsonify({"tasks": []}), 500


@app.route("/api/tasks", methods=["POST"])
def api_tasks_create():
    data = request.get_json(force=True) or {}
    title = str(data.get("title", "")).strip()[:300]
    if not title:
        return jsonify({"error": "title required"}), 400
    notes = str(data.get("notes", ""))[:2000]
    urgency = str(data.get("urgency", "low"))
    due_date = data.get("due_date") or None
    recurrence = str(data.get("recurrence", "")).lower() or None

    conn = get_db()
    cur = conn.cursor()

    if recurrence and recurrence in ("daily", "weekly", "biweekly", "monthly"):
        cur.execute("""
INSERT INTO recurring_tasks (title, notes, urgency, recurrence, active)
VALUES (%s, %s, %s, %s, TRUE) RETURNING id""",
                    (title, notes, urgency, recurrence))
        task_id = cur.fetchone()["id"]

        calc_due_date = _calculate_next_due_date(recurrence)
        cur.execute("""
INSERT INTO tasks (title, notes, urgency, due_date)
VALUES (%s, %s, %s, %s)""",
                    (title, f"[Recurring: {recurrence}]\n{notes}" if notes else f"[Recurring: {recurrence}]", urgency, calc_due_date))
        cur.execute("UPDATE recurring_tasks SET last_created_at = NOW() WHERE id = %s", (task_id,))
    else:
        cur.execute("""
INSERT INTO tasks (title, notes, urgency, due_date) VALUES (%s, %s, %s, %s) RETURNING id""",
                    (title, notes, urgency, due_date))
        task_id = cur.fetchone()["id"]

    conn.commit()
    cur.close()
    conn.close()
    return jsonify({"id": task_id, "status": "ok"})


@app.route("/api/tasks/<int:task_id>", methods=["PATCH"])
def api_tasks_update(task_id):
    data = request.get_json(force=True) or {}
    conn = get_db()
    cur = conn.cursor()
    if "completed" in data:
        completed = bool(data["completed"])
        cur.execute("""
UPDATE tasks SET completed=%s, completed_at=%s WHERE id=%s""",
                    (completed, datetime.now(TZ) if completed else None, task_id))
        # Mark today's plan as needing update if task is completed
        if completed:
            today = datetime.now(TZ).date()
            cur.execute("""
UPDATE daily_plans SET needs_update = TRUE, last_updated_at = NOW()
WHERE plan_date = %s""", (today,))
    if "title" in data:
        cur.execute("UPDATE tasks SET title=%s WHERE id=%s", (str(data["title"])[:300], task_id))
    if "urgency" in data:
        cur.execute("UPDATE tasks SET urgency=%s WHERE id=%s", (str(data["urgency"]), task_id))
    if "notes" in data:
        cur.execute("UPDATE tasks SET notes=%s WHERE id=%s", (str(data["notes"])[:2000], task_id))
    if "due_date" in data:
        cur.execute("UPDATE tasks SET due_date=%s WHERE id=%s", (data["due_date"] or None, task_id))
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({"status": "ok"})


@app.route("/api/tasks/<int:task_id>", methods=["DELETE"])
def api_tasks_delete(task_id):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM tasks WHERE id=%s", (task_id,))
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({"status": "ok"})


# ── Parent API Endpoints ────────────────────────────────────────

@app.route("/api/parent/tasks", methods=["GET"])
def api_parent_tasks_get():
    """Get tasks created by parent (parent can only see their own tasks)."""
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
SELECT id, created_at, title, notes, urgency, completed, completed_at, due_date
FROM tasks WHERE created_by_parent = TRUE
ORDER BY completed ASC, created_at DESC""")
    rows = [dict(r) for r in cur.fetchall()]
    cur.close()
    conn.close()

    for row in rows:
        if row["created_at"]:
            row["created_at"] = row["created_at"].isoformat()
        if row["completed_at"]:
            row["completed_at"] = row["completed_at"].isoformat()
        if row["due_date"]:
            row["due_date"] = row["due_date"].isoformat()

    return jsonify({"tasks": rows})


@app.route("/api/parent/tasks", methods=["POST"])
def api_parent_tasks_create():
    """Create a task from parent portal."""
    data = request.get_json(force=True) or {}
    title = (data.get("title") or "").strip()
    urgency = data.get("urgency", "medium")
    due_date = data.get("due_date")
    notes = (data.get("notes") or "").strip()

    if not title:
        return jsonify({"error": "Task title required"}), 400

    if urgency not in ("high", "medium", "low"):
        urgency = "medium"

    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
INSERT INTO tasks (title, urgency, due_date, notes, created_by_parent)
VALUES (%s, %s, %s, %s, TRUE) RETURNING id, created_at""",
        (title, urgency, due_date or None, notes))
    result = cur.fetchone()
    task_id = result["id"]
    created_at = result["created_at"]
    conn.commit()
    cur.close()
    conn.close()

    return jsonify({
        "id": task_id,
        "title": title,
        "urgency": urgency,
        "due_date": due_date,
        "notes": notes,
        "completed": False,
        "created_at": created_at.isoformat() if created_at else None
    }), 201


@app.route("/api/parent/tasks/<int:task_id>", methods=["PATCH"])
def api_parent_task_update(task_id):
    """Update a parent-created task (can update notes and mark complete)."""
    data = request.get_json(force=True) or {}

    conn = get_db()
    cur = conn.cursor()

    # Verify task was created by parent
    cur.execute("SELECT created_by_parent FROM tasks WHERE id=%s", (task_id,))
    row = cur.fetchone()
    if not row or not row["created_by_parent"]:
        cur.close()
        conn.close()
        return jsonify({"error": "Task not found or not created by parent"}), 404

    updates = []
    params = []

    if "notes" in data:
        updates.append("notes = %s")
        params.append(data["notes"])

    if "completed" in data:
        updates.append("completed = %s")
        params.append(data["completed"])
        if data["completed"]:
            updates.append("completed_at = NOW()")

    if not updates:
        cur.close()
        conn.close()
        return jsonify({"error": "Nothing to update"}), 400

    params.append(task_id)
    query = f"UPDATE tasks SET {', '.join(updates)} WHERE id = %s"
    cur.execute(query, params)
    conn.commit()
    cur.close()
    conn.close()

    return jsonify({"status": "ok"})


@app.route("/recurring-tasks", methods=["GET"])
def api_recurring_tasks_get():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
SELECT id, title, notes, urgency, recurrence, last_created_at, active, created_at
FROM recurring_tasks WHERE active = TRUE ORDER BY created_at DESC""")
    rows = [dict(r) for r in cur.fetchall()]
    cur.close()
    conn.close()
    for r in rows:
        if r["last_created_at"]:
            r["last_created_at"] = r["last_created_at"].isoformat()
        if r["created_at"]:
            r["created_at"] = r["created_at"].isoformat()
    return jsonify({"recurring_tasks": rows})


@app.route("/api/recurring-tasks", methods=["POST"])
def api_recurring_tasks_create():
    data = request.get_json(force=True) or {}
    title = str(data.get("title", "")).strip()[:200]
    notes = str(data.get("notes", "")).strip()[:2000]
    urgency = str(data.get("urgency", "low")).lower()
    if urgency not in ("low", "medium", "high"):
        urgency = "low"
    recurrence = str(data.get("recurrence", "weekly")).lower()
    if recurrence not in ("daily", "weekly", "biweekly", "monthly"):
        recurrence = "weekly"

    if not title:
        return jsonify({"error": "Title required"}), 400

    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
INSERT INTO recurring_tasks (title, notes, urgency, recurrence, active)
VALUES (%s, %s, %s, %s, TRUE) RETURNING id""",
                (title, notes, urgency, recurrence))
    task_id = cur.fetchone()["id"]

    # Create first instance
    due_date = _calculate_next_due_date(recurrence)
    cur.execute("""
INSERT INTO tasks (title, notes, urgency, due_date)
VALUES (%s, %s, %s, %s)""",
                (title, f"[Recurring: {recurrence}]\n{notes}" if notes else f"[Recurring: {recurrence}]", urgency, due_date))
    cur.execute("UPDATE recurring_tasks SET last_created_at = NOW() WHERE id = %s", (task_id,))

    conn.commit()
    cur.close()
    conn.close()
    return jsonify({"status": "ok", "id": task_id}), 201


@app.route("/api/recurring-tasks/<int:task_id>", methods=["PATCH"])
def api_recurring_tasks_update(task_id):
    data = request.get_json(force=True) or {}
    conn = get_db()
    cur = conn.cursor()

    if "active" in data:
        cur.execute("UPDATE recurring_tasks SET active=%s WHERE id=%s", (bool(data["active"]), task_id))

    conn.commit()
    cur.close()
    conn.close()
    return jsonify({"status": "ok"})


@app.route("/api/recurring-tasks/<int:task_id>", methods=["DELETE"])
def api_recurring_tasks_delete(task_id):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM recurring_tasks WHERE id=%s", (task_id,))
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({"status": "ok"})


@app.route("/api/task-suggestions", methods=["GET"])
def api_task_suggestions():
    """Generate AI-powered task suggestions based on pending assignments and calendar events."""
    start = time.time()
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return jsonify({"suggestions": []}), 200

    try:
        # Fetch pending assignments
        assignments = []
        try:
            cal = fetch_ical(CANVAS_ICAL_URL)
            if cal:
                assignments = parse_canvas_assignments(cal)
        except Exception as e:
            log.warning(f"Could not fetch assignments for suggestions: {e}")

        # Fetch calendar events
        calendar_events = []
        try:
            cal = fetch_ical(PERSONAL_ICAL_URL)
            if cal:
                calendar_events = parse_calendar_events(cal, days_ahead=7)
        except Exception as e:
            log.warning(f"Could not fetch calendar events for suggestions: {e}")

        # Get existing tasks and filter completed assignments
        existing_task_titles = set()
        completed_titles = set()
        conn = None
        try:
            conn = get_db()
            cur = conn.cursor()
            # Filter out completed assignments
            cur.execute("SELECT DISTINCT assignment_title FROM completions")
            completed_titles = set(r["assignment_title"] for r in cur.fetchall())
            # Get existing tasks to avoid duplicates
            cur.execute("SELECT title FROM tasks WHERE completed = FALSE")
            existing_task_titles = set(r["title"] for r in cur.fetchall())
            cur.close()
        except Exception as e:
            log.warning(f"Could not fetch data from database: {e}")
        finally:
            if conn:
                conn.close()

        assignments = [a for a in assignments if a["title"] not in completed_titles]

        # Build context for Claude
        asgn_text = "; ".join(
            f"{a['title']} ({a['class_name']}, due {a['due_display']})"
            for a in assignments[:10]
        ) or "None"

        event_text = "; ".join(
            f"{e['title']} on {e['date']}"
            for e in calendar_events[:10]
        ) or "None"

        existing_text = "; ".join(list(existing_task_titles)[:10]) or "None"

        # Prompt Claude to suggest tasks
        prompt = f"""Conduct a thorough analysis of the following items and propose 1-3 strategically relevant NEW tasks.

CRITERIA - Suggestions should meet all of the following conditions:
- The assignment or event has not yet been completed or resolved
- It does not currently exist within the existing task inventory
- The commitment falls within a 14-day window from the present date

Pending assignments: {asgn_text}
Upcoming calendar events: {event_text}
Existing tasks: {existing_text}

Return a valid JSON array exclusively (no markdown blocks, no explanatory prose):
[{{"title": "...", "urgency": "high|medium|low", "due_date": "YYYY-MM-DD", "reason": "..."}}]"""

        client = anthropic.Anthropic(api_key=api_key)
        message = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}]
        )

        content = message.content[0].text.strip()

        # Parse JSON response
        try:
            # Extract JSON array from response (handle potential markdown code blocks)
            json_match = content
            if "```" in content:
                json_match = content.split("```")[1].strip()
                if json_match.startswith("json"):
                    json_match = json_match[4:].strip()

            suggestions = json.loads(json_match)

            # Validate suggestions
            valid_suggestions = []
            for sugg in suggestions:
                if isinstance(sugg, dict) and "title" in sugg:
                    # Validate urgency
                    urgency = sugg.get("urgency", "medium")
                    if urgency not in ("high", "medium", "low"):
                        urgency = "medium"

                    # Validate due_date format
                    due_date = sugg.get("due_date")
                    if due_date:
                        try:
                            datetime.strptime(due_date, "%Y-%m-%d")
                        except ValueError:
                            due_date = None

                    # Skip if already in existing tasks
                    if sugg["title"] in existing_task_titles:
                        continue

                    valid_suggestions.append({
                        "title": str(sugg["title"])[:300],
                        "urgency": urgency,
                        "due_date": due_date,
                        "reason": str(sugg.get("reason", ""))[:200]
                    })

            log.info(f"/api/task-suggestions: returned {len(valid_suggestions)} suggestions in {time.time()-start:.2f}s")
            return jsonify({"suggestions": valid_suggestions[:3]})  # Limit to 3 suggestions

        except json.JSONDecodeError as e:
            log.warning(f"Failed to parse suggestions JSON: {content[:100]} - {e}")
            return jsonify({"suggestions": []}), 200

    except Exception as e:
        log.exception(f"/api/task-suggestions failed after {time.time()-start:.2f}s: {e}")
        return jsonify({"suggestions": []}), 200  # Graceful failure - don't error


def _get_next_monthly_occurrence(position, day_of_week, start_after=None):
    """
    Calculate next occurrence of a monthly pattern like "first Monday" or "last Friday".

    Args:
        position: "first", "second", "third", "fourth", "last"
        day_of_week: 0-6 (0=Monday, 6=Sunday) - matches Python's weekday()
        start_after: date to start searching after (default: today)

    Returns:
        date object of the next occurrence
    """
    import calendar as cal_module

    if start_after is None:
        start_after = date.today()

    # Start checking from next day
    check_date = start_after + timedelta(days=1)

    # Search within next 2 months to find the pattern
    for _ in range(60):
        year, month, _ = check_date.year, check_date.month, check_date.day

        # Get all days in this month with the target weekday
        days_with_weekday = []
        for day in range(1, cal_module.monthrange(year, month)[1] + 1):
            d = date(year, month, day)
            if d.weekday() == day_of_week:
                days_with_weekday.append(d)

        if not days_with_weekday:
            check_date = date(year, month + 1 if month < 12 else year + 1, 1 if month < 12 else 1)
            if month == 12:
                year += 1
            continue

        # Select based on position
        if position == "first":
            result = days_with_weekday[0]
        elif position == "second":
            result = days_with_weekday[1] if len(days_with_weekday) > 1 else days_with_weekday[-1]
        elif position == "third":
            result = days_with_weekday[2] if len(days_with_weekday) > 2 else days_with_weekday[-1]
        elif position == "fourth":
            result = days_with_weekday[3] if len(days_with_weekday) > 3 else days_with_weekday[-1]
        elif position == "last":
            result = days_with_weekday[-1]
        else:
            result = days_with_weekday[0]

        if result > start_after:
            return result

        # Move to next month
        check_date = date(year, month + 1 if month < 12 else year + 1, 1)

    return start_after + timedelta(days=30)


def _calculate_next_due_date(recurrence):
    """Calculate next due date based on recurrence pattern.

    Supports:
    - Legacy formats: "daily", "weekly", "biweekly", "monthly"
    - JSON: {"type": "weekly", "day_of_week": 0}
    - JSON: {"type": "monthly", "position": "first", "day_of_week": 0}
    """
    import json
    today = date.today()

    # Try to parse as JSON first
    try:
        pattern = json.loads(recurrence)
        ptype = pattern.get("type", "daily")

        if ptype == "daily":
            return today + timedelta(days=1)
        elif ptype == "weekly":
            day_of_week = pattern.get("day_of_week", 0)
            # Find next occurrence of this weekday
            days_ahead = (day_of_week - today.weekday()) % 7
            if days_ahead == 0:
                days_ahead = 7  # Next week if today is the target day
            return today + timedelta(days=days_ahead)
        elif ptype == "monthly":
            position = pattern.get("position", "first")
            day_of_week = pattern.get("day_of_week", 0)
            return _get_next_monthly_occurrence(position, day_of_week, today)
        else:
            return today + timedelta(days=1)
    except (json.JSONDecodeError, ValueError, TypeError):
        pass

    # Fall back to legacy string formats
    if recurrence == "daily":
        return today + timedelta(days=1)
    elif recurrence == "weekly":
        return today + timedelta(weeks=1)
    elif recurrence == "biweekly":
        return today + timedelta(weeks=2)
    elif recurrence == "monthly":
        return today + timedelta(days=30)

    return today + timedelta(weeks=1)


def _process_recurring_tasks():
    """Create new task instances for recurring tasks that are due."""
    conn = get_db()
    cur = conn.cursor()
    today = date.today()

    # Find all active recurring tasks where last_created_at is older than recurrence interval
    cur.execute("""
SELECT id, title, notes, urgency, recurrence
FROM recurring_tasks
WHERE active = TRUE
AND (last_created_at IS NULL OR last_created_at::date < %s)""", (today,))

    tasks_to_create = cur.fetchall()

    for task in tasks_to_create:
        task_id = task["id"]
        title = task["title"]
        notes = task["notes"]
        urgency = task["urgency"]
        recurrence = task["recurrence"]

        # Check if we should create a new instance
        should_create = False
        if task["last_created_at"] is None:
            should_create = True
        else:
            last_created = task["last_created_at"].date()
            if recurrence == "daily" and today > last_created:
                should_create = True
            elif recurrence == "weekly" and today >= last_created + timedelta(weeks=1):
                should_create = True
            elif recurrence == "biweekly" and today >= last_created + timedelta(weeks=2):
                should_create = True
            elif recurrence == "monthly" and today >= last_created + timedelta(days=30):
                should_create = True

        if should_create:
            due_date = _calculate_next_due_date(recurrence)
            task_notes = f"[Recurring: {recurrence}]\n{notes}" if notes else f"[Recurring: {recurrence}]"
            cur.execute("""
INSERT INTO tasks (title, notes, urgency, due_date)
VALUES (%s, %s, %s, %s)""",
                        (title, task_notes, urgency, due_date))
            cur.execute("UPDATE recurring_tasks SET last_created_at = NOW() WHERE id = %s", (task_id,))

    conn.commit()
    cur.close()
    conn.close()


# ── Projects ─────────────────────────────────────────────────────────────────

@app.route("/api/projects", methods=["GET"])
def api_projects_get():
    start = time.time()
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
SELECT id, title, description, status, lead, members, last_checkin,
       checkin_interval_days, completion_pct, created_at,
       CASE WHEN last_checkin IS NULL OR
           NOW() - last_checkin > make_interval(days => checkin_interval_days)
       THEN TRUE ELSE FALSE END as needs_checkin
FROM projects
ORDER BY CASE status WHEN 'active' THEN 0 WHEN 'paused' THEN 1 WHEN 'done' THEN 2 ELSE 3 END,
         created_at DESC""")
        rows = [dict(r) for r in cur.fetchall()]
        cur.close()
        conn.close()
        for r in rows:
            if r["last_checkin"]:
                r["last_checkin"] = r["last_checkin"].isoformat()
            r["created_at"] = r["created_at"].isoformat()
        log.info(f"/api/projects: returned {len(rows)} projects in {time.time()-start:.2f}s")
        return jsonify({"projects": rows})
    except Exception as e:
        log.exception(f"/api/projects failed after {time.time()-start:.2f}s: {e}")
        return jsonify({"projects": []}), 500


@app.route("/api/projects", methods=["POST"])
def api_projects_create():
    data = request.get_json(force=True) or {}
    title = str(data.get("title", "")).strip()[:300]
    if not title:
        return jsonify({"error": "title required"}), 400
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
INSERT INTO projects (title, description, status, lead, members, checkin_interval_days, completion_pct, last_checkin)
VALUES (%s, %s, %s, %s, %s, %s, %s, NOW()) RETURNING id""",
                (title, str(data.get("description", ""))[:2000],
                 str(data.get("status", "active")),
                 str(data.get("lead", ""))[:200],
                 str(data.get("members", ""))[:500],
                 int(data.get("checkin_interval_days", 7)),
                 int(data.get("completion_pct", 0))))
    new_id = cur.fetchone()["id"]
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({"id": new_id, "status": "ok"})


@app.route("/api/projects/<int:project_id>", methods=["PATCH"])
def api_projects_update(project_id):
    data = request.get_json(force=True) or {}
    conn = get_db()
    cur = conn.cursor()

    # Validate status if provided
    if "status" in data:
        st = str(data["status"]).strip().lower()
        if st not in ("active", "paused", "done"):
            cur.close()
            conn.close()
            return jsonify({"error": "status must be active, paused, or done"}), 400

    # Build single UPDATE statement with all fields
    updates = {}
    fields_map = {
        "title": ("title", lambda v: str(v)[:300]),
        "description": ("description", lambda v: str(v)[:2000]),
        "lead": ("lead", lambda v: str(v)[:200]),
        "members": ("members", lambda v: str(v)[:500]),
        "status": ("status", lambda v: str(v).strip().lower()),
        "checkin_interval_days": ("checkin_interval_days", lambda v: max(1, min(90, int(v) if isinstance(v, (int, float)) else 7))),
        "completion_pct": ("completion_pct", lambda v: max(0, min(100, int(v) if isinstance(v, (int, float)) else 0)))
    }

    for key, (db_field, transform) in fields_map.items():
        if key in data:
            try:
                updates[db_field] = transform(data[key])
            except (TypeError, ValueError):
                if key == "checkin_interval_days":
                    updates[db_field] = 7
                elif key == "completion_pct":
                    updates[db_field] = 0

    # Add checkin_now if requested
    if data.get("checkin_now"):
        updates["last_checkin"] = pgsql.SQL("NOW()")

    # Execute single UPDATE if there are changes
    if updates:
        set_clause = pgsql.SQL(", ").join(
            pgsql.SQL("{} = %s").format(pgsql.Identifier(k)) if not isinstance(v, pgsql.SQL)
            else pgsql.SQL("{} = {}").format(pgsql.Identifier(k), v)
            for k, v in updates.items()
        )
        values = [v for v in updates.values() if not isinstance(v, pgsql.SQL)]
        cur.execute(
            pgsql.SQL("UPDATE projects SET {} WHERE id = %s").format(set_clause),
            values + [project_id]
        )

    conn.commit()
    cur.close()
    conn.close()
    return jsonify({"status": "ok"})


@app.route("/api/projects/<int:project_id>", methods=["DELETE"])
def api_projects_delete(project_id):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM projects WHERE id=%s", (project_id,))
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({"status": "ok"})


@app.route("/api/projects/<int:project_id>/notes", methods=["GET"])
def api_project_notes_get(project_id):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id, content, created_at FROM project_notes WHERE project_id=%s ORDER BY created_at DESC", (project_id,))
    rows = [dict(r) for r in cur.fetchall()]
    cur.close()
    conn.close()
    for r in rows:
        r["created_at"] = r["created_at"].isoformat()
    return jsonify({"notes": rows})


@app.route("/api/projects/<int:project_id>/notes", methods=["POST"])
def api_project_notes_create(project_id):
    data = request.get_json(force=True) or {}
    content = str(data.get("content", "")).strip()
    if not content:
        return jsonify({"error": "content required"}), 400
    conn = get_db()
    cur = conn.cursor()
    cur.execute("INSERT INTO project_notes (project_id, content) VALUES (%s, %s) RETURNING id",
                (project_id, content))
    new_id = cur.fetchone()["id"]
    # Also update last_checkin
    cur.execute("UPDATE projects SET last_checkin=NOW() WHERE id=%s", (project_id,))
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({"id": new_id, "status": "ok"})


@app.route("/api/projects/<int:project_id>/notes/<int:note_id>", methods=["DELETE"])
def api_project_notes_delete(project_id, note_id):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM project_notes WHERE id=%s AND project_id=%s", (note_id, project_id))
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({"status": "ok"})


# ── Project Tasks ─────────────────────────────────────────────────────────────

@app.route("/api/projects/<int:project_id>/tasks", methods=["GET"])
def api_project_tasks_get(project_id):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
SELECT id, title, notes, assignee, status, due_date, created_at
FROM project_tasks WHERE project_id=%s ORDER BY created_at ASC""", (project_id,))
    rows = [dict(r) for r in cur.fetchall()]
    cur.close()
    conn.close()
    for r in rows:
        if r["due_date"]:
            r["due_date"] = str(r["due_date"])
        r["created_at"] = r["created_at"].isoformat()
    return jsonify({"tasks": rows})


@app.route("/api/projects/<int:project_id>/tasks", methods=["POST"])
def api_project_tasks_create(project_id):
    data = request.get_json(force=True) or {}
    title = str(data.get("title", "")).strip()[:300]
    if not title:
        return jsonify({"error": "title required"}), 400
    notes = str(data.get("notes", ""))[:2000]
    assignee = str(data.get("assignee", ""))[:100]
    status = str(data.get("status", "pending"))
    due_date = data.get("due_date") or None
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
INSERT INTO project_tasks (project_id, title, notes, assignee, status, due_date)
VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
                (project_id, title, notes, assignee, status, due_date))
    new_id = cur.fetchone()["id"]
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({"id": new_id, "status": "ok"})


@app.route("/api/projects/<int:project_id>/tasks/<int:task_id>", methods=["PATCH"])
def api_project_tasks_update(project_id, task_id):
    data = request.get_json(force=True) or {}
    conn = get_db()
    cur = conn.cursor()

    # Build single UPDATE with all provided fields
    updates = {}
    if "title" in data:
        updates["title"] = str(data["title"])[:300]
    if "notes" in data:
        updates["notes"] = str(data["notes"])[:2000]
    if "assignee" in data:
        updates["assignee"] = str(data["assignee"])[:300]
    if "status" in data:
        updates["status"] = str(data["status"])[:100]
    if "due_date" in data:
        updates["due_date"] = data["due_date"] or None

    # Execute single UPDATE if there are changes
    if updates:
        set_clause = pgsql.SQL(", ").join(
            pgsql.SQL("{} = %s").format(pgsql.Identifier(k))
            for k in updates.keys()
        )
        values = list(updates.values()) + [task_id, project_id]
        cur.execute(
            pgsql.SQL("UPDATE project_tasks SET {} WHERE id = %s AND project_id = %s").format(set_clause),
            values
        )

    conn.commit()
    cur.close()
    conn.close()
    return jsonify({"status": "ok"})


@app.route("/api/projects/<int:project_id>/tasks/<int:task_id>", methods=["DELETE"])
def api_project_tasks_delete(project_id, task_id):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM project_tasks WHERE id=%s AND project_id=%s", (task_id, project_id))
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({"status": "ok"})


@app.route("/api/config", methods=["GET"])
def api_config_get():
    cfg = get_config()
    return jsonify({
        "name": cfg.get("name", "Jarvis"),
        "morning_briefing_time": cfg.get("morning_briefing_time", "07:00"),
        "timer_cutoff_multiplier": cfg.get("timer_cutoff_multiplier", "2.0"),
        "has_api_key": bool(cfg.get("anthropic_api_key", "")),
        "weekly_recap_advisor": cfg.get("weekly_recap_advisor", "Mr. Goldberg"),
        "formal_signoff_name": cfg.get("formal_signoff_name", "Finley Thomas"),
        "timezone": cfg.get("timezone", "America/Denver"),
    })


@app.route("/api/config", methods=["POST"])
def api_config_post():
    data = request.get_json(force=True) or {}
    allowed = {
        "name", "morning_briefing_time", "timer_cutoff_multiplier", "anthropic_api_key",
        "weekly_recap_advisor", "formal_signoff_name", "timezone",
    }
    updates = {k: str(v)[:2000] for k, v in data.items() if k in allowed}
    if updates:
        # Validate timezone if provided
        if "timezone" in updates:
            try:
                ZoneInfo(updates["timezone"])
            except Exception:
                return jsonify({"status": "error", "message": "Invalid timezone"}), 400
        set_config(updates)
        if "morning_briefing_time" in updates:
            schedule_briefing()
    return jsonify({"status": "ok"})



@app.route("/api/chat", methods=["POST"])
def api_chat():
    data = request.get_json(force=True) or {}
    system_prompt = data.get("system", "")
    messages = data.get("messages", [])
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return jsonify({"error": "ANTHROPIC_API_KEY not configured in Railway environment."}), 500
    try:
        now_chat = datetime.now(TZ)
        system_prompt = (
            "You are Jarvis, a refined and exceptionally capable AI assistant. You communicate with intelligence, wit, and professional sophistication. "
            "Reference this authoritative date in all temporal reasoning—employ it whenever the student references 'today' or 'tomorrow' "
            "and in all comparisons to assignment due dates: %s. Current local time (Utah): %s. "
        ) % (now_chat.strftime("%A, %B %d, %Y"), now_chat.strftime("%-I:%M %p %Z")) + system_prompt

        # Inject school schedule context
        try:
            today = datetime.now(TZ).date()
            dtype = get_day_type(today)
            school_hours = get_school_hours(today)
            if school_hours:
                sh, sm, eh, em = school_hours
                system_prompt += (
                    " Today is a %s day at Park City High School. "
                    "School runs 7:%02d AM – %d:%02d %s. "
                    "Jarvis is NOT available during school hours. "
                    "Mon-Thu Red: 7:30–11:53 AM, Mon-Thu White: 7:30–2:25 PM, "
                    "Fri Red: 7:30–10:25 AM, Fri White: 7:30–11:30 AM. "
                    "If asked what color day any date is, look it up from the calendar system."
                ) % (dtype.title(), sm, eh % 12 or 12, em, "AM" if eh < 12 else "PM")
            else:
                dow = today.weekday()
                if dow >= 5:
                    system_prompt += " Today is a weekend — no school."
                else:
                    system_prompt += " Today is a no-school day (holiday or break)."
        except Exception:
            pass

        # Inject live assignments into the system prompt
        try:
            cal = fetch_ical(CANVAS_ICAL_URL)
            if cal:
                asgn_list = parse_canvas_assignments(cal)
                # Filter out already-completed assignments
                try:
                    _conn = get_db()
                    _cur = _conn.cursor()
                    _cur.execute("SELECT DISTINCT assignment_title FROM completions")
                    _done = set(r["assignment_title"] for r in _cur.fetchall())
                    _cur.close()
                    _conn.close()
                    asgn_list = [a for a in asgn_list if a["title"] not in _done]
                except Exception:
                    pass
                if asgn_list:
                    asgn_text = "; ".join(
                        "%s (%s, due %s, due_date=%s)" % (
                            a["title"],
                            a["class_name"],
                            a["due_display"],
                            (a.get("due_iso") or "")[:10],
                        )
                        for a in asgn_list
                    )
                    system_prompt += (
                        " Upcoming assignments (not yet completed; due_date is YYYY-MM-DD in your timezone, "
                        "aligned with the authoritative 'today' above): " + asgn_text + "."
                    )
                else:
                    system_prompt += " All assignments are completed."
        except Exception:
            log.warning("/api/chat could not fetch assignments for context")

        # Inject pending tasks (with notes) and project context into the system prompt
        try:
            conn = get_db()
            cur = conn.cursor()
            cur.execute(
                "SELECT title, urgency, notes FROM tasks WHERE completed = FALSE "
                "ORDER BY urgency DESC, created_at ASC LIMIT 10"
            )
            tasks = [dict(r) for r in cur.fetchall()]
            cur.execute("""
SELECT p.title as project, pt.title as task, pt.assignee, pt.status, pt.notes
FROM project_tasks pt JOIN projects p ON p.id = pt.project_id
WHERE p.status = 'active' AND pt.status != 'done' ORDER BY pt.created_at ASC LIMIT 10""")
            proj_tasks = [dict(r) for r in cur.fetchall()]
            cur.execute("""
SELECT p.title as project, pn.content as note
FROM project_notes pn JOIN projects p ON p.id = pn.project_id
WHERE p.status = 'active'
ORDER BY pn.created_at DESC LIMIT 6""")
            proj_notes = [dict(r) for r in cur.fetchall()]
            cur.close()
            conn.close()
            if tasks:
                tasks_text = "; ".join(
                    "[%s] %s%s" % (t["urgency"], t["title"], (" — " + t["notes"][:80]) if t["notes"] else "")
                    for t in tasks
                )
                system_prompt += " Pending tasks: " + tasks_text + "."
            if proj_tasks:
                pt_text = "; ".join(
                    "%s (project: %s, assigned: %s, status: %s)" % (t["task"], t["project"], t["assignee"] or "unassigned", t["status"])
                    for t in proj_tasks
                )
                system_prompt += " Project tasks: " + pt_text + "."
            if proj_notes:
                pn_text = "; ".join("%s: %s" % (n["project"], n["note"][:100]) for n in proj_notes)
                system_prompt += " Recent project notes: " + pn_text + "."
        except Exception:
            log.warning("/api/chat could not fetch tasks for context")

        client = anthropic.Anthropic(api_key=api_key)
        kwargs = {"model": "claude-sonnet-4-6", "max_tokens": 1024, "messages": messages}
        if system_prompt:
            kwargs["system"] = system_prompt
        message = client.messages.create(**kwargs)
        track_api_usage(message)
        content = message.content[0].text if message.content else ""
        return jsonify({"content": content})
    except Exception:
        log.exception("/api/chat failed")
        return jsonify({"error": "Failed to reach AI. Check server logs."}), 500


# ── Plan My Day ──────────────────────────────────────────────────────────────

@app.route("/api/plan-my-day", methods=["GET"])
def api_plan_my_day_get():
    """Get today's daily plan with all scheduled items."""
    start = time.time()
    today = datetime.now(TZ).date()
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT id, needs_update FROM daily_plans WHERE plan_date = %s", (today,))
        plan_row = cur.fetchone()
        if not plan_row:
            cur.close()
            conn.close()
            log.info(f"/api/plan-my-day: no plan found in {time.time()-start:.2f}s")
            return jsonify({"plan_id": None, "items": [], "needs_update": False})

        plan_id = plan_row["id"]
        needs_update = plan_row["needs_update"]

        cur.execute("""
SELECT id, item_type, item_id, item_title, scheduled_start_time, scheduled_end_time,
       estimated_minutes, completed, order_index
FROM daily_plan_items
WHERE plan_id = %s
ORDER BY order_index ASC""", (plan_id,))
        items = [dict(r) for r in cur.fetchall()]
        cur.close()
        conn.close()

        # Format time fields to strings
        for item in items:
            if isinstance(item["scheduled_start_time"], str):
                start_str = item["scheduled_start_time"]
            else:
                start_str = item["scheduled_start_time"].strftime("%H:%M")

            if isinstance(item["scheduled_end_time"], str):
                end_str = item["scheduled_end_time"]
            else:
                end_str = item["scheduled_end_time"].strftime("%H:%M")

            item["scheduled_start_time"] = start_str
            item["scheduled_end_time"] = end_str

        log.info(f"/api/plan-my-day: returned {len(items)} items in {time.time()-start:.2f}s")
        return jsonify({"plan_id": plan_id, "items": items, "needs_update": needs_update})
    except Exception as e:
        log.exception(f"/api/plan-my-day failed after {time.time()-start:.2f}s: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/plan-my-day/generate", methods=["POST"])
def api_plan_my_day_generate():
    """Generate a new daily plan using AI."""
    today = datetime.now(TZ).date()
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")

    try:
        with _plan_lock:
            conn = get_db()
            cur = conn.cursor()

            # Check if plan already exists
            cur.execute("SELECT id FROM daily_plans WHERE plan_date = %s", (today,))
            existing = cur.fetchone()
            if existing:
                # Delete old plan
                cur.execute("DELETE FROM daily_plans WHERE plan_date = %s", (today,))

            # Fetch assignments, tasks, projects, and calendar events
            assignments = []
            tasks = []
            projects = []
            calendar_events = []

            # Get assignments due today (fetch from Canvas calendar, not via HTTP)
            try:
                cal = fetch_ical(CANVAS_ICAL_URL)
                if cal:
                    # Reuse existing cursor instead of opening new connection
                    cur.execute("SELECT DISTINCT assignment_title FROM completions")
                    completed_titles = set(r["assignment_title"] for r in cur.fetchall())
                    cur.execute("SELECT uid, minutes FROM assignment_estimates")
                    custom_estimates = {r["uid"]: r["minutes"] for r in cur.fetchall()}

                    all_asgn = parse_canvas_assignments(cal)
                    # Batch load class averages to avoid N+1 queries
                    class_names = {a["class_name"] for a in all_asgn if a.get("class_name")}
                    class_avg_cache = get_class_averages_batch(class_names)

                    for a in all_asgn:
                        if a["title"] in completed_titles:
                            continue
                        due_iso = a.get("due_iso", "")
                        if due_iso:
                            try:
                                due_dt = datetime.fromisoformat(due_iso.replace("Z", "+00:00"))
                                due_date = due_dt.astimezone(TZ).date()
                                if due_date == today:
                                    uid = a.get("uid", "")
                                    if uid in custom_estimates:
                                        est_mins = custom_estimates[uid]
                                    else:
                                        est_mins = estimate_assignment(a.get("title", ""), a.get("class_name", ""), class_avg_cache=class_avg_cache)
                                    assignments.append({
                                        "type": "assignment",
                                        "id": uid,
                                        "title": a.get("title", ""),
                                        "class": a.get("class_name", ""),
                                        "estimated_minutes": int(est_mins)
                                    })
                            except Exception:
                                pass
            except Exception as e:
                log.warning(f"Could not fetch assignments for plan: {e}")

            # Get all incomplete tasks (not just due today), limit to avoid huge prompts
            # Only include tasks due within the next 14 days or urgent tasks
            cur.execute("""
                SELECT id, title, due_date, urgency FROM tasks
                WHERE completed = FALSE
                AND (due_date IS NULL OR due_date <= %s)
                ORDER BY urgency DESC, due_date ASC
                LIMIT 20
            """, (today + timedelta(days=14),))

            urgency_mins = {"critical": 45, "high": 30, "medium": 20, "low": 15}
            for task_row in cur.fetchall():
                due_date = task_row.get("due_date")
                urgency = task_row.get("urgency", "medium")
                est_mins = urgency_mins.get(urgency, 20)

                tasks.append({
                    "type": "task",
                    "id": str(task_row["id"]),
                    "title": task_row["title"],
                    "due_date": str(due_date) if due_date else None,
                    "urgency": urgency,
                    "estimated_minutes": est_mins
                })

            # Get all active projects (fetch directly from database, not via HTTP)
            try:
                cur.execute("SELECT id, title FROM projects WHERE status = 'active' ORDER BY created_at DESC LIMIT 10")
                for p in cur.fetchall():
                    projects.append({
                        "type": "project",
                        "id": str(p["id"]),
                        "title": p["title"],
                        "estimated_minutes": 45
                    })
            except Exception as e:
                log.warning(f"Could not fetch projects for plan: {e}")

            # Get calendar events for today
            personal_cal = None
            personal_events = []
            try:
                personal_cal = fetch_ical(PERSONAL_ICAL_URL)
                if personal_cal:
                    personal_events = parse_calendar_events(personal_cal, days_ahead=1)
                    for event in personal_events:
                        if event["date"] == today.isoformat() and not event.get("all_day"):
                            calendar_events.append({
                                "type": "calendar",
                                "id": event.get("uid", ""),
                                "title": event["title"],
                                "start_time": event.get("start_iso", ""),
                                "end_time": event.get("end_iso", "")
                            })
            except Exception as e:
                log.warning(f"Could not fetch calendar for plan: {e}")

            # Get free time windows (compute directly, not via HTTP)
            free_windows = []
            try:
                now_local = datetime.now(TZ)
                dtype = get_day_type(today)
                school_hours = get_school_hours(today)

                busy = []
                if school_hours:
                    sh, sm, eh, em = school_hours
                    busy.append({
                        "start": now_local.replace(hour=sh, minute=sm, second=0, microsecond=0),
                        "end": now_local.replace(hour=eh, minute=em, second=0, microsecond=0),
                        "label": f"School ({dtype.title()} day)"
                    })

                # Reuse personal calendar events from above fetch
                for e in personal_events:
                        if e["date"] == today.isoformat() and not e.get("all_day"):
                            try:
                                es = datetime.fromisoformat(e["start_iso"])
                                ee_str = e.get("end_iso") or e["start_iso"]
                                ee = datetime.fromisoformat(ee_str)
                                if es.tzinfo is None:
                                    es = es.replace(tzinfo=TZ)
                                if ee.tzinfo is None:
                                    ee = ee.replace(tzinfo=TZ)
                                busy.append({"start": es, "end": ee, "label": e["title"]})
                            except Exception:
                                pass

                # Merge and find free windows
                busy.sort(key=lambda x: x["start"])
                merged = []
                for b in busy:
                    if merged and b["start"] <= merged[-1]["end"]:
                        merged[-1]["end"] = max(merged[-1]["end"], b["end"])
                    else:
                        merged.append(dict(b))

                day_end = now_local.replace(hour=22, minute=0, second=0, microsecond=0)
                cursor = now_local.replace(second=0, microsecond=0)
                for b in merged:
                    if b["end"] <= cursor:
                        continue
                    if b["start"] > cursor:
                        mins = int((b["start"] - cursor).total_seconds() / 60)
                        if mins >= 15:
                            free_windows.append({
                                "start": cursor.strftime("%-I:%M %p"),
                                "end": b["start"].strftime("%-I:%M %p"),
                                "minutes": mins
                            })
                    cursor = max(cursor, b["end"])

                if cursor < day_end:
                    mins = int((day_end - cursor).total_seconds() / 60)
                    if mins >= 15:
                        free_windows.append({
                            "start": cursor.strftime("%-I:%M %p"),
                            "end": day_end.strftime("%-I:%M %p"),
                            "minutes": mins
                        })
            except Exception as e:
                log.warning(f"Could not compute availability for plan: {e}")

            # Use Claude to generate optimal schedule
            if not api_key:
                cur.close()
                conn.close()
                return jsonify({"error": "ANTHROPIC_API_KEY not configured"}), 500

            client = anthropic.Anthropic(api_key=api_key)
            schedule_prompt = f"""You are Jarvis, an exceptionally sophisticated temporal planner. Orchestrate an optimal daily schedule for today ({today}).

Available time windows (free slots permitting work):
{json.dumps(free_windows, indent=2)}

ITEMS REQUIRING SCHEDULING (select the most strategically valuable within available capacity):
- Assignments due TODAY (MUST be incorporated):
{json.dumps(assignments, indent=2)}

- Tasks to accomplish (sequenced by urgency and due date):
{json.dumps(tasks, indent=2)}

- Active projects (to be addressed as temporal availability permits):
{json.dumps(projects, indent=2)}

Fixed calendar commitments (immovable, pre-established):
{json.dumps(calendar_events, indent=2)}

Deliver your response as a JSON array of scheduled items. Each entry shall include:
- item_type: "assignment", "task", "project", or "calendar"
- item_id: the original ID
- item_title: the title
- scheduled_start_time: "HH:MM" format (24-hour)
- scheduled_end_time: "HH:MM" format (24-hour)

SCHEDULING PROTOCOLS:
1. All assignments due today MUST be incorporated
2. Integrate high/critical urgency tasks that approach their due dates
3. Allocate remaining capacity to medium/low urgency tasks and project work
4. Maintain absolute respect for all fixed calendar commitments (do not encroach)
5. Schedule exclusively within available free windows
6. Front-load the day with high-urgency items and those approaching due dates
7. Consolidate similar work when logically defensible
8. Should capacity prove insufficient, apply this hierarchy: assignments > critical tasks > high tasks > projects

Provide ONLY a valid JSON array response—no markdown, no supplementary explanation."""

            try:
                message = client.messages.create(
                    model="claude-sonnet-4-6",
                    max_tokens=2048,
                    messages=[{"role": "user", "content": schedule_prompt}]
                )
                track_api_usage(message)
                response_text = message.content[0].text if message.content else "[]"
                scheduled_items = json.loads(response_text)
            except Exception as e:
                log.warning(f"Claude plan generation failed: {e}")
                # Fallback: create a simple schedule
                scheduled_items = []
                current_hour = 15  # Start at 3 PM
                for item in assignments + tasks:
                    estimated_mins = item.get("estimated_minutes", 30)
                    start_min = current_hour * 60
                    end_min = start_min + estimated_mins
                    scheduled_items.append({
                        "item_type": item["type"],
                        "item_id": item["id"],
                        "item_title": item["title"],
                        "scheduled_start_time": f"{current_hour:02d}:00",
                        "scheduled_end_time": f"{end_min // 60:02d}:{end_min % 60:02d}"
                    })
                    current_hour = end_min // 60

            # Insert plan into database
            cur.execute(
                "INSERT INTO daily_plans (plan_date, generated_at) VALUES (%s, NOW()) RETURNING id",
                (today,)
            )
            plan_id = cur.fetchone()["id"]

            # Insert scheduled items
            for idx, item in enumerate(scheduled_items):
                cur.execute("""
INSERT INTO daily_plan_items (plan_id, item_type, item_id, item_title,
                              scheduled_start_time, scheduled_end_time, estimated_minutes, order_index)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                    (
                        plan_id,
                        item.get("item_type", ""),
                        str(item.get("item_id", "")),
                        item.get("item_title", ""),
                        item.get("scheduled_start_time", "09:00"),
                        item.get("scheduled_end_time", "09:30"),
                        item.get("estimated_minutes", 30),
                        idx
                    )
                )

            conn.commit()
            cur.close()
            conn.close()

            return jsonify({
                "status": "ok",
                "plan_id": plan_id,
                "items_count": len(scheduled_items)
            })
    except Exception as e:
        log.exception("Error generating daily plan")
        return jsonify({"error": str(e)}), 500


@app.route("/api/plan-my-day/reorder", methods=["PATCH"])
def api_plan_my_day_reorder():
    """Reorder items in today's plan."""
    data = request.get_json(force=True) or {}
    try:
        with _plan_lock:
            conn = get_db()
            cur = conn.cursor()

            today = datetime.now(TZ).date()
            cur.execute("SELECT id FROM daily_plans WHERE plan_date = %s", (today,))
            plan_row = cur.fetchone()

            if not plan_row:
                cur.close()
                conn.close()
                return jsonify({"error": "No plan for today"}), 404

            plan_id = plan_row["id"]

            # Update all items' order
            items = data.get("items", [])
            for item in items:
                cur.execute("""
UPDATE daily_plan_items SET order_index = %s, user_edited = TRUE, updated_at = NOW()
WHERE id = %s AND plan_id = %s""",
                    (item.get("order_index"), item.get("id"), plan_id)
                )

            conn.commit()
            cur.close()
            conn.close()
            return jsonify({"status": "ok"})
    except Exception as e:
        log.exception("Error reordering plan items")
        return jsonify({"error": str(e)}), 500


@app.route("/api/plan-my-day/items/<int:item_id>", methods=["PATCH"])
def api_plan_my_day_item_update(item_id):
    """Update a specific plan item's times."""
    data = request.get_json(force=True) or {}
    try:
        with _plan_lock:
            conn = get_db()
            cur = conn.cursor()

            start_time = data.get("scheduled_start_time")
            end_time = data.get("scheduled_end_time")

            cur.execute("""
UPDATE daily_plan_items
SET scheduled_start_time = COALESCE(%s, scheduled_start_time),
    scheduled_end_time = COALESCE(%s, scheduled_end_time),
    user_edited = TRUE,
    updated_at = NOW()
WHERE id = %s""",
                (start_time, end_time, item_id)
            )

            conn.commit()
            cur.close()
            conn.close()
            return jsonify({"status": "ok"})
    except Exception as e:
        log.exception("Error updating plan item")
        return jsonify({"error": str(e)}), 500


@app.route("/api/plan-my-day", methods=["DELETE"])
def api_plan_my_day_delete():
    """Delete today's plan."""
    today = datetime.now(TZ).date()
    try:
        with _plan_lock:
            conn = get_db()
            cur = conn.cursor()
            cur.execute("DELETE FROM daily_plans WHERE plan_date = %s", (today,))
            conn.commit()
            cur.close()
            conn.close()
            return jsonify({"status": "ok"})
    except Exception as e:
        log.exception("Error deleting daily plan")
        return jsonify({"error": str(e)}), 500


# ──────────────────────────────────────────────────────────────────────────────
# WITHINGS DATA SYNC FUNCTIONS
# ──────────────────────────────────────────────────────────────────────────────

# ──────────────────────────────────────────────────────────────────────────────
# GOOGLE FIT DATA SYNC FUNCTIONS
# ──────────────────────────────────────────────────────────────────────────────


# Initialize database if available
try:
    init_db()
    log.info("Database initialized successfully")
except Exception as e:
    log.warning(f"Database initialization failed: {e}. Running in limited mode.")

# Seed API key from env var into DB so it persists across deploys
try:
    _env_api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if _env_api_key and not get_config().get("anthropic_api_key", ""):
        set_config({"anthropic_api_key": _env_api_key})
        log.info("Seeded ANTHROPIC_API_KEY from environment into DB config")
except Exception as e:
    log.warning(f"Could not seed API key: {e}")

# Guard: only start scheduler and background briefing in the first/main worker.
# With gunicorn --workers 1 this always runs. With multiple workers it only runs
# in the first gunicorn worker (SERVER_SOFTWARE is set before fork).
try:
    _worker_id = os.environ.get("GUNICORN_WORKER_ID", "0")
    if _worker_id in ("", "0", "1"):
        schedule_briefing()
        scheduler.start()
        threading.Thread(target=generate_briefing, daemon=True).start()

        # Ensure debrief is generated if we're in the debrief window (6:30 PM - 7:30 PM)
        now = datetime.now(TZ)
        debrief_start = now.replace(hour=18, minute=30, second=0, microsecond=0)
        debrief_end = now.replace(hour=19, minute=30, second=0, microsecond=0)
        if debrief_start <= now <= debrief_end:
            try:
                conn = get_db()
                cur = conn.cursor()
                cur.execute("SELECT content FROM debrief_cache WHERE id = 1")
                row = cur.fetchone()
                cur.close()
                conn.close()
                # If no debrief or it's empty, generate it
                if not row or not row["content"]:
                    threading.Thread(target=generate_evening_debrief, daemon=True).start()
                    log.info("Debrief window detected - generating debrief on startup")
            except Exception as e:
                log.warning(f"Could not check debrief status: {e}")

        log.info("Background scheduler started")
except Exception as e:
    log.warning(f"Background scheduler failed to start: {e}")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
