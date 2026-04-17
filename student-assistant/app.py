import os
import time
import logging
import threading
import socket
import json
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo

import psycopg2
import psycopg2.extras
from psycopg2 import sql as pgsql
import requests
from functools import wraps
from flask import Flask, request, jsonify, render_template, session, redirect
from icalendar import Calendar
import recurring_ical_events
from apscheduler.schedulers.background import BackgroundScheduler
import anthropic

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "finn-dashboard-secret-change-me")
app.permanent_session_lifetime = timedelta(days=30)
APP_PASSWORD = os.environ.get("APP_PASSWORD", "finn2025")
logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)


@app.before_request
def require_auth():
    if request.path in ('/login', '/logout'):
        return None
    if not session.get("authenticated"):
        if request.path.startswith('/api/'):
            return jsonify({"error": "Not authenticated"}), 401
        return redirect("/login")

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

_briefing_lock = threading.Lock()
_timer_lock = threading.Lock()
_workout_lock = threading.Lock()

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
    due_date DATE
)""")

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

    defaults = {
        "name": "Finn",
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

    # Create indexes for frequently queried columns
    cur.execute("CREATE INDEX IF NOT EXISTS idx_completions_assignment_title ON completions(assignment_title)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_tasks_completed ON tasks(completed, created_at DESC)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_projects_status ON projects(status)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_project_tasks_assignee_status ON project_tasks(assignee, status)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_tasks_due_date ON tasks(due_date)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_completions_completed_at ON completions(completed_at DESC)")

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
        if url in _ical_cache:
            cached_at, cached_cal = _ical_cache[url]
            if now - cached_at < ICAL_CACHE_TTL:
                return cached_cal

        # Check if another thread is already fetching this URL
        if url in _ical_inflight:
            event = _ical_inflight[url]

    # If another thread is fetching, wait for it
    if url in _ical_inflight:
        _ical_inflight[url].wait(timeout=20)
        with _ical_cache_lock:
            if url in _ical_cache:
                return _ical_cache[url][1]
        return None

    # Mark this URL as being fetched
    event = threading.Event()
    with _ical_cache_lock:
        _ical_inflight[url] = event

    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        cal = Calendar.from_ical(resp.content)
        with _ical_cache_lock:
            _ical_cache[url] = (time.monotonic(), cal)
        event.set()  # Signal other waiting threads
        return cal
    except Exception as e:
        log.warning("iCal fetch failed for %s: %s", url, e)
        event.set()  # Signal other waiting threads even on failure
        # Return stale cache on failure rather than None
        with _ical_cache_lock:
            if url in _ical_cache:
                return _ical_cache[url][1]
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
        name = cfg.get("name", "Finn")
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
            "You are a sharp personal assistant for %s, a high school student and student leader in Park City, Utah.\n"
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
            "Write TODAY'S PLAN using EXACTLY these four markdown sections with ## headings (spell each heading exactly):\n\n"
            "## Needs to get done today:\n"
            "• Use bullets. Combine OVERDUE WORK and DUE-TODAY WORK from the reference (not quiz/test).\n"
            "• If both reference lists are None/empty for work, write one bullet: Nothing critical listed — you're caught up on due-today work.\n"
            "• You may mention urgent tasks from Pending tasks if relevant.\n\n"
            "## If you have time it would be good to get this done today:\n"
            "• Bullets from the 'Good to do if time' reference; optional prep or lighter work.\n"
            "• **If the upcoming-quizzes reference is not \"- None.\":** add a bullet for each listed quiz/test "
            "recommending **studying or reviewing** for it today (e.g. \"Review notes / practice problems for the "
            "**Class** quiz\"). Sooner due dates deserve more urgency. For a quiz **today**, suggest a short focused "
            "review if there is time before it — still keep the ⚠️ line under Schedule.\n"
            "• If the only items would be study bullets and you added those, you may omit \"Nothing extra queued.\" "
            "If there are no good-time items and no upcoming quizzes, one bullet: Nothing extra queued.\n\n"
            "## Schedule:\n"
            "• First bullets: today's calendar events (paraphrase from Today's calendar events).\n"
            "• Then add EVERY line from REFERENCE Quizzes/tests exactly as given (each ⚠️ line is its own bullet).\n"
            "• If no events and no quiz lines, one bullet: No calendar entries or quizzes/tests flagged.\n\n"
            "## Upcoming projects and longer homework's:\n"
            "• Bullets from REFERENCE Larger/longer; English papers, big assignments not already covered above.\n"
            "• If none, one bullet: Nothing extra flagged.\n\n"
            "Rules: NEVER list a quiz/test as regular homework under ## Needs to get done today. "
            "Quizzes/tests **due today or overdue** appear only under ## Schedule as the provided ⚠️ lines. "
            "Under ## If you have time, you **may** (and should, when the reference lists any) add **study/review** "
            "bullets for quizzes/tests in the next 7 days — never imply turning in the quiz as an assignment there. "
            "Use **bold** for assignment names where helpful. No intro paragraph. Only these four sections."
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
        name = cfg.get("name", "Finn")
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
            "You are a sharp personal assistant for %s, a high school student in Park City, Utah.\n"
            "Current time: %s (evening debrief)\n\n"
            "TODAY'S ACCOMPLISHMENTS:\n%s\n\n"
            "PRODUCTIVITY METRICS:\n%s\n\n"
            "TIME BREAKDOWN BY CLASS:\n%s\n\n"
            "STILL DUE (not completed):\n%s\n\n"
            "PENDING TASKS:\n%s\n\n"
            "Write a concise evening debrief using ONLY bullet points (start each with •). Include sections:\n"
            "- Summary of accomplishments (reference the items and metrics above)\n"
            "- What still needs doing\n"
            "- Tomorrow's Outlook (brief forecast of what's coming)\n\n"
            "Be direct, encouraging, and insightful. No intro sentence."
        ) % (name, now_str, done_text, metrics_text, time_breakdown, remaining_text, tasks_text)

        try:
            client = anthropic.Anthropic(api_key=api_key)
            message = client.messages.create(model="claude-sonnet-4-6", max_tokens=600,
                                             messages=[{"role": "user", "content": prompt}])
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


def get_timer_state_row():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM timer_state WHERE id = 1")
    row = cur.fetchone()
    cur.close()
    conn.close()
    return dict(row) if row else {}


def get_timer_elapsed(row):
    accumulated = float(row.get("accumulated_seconds") or 0)
    if row.get("active") and row.get("started_at") and not row.get("paused_at"):
        started = row["started_at"]
        if started.tzinfo is None:
            started = started.replace(tzinfo=ZoneInfo("UTC"))
        delta = datetime.now(ZoneInfo("UTC")) - started
        accumulated += delta.total_seconds()
    return accumulated


def timer_response(row):
    elapsed = get_timer_elapsed(row)
    elapsed_min = elapsed / 60.0
    estimate = float(row.get("estimate_minutes") or 30)
    cfg = get_config()
    try:
        multiplier = float(cfg.get("timer_cutoff_multiplier", "2.0"))
    except Exception:
        multiplier = 2.0
    cutoff_min = estimate * multiplier
    return {
        "active": bool(row.get("active")),
        "paused": bool(row.get("paused_at")),
        "assignment_uid": row.get("assignment_uid", ""),
        "assignment_title": row.get("assignment_title", ""),
        "class_name": row.get("class_name", ""),
        "estimate_minutes": estimate,
        "elapsed_minutes": round(elapsed_min, 2),
        "cutoff_minutes": round(cutoff_min, 2),
        "over_estimate": elapsed_min > estimate,
        "over_cutoff": elapsed_min > cutoff_min
    }


# ── Routes ───────────────────────────────────────────────────────────────────

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        if session.get("authenticated"):
            return redirect("/")
        return render_template("login.html")
    data = request.get_json(force=True) or {}
    if data.get("password") == APP_PASSWORD:
        session.permanent = True
        session["authenticated"] = True
        return jsonify({"status": "ok"})
    return jsonify({"error": "Wrong password"}), 401


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")


@app.route("/api/csrf-token")
def api_csrf_token():
    """Get CSRF token for form submissions"""
    import secrets
    if 'csrf_token' not in session:
        session['csrf_token'] = secrets.token_hex(32)
    return jsonify({"csrf_token": session.get('csrf_token')})


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/assignments")
def api_assignments():
    try:
        cal = fetch_ical(CANVAS_ICAL_URL)
        if cal is None:
            return jsonify({"assignments": [], "error": "Failed to fetch Canvas calendar."})
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT assignment_title FROM completions")
        completed_titles = set(r["assignment_title"] for r in cur.fetchall())
        cur.execute("SELECT uid, minutes FROM assignment_estimates")
        custom_estimates = {r["uid"]: r["minutes"] for r in cur.fetchall()}
        cur.close()
        conn.close()
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
        cfg = get_config()
        return jsonify({"assignments": result, "timezone": cfg.get("timezone", "America/Denver")})
    except Exception:
        log.exception("/api/assignments failed")
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
    days = int(request.args.get("days", 30))
    events = []
    # Personal calendar
    cal = fetch_ical(PERSONAL_ICAL_URL)
    if cal:
        for e in parse_calendar_events(cal, days_ahead=days):
            e["source"] = "personal"
            events.append(e)
    # Canvas assignments as calendar events
    cal2 = fetch_ical(CANVAS_ICAL_URL)
    if cal2:
        for a in parse_canvas_assignments(cal2):
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
    # Day-specific calendar (red or white day)
    today = datetime.now(TZ).date()
    events.extend(fetch_day_calendar_events(today, days_ahead=days))
    events.sort(key=lambda x: x["start_iso"])
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
        name = get_config().get("name", "Finn")
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
                    "You are an experienced, safety-conscious strength and conditioning coach for a high school student. "
                    "Programs must be realistic for the equipment listed. Never recommend reckless loading. "
                    "If history shows an injury concern or 'too hard', scale down proactively."
                ),
            )
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
            "The user describes a workout they just completed:\n\n"
            '"%s"\n\n'
            "Categorize this workout and format it nicely. Your response should be structured markdown with:\n"
            "1. A brief summary of the workout (1-2 lines)\n"
            "2. Exercises performed (list them with sets/reps/details if mentioned)\n"
            "3. Estimated difficulty (1-10 scale based on description)\n"
            "4. Primary focus area (e.g., Back, Legs, Biceps & Triceps, Core / Cardio, Shoulders, or Other)\n\n"
            "Format as markdown sections with ## headers. Be supportive and encouraging."
        ) % user_description

        message = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1000,
            messages=[{"role": "user", "content": categorize_prompt}],
        )
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
    name = get_config().get("name", "Finn")

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
                "You are an experienced, safety-conscious strength and conditioning coach for a high school student. "
                "Programs must be realistic for the equipment listed. Never recommend reckless loading. "
                "If history shows an injury concern or 'too hard', scale down proactively."
            ),
        )
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


@app.route("/api/timer", methods=["GET"])
def api_timer_get():
    # Read timer state within lock to prevent race conditions
    with _timer_lock:
        row = get_timer_state_row()
        response = timer_response(row)
    return jsonify(response)


@app.route("/api/timer/start", methods=["POST"])
def api_timer_start():
    data = request.get_json(force=True) or {}
    uid = str(data.get("uid", ""))
    title = str(data.get("title", ""))
    class_name = str(data.get("class_name", ""))
    estimate = float(data.get("estimate_minutes", 30))
    with _timer_lock:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
UPDATE timer_state SET assignment_uid=%s, assignment_title=%s, class_name=%s,
estimate_minutes=%s, started_at=NOW(), paused_at=NULL, accumulated_seconds=0, active=TRUE WHERE id=1""",
                    (uid, title, class_name, estimate))
        conn.commit()
        cur.close()
        conn.close()
        # Read state within lock to prevent race condition
        row = get_timer_state_row()
        response = timer_response(row)
    return jsonify(response)


@app.route("/api/timer/pause", methods=["POST"])
def api_timer_pause():
    with _timer_lock:
        row = get_timer_state_row()
        if not row.get("active") or row.get("paused_at"):
            response = timer_response(row)
            return jsonify(response)
        elapsed = get_timer_elapsed(row)
        conn = get_db()
        cur = conn.cursor()
        cur.execute("UPDATE timer_state SET paused_at=NOW(), accumulated_seconds=%s WHERE id=1", (elapsed,))
        conn.commit()
        cur.close()
        conn.close()
        # Read state within lock
        row = get_timer_state_row()
        response = timer_response(row)
    return jsonify(response)


@app.route("/api/timer/resume", methods=["POST"])
def api_timer_resume():
    with _timer_lock:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("UPDATE timer_state SET started_at=NOW(), paused_at=NULL WHERE id=1")
        conn.commit()
        cur.close()
        conn.close()
        # Read state within lock
        row = get_timer_state_row()
        response = timer_response(row)
    return jsonify(response)


@app.route("/api/timer/stop", methods=["POST"])
def api_timer_stop():
    data = request.get_json(force=True) or {}
    save = bool(data.get("save", True))
    with _timer_lock:
        row = get_timer_state_row()
        elapsed = get_timer_elapsed(row)
        elapsed_min = elapsed / 60.0
        try:
            if save and row.get("assignment_title") and elapsed_min > 0.5:
                conn = get_db()
                try:
                    cur = conn.cursor()
                    cur.execute("""
INSERT INTO completions (assignment_title, class_name, duration_minutes, estimate_minutes, timed)
VALUES (%s, %s, %s, %s, TRUE)""",
                                (row["assignment_title"], row.get("class_name", ""),
                                 round(elapsed_min, 2), float(row.get("estimate_minutes") or 30)))
                    conn.commit()
                finally:
                    cur.close()
                    conn.close()
        finally:
            # Always reset timer state, even if completion insertion fails
            conn2 = get_db()
            try:
                cur2 = conn2.cursor()
                cur2.execute("""
UPDATE timer_state SET active=FALSE, paused_at=NULL, started_at=NULL,
accumulated_seconds=0, assignment_uid='', assignment_title='', class_name='' WHERE id=1""")
                conn2.commit()
            finally:
                cur2.close()
                conn2.close()
    return jsonify({"saved": save, "elapsed_minutes": round(elapsed_min, 2)})


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
    return jsonify({"tasks": rows + proj_rows})


@app.route("/api/tasks", methods=["POST"])
def api_tasks_create():
    data = request.get_json(force=True) or {}
    title = str(data.get("title", "")).strip()[:300]
    if not title:
        return jsonify({"error": "title required"}), 400
    notes = str(data.get("notes", ""))[:2000]
    urgency = str(data.get("urgency", "low"))
    due_date = data.get("due_date") or None
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
INSERT INTO tasks (title, notes, urgency, due_date) VALUES (%s, %s, %s, %s) RETURNING id""",
                (title, notes, urgency, due_date))
    new_id = cur.fetchone()["id"]
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({"id": new_id, "status": "ok"})


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


@app.route("/api/recurring-tasks", methods=["GET"])
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
    return jsonify({"projects": rows})


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
    if "status" in data:
        st = str(data["status"]).strip().lower()
        if st not in ("active", "paused", "done"):
            cur.close()
            conn.close()
            return jsonify({"error": "status must be active, paused, or done"}), 400
        cur.execute("UPDATE projects SET status=%s WHERE id=%s", (st, project_id))
    fields = ["title", "description", "lead", "members",
              "checkin_interval_days", "completion_pct"]
    for f in fields:
        if f not in data:
            continue
        val = data[f]
        if f == "checkin_interval_days":
            try:
                val = max(1, min(90, int(val)))
            except (TypeError, ValueError):
                val = 7
        elif f == "completion_pct":
            try:
                val = max(0, min(100, int(val)))
            except (TypeError, ValueError):
                val = 0
        elif f == "description":
            val = str(val)[:2000]
        elif f == "title":
            val = str(val)[:300]
        elif f == "lead":
            val = str(val)[:200]
        elif f == "members":
            val = str(val)[:500]
        else:
            val = str(val)[:500]
        cur.execute(
            pgsql.SQL("UPDATE projects SET {}=%s WHERE id=%s").format(pgsql.Identifier(f)),
            (val, project_id)
        )
    if data.get("checkin_now"):
        cur.execute("UPDATE projects SET last_checkin=NOW() WHERE id=%s", (project_id,))
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
    allowed = {"title": str, "notes": str, "assignee": str, "status": str, "due_date": None}
    for field, cast in allowed.items():
        if field in data:
            val = str(data[field])[:300] if cast else (data[field] or None)
            cur.execute(
                pgsql.SQL("UPDATE project_tasks SET {} = %s WHERE id = %s AND project_id = %s").format(
                    pgsql.Identifier(field)
                ),
                (val, task_id, project_id)
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
        "name": cfg.get("name", "Finn"),
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
            "Today's date (authoritative for this conversation—use it whenever the student says 'today' or 'tomorrow' "
            "and when comparing to due dates): %s. Current local time (Utah): %s. "
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
                    "Finn is NOT available during school hours. "
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
        content = message.content[0].text if message.content else ""
        return jsonify({"content": content})
    except Exception:
        log.exception("/api/chat failed")
        return jsonify({"error": "Failed to reach AI. Check server logs."}), 500


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
