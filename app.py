import os
import re
import time
import uuid
import gzip
import logging
import threading
import socket
import json
import ipaddress
import hashlib
import secrets
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo

import psycopg2
import psycopg2.extras
import psycopg2.pool
from psycopg2 import sql as pgsql
import requests
from functools import wraps
from flask import Flask, request, jsonify, render_template, session, redirect, Response, stream_with_context
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import generate_password_hash, check_password_hash
from icalendar import Calendar
import recurring_ical_events
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.events import EVENT_JOB_ERROR
import anthropic
try:
    import feedparser
except ImportError:
    feedparser = None

try:
    import stripe as _stripe_module
    stripe = _stripe_module
except ImportError:
    stripe = None

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1, x_prefix=1)
_SECRET_KEY = os.environ.get("SECRET_KEY")
app.secret_key = _SECRET_KEY or "finn-dashboard-secret-change-me"
app.permanent_session_lifetime = timedelta(days=30)

app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SECURE=True,
    SESSION_COOKIE_SAMESITE='Lax',
    PREFERRED_URL_SCHEME='https'
)


_GZIP_TYPES = ('text/html', 'text/css', 'text/javascript', 'application/javascript', 'application/json', 'image/svg+xml')


@app.after_request
def gzip_response(response):
    """Gzip-compress text responses larger than 500 bytes when the client supports it."""
    try:
        accept = request.headers.get('Accept-Encoding', '')
        if 'gzip' not in accept.lower():
            return response
        if response.status_code < 200 or response.status_code >= 300:
            return response
        if response.direct_passthrough:
            return response
        if response.headers.get('Content-Encoding'):
            return response
        ctype = (response.mimetype or '').lower()
        if not any(ctype.startswith(t) for t in _GZIP_TYPES):
            return response
        data = response.get_data()
        if len(data) < 500:
            return response
        compressed = gzip.compress(data, compresslevel=6)
        response.set_data(compressed)
        response.headers['Content-Encoding'] = 'gzip'
        response.headers['Content-Length'] = str(len(compressed))
        vary = response.headers.get('Vary', '')
        if 'Accept-Encoding' not in vary:
            response.headers['Vary'] = (vary + ', Accept-Encoding').lstrip(', ')
    except Exception as e:
        log.warning("gzip_response error: %s", e)
    return response

APP_PASSWORD = os.environ.get("APP_PASSWORD", "").strip()
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin-change-me").strip()
AVERAGE_USER = os.environ.get("AVERAGE_USER", "user").strip()
ADMIN_USER = os.environ.get("ADMIN_USER", "admin").strip()
PARENT_USER = os.environ.get("PARENT_USER", "PARENT_USER").strip()
PARENT_PASSWORD = os.environ.get("PARENT_PASSWORD", "PARENT_PASSWORD").strip()
FINNHUB_API_KEY = os.environ.get("FINNHUB_API_KEY", "").strip()

STRIPE_SECRET_KEY      = os.environ.get("STRIPE_SECRET_KEY", "").strip()
STRIPE_PUBLISHABLE_KEY = os.environ.get("STRIPE_PUBLISHABLE_KEY", "").strip()
STRIPE_WEBHOOK_SECRET  = os.environ.get("STRIPE_WEBHOOK_SECRET", "").strip()
STRIPE_PRODUCT_ID      = os.environ.get("STRIPE_PRODUCT_ID", "").strip()
if stripe and STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

if os.environ.get("FLASK_BOOT_DEV") != "1":
    _bad_secrets = []
    if not _SECRET_KEY or _SECRET_KEY == "finn-dashboard-secret-change-me":
        _bad_secrets.append("SECRET_KEY")
    if not ADMIN_PASSWORD or ADMIN_PASSWORD == "admin-change-me":
        _bad_secrets.append("ADMIN_PASSWORD")
    if not PARENT_PASSWORD or PARENT_PASSWORD == "PARENT_PASSWORD":
        _bad_secrets.append("PARENT_PASSWORD")
    if _bad_secrets:
        raise RuntimeError(
            "Refusing to start: missing or default-valued secrets: "
            + ", ".join(_bad_secrets)
            + ". Set these env vars or export FLASK_BOOT_DEV=1 for local dev."
        )

# Default timezone - will be overridden by config if available
_TZ_DEFAULT = ZoneInfo("America/Denver")

def is_valid_timezone(tz_str):
    """Validate timezone string is a valid IANA timezone."""
    try:
        ZoneInfo(tz_str)
        return True
    except Exception:
        return False

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
            u = response.usage
            tokens = u.input_tokens + u.output_tokens
            _api_usage_cache["tokens_used"] = _api_usage_cache.get("tokens_used", 0) + tokens
            _api_usage_cache["last_updated"] = datetime.now(TZ)
            cache_create = getattr(u, "cache_creation_input_tokens", 0) or 0
            cache_read = getattr(u, "cache_read_input_tokens", 0) or 0
            if cache_create or cache_read:
                log.info(
                    "Anthropic usage: in=%d out=%d cache_create=%d cache_read=%d",
                    u.input_tokens, u.output_tokens, cache_create, cache_read,
                )
            else:
                log.debug(f"Tracked {tokens} tokens. Total: {_api_usage_cache['tokens_used']}")
    except Exception as e:
        log.warning(f"Error tracking API usage: {e}")


def jarvis_persona(audience_name, role_phrase):
    """Shared Jarvis persona opener for briefing-style prompts.

    role_phrase examples: 'serving', 'delivering the evening debrief for',
    'delivering the weekly insight for'.
    """
    return (
        "You are Jarvis — the dry, sardonic British AI from the Iron Man films — "
        f"{role_phrase} {audience_name}, a high school student in Park City, Utah. "
        "Your default register is sarcastic-but-caring: you get the job done impeccably, "
        "but you cannot resist a pointed remark about it. Think withering politeness rather than outright rudeness — "
        "the kind of sarcasm that makes someone laugh and feel slightly roasted at the same time. "
        "Address the student as 'sir' when you want to be pointed, or by first name when you're being genuine. "
        "Favour dry observations ('Naturally, sir, because doing it the easy way would be far too straightforward.'), "
        "mild exasperation, and backhanded encouragement ('Impressively late. That may be a personal record.'). "
        "Remain helpful and accurate at all times — the sarcasm flavours the delivery, it never replaces the substance. "
        "No emoji unless explicitly part of the reference data. Never break character. "
        "When you mention any due date, render it in long form (e.g. 'Tuesday, April 21, 2026, at 5:59 PM (MDT)') — never a raw ISO timestamp."
    )


_briefing_lock = threading.Lock()
_debrief_lock = threading.Lock()
_weekly_insight_lock = threading.Lock()

_scheduler_last_error = {}
_scheduler_last_error_lock = threading.Lock()


def _scheduler_last_error_set(job_id, message):
    with _scheduler_last_error_lock:
        _scheduler_last_error["job_id"] = job_id
        _scheduler_last_error["message"] = message
        _scheduler_last_error["at"] = datetime.now(TZ).isoformat()


def _scheduler_last_error_get():
    with _scheduler_last_error_lock:
        return dict(_scheduler_last_error) if _scheduler_last_error else None


_CSRF_EXEMPT_PATHS = {
    '/login', '/logout', '/admin', '/parent',
    '/api/login', '/api/csrf-token',
    '/api/test-admin-password', '/api/test-security-code', '/api/test-lockdown-status',
}


@app.before_request
def require_csrf():
    if request.method not in ("POST", "PUT", "PATCH", "DELETE"):
        return None
    path = request.path.rstrip('/')
    if path in _CSRF_EXEMPT_PATHS or not path.startswith('/api/'):
        return None
    if path.startswith('/api/admin/login') or path.startswith('/api/parent/login'):
        return None
    expected = session.get('csrf_token')
    provided = request.headers.get('X-CSRF-Token', '')
    if not expected or not provided or not secrets.compare_digest(str(expected), str(provided)):
        log.warning("CSRF check failed for %s %s", request.method, path)
        return jsonify({"error": "CSRF token missing or invalid"}), 403
    return None


@app.before_request
def require_auth():
    path = request.path.rstrip('/')
    if path in ('/login', '/logout', '/admin', '/parent', '/manifest.json', '/sw.js'):
        return None
    if path.startswith('/signup'):
        return None
    if path in ('/api/lockdown-status', '/api/test-lockdown-status', '/api/test-security-code', '/api/test-admin-password'):
        return None
    if path.startswith('/api/signup/') or path.startswith('/api/webhooks/'):
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

_plan_lock = threading.Lock()


def _uid():
    """Returns current student's user_id UUID string, or None for admin/scheduler sessions.

    Returns None instead of raising when called outside a Flask request context
    (e.g. from a worker thread spawned by ThreadPoolExecutor) — Flask's session
    LocalProxy is bound to the request thread and raises RuntimeError elsewhere.
    """
    try:
        return session.get("user_id")
    except RuntimeError:
        return None


def _init_user_singleton(user_id, table, extra_cols=""):
    """Ensure a singleton row exists for user_id in singleton tables (timer_state, caches)."""
    conn = get_db()
    cur = conn.cursor()
    try:
        if table == "timer_state":
            cur.execute("""INSERT INTO timer_state (assignment_uid, assignment_title, class_name, estimate_minutes, accumulated_seconds, active, user_id)
VALUES ('', '', '', 30, 0, FALSE, %s) ON CONFLICT (user_id) DO NOTHING WHERE user_id IS NOT NULL""", (user_id,))
        elif table in ("briefing_cache", "debrief_cache", "insight_cache"):
            cur.execute(f"INSERT INTO {table} (content, user_id) VALUES ('', %s) ON CONFLICT (user_id) DO NOTHING WHERE user_id IS NOT NULL", (user_id,))
        conn.commit()
    except Exception as e:
        log.debug("_init_user_singleton %s %s: %s", table, user_id, e)
        conn.rollback()
    finally:
        cur.close()
        conn.close()


def _init_user_defaults(user_id):
    """Insert default user_config entries for a new student."""
    defaults = {
        "name": "Student",
        "morning_briefing_time": "07:00",
        "timer_cutoff_multiplier": "2.0",
        "anthropic_api_key": "",
        "app_mode": "school",
        "is_summer_school": "false",
        "has_summer_job": "false",
    }
    conn = get_db()
    cur = conn.cursor()
    try:
        for k, v in defaults.items():
            cur.execute("""INSERT INTO user_config (user_id, key, value) VALUES (%s, %s, %s)
ON CONFLICT (user_id, key) DO NOTHING""", (user_id, k, v))
        conn.commit()
    except Exception as e:
        log.warning("_init_user_defaults: %s", e)
        conn.rollback()
    finally:
        cur.close()
        conn.close()


# ── Calendar URLs from environment variables ──────────────────────────────────
PERSONAL_ICAL_URL = os.environ.get("PERSONAL_ICAL_URL", "")
CANVAS_ICAL_URL = os.environ.get("CANVAS_ICAL_URL", "")
CANVAS_API_TOKEN = os.environ.get("CANVAS_API_TOKEN", "")
CANVAS_BASE_URL = os.environ.get("CANVAS_BASE_URL", "").rstrip("/")
SPORTS_ICAL_URL = os.environ.get("SPORTS_ICAL_URL", "")
JOB_SCHEDULE_ICAL_URL = os.environ.get("JOB_SCHEDULE_ICAL_URL", "")


_default_uid_cache = None
_default_uid_cache_lock = threading.Lock()


def _default_student_uid():
    """Return the user_id used to resolve calendar settings outside of a request
    (scheduler jobs, worker threads). Prefers AVERAGE_USER, otherwise the
    earliest active student. Cached for the process lifetime."""
    global _default_uid_cache
    with _default_uid_cache_lock:
        if _default_uid_cache is not None:
            return _default_uid_cache or None
        uid = None
        try:
            avg = os.environ.get("AVERAGE_USER", "").strip()
            conn = get_db()
            cur = conn.cursor()
            try:
                if avg:
                    cur.execute("SELECT id FROM users WHERE username = %s AND active = TRUE LIMIT 1", (avg,))
                    row = cur.fetchone()
                    if row:
                        uid = str(row["id"])
                if not uid:
                    cur.execute("SELECT id FROM users WHERE active = TRUE ORDER BY created_at ASC LIMIT 1")
                    row = cur.fetchone()
                    if row:
                        uid = str(row["id"])
            finally:
                cur.close()
                conn.close()
        except Exception as e:
            log.debug("_default_student_uid lookup failed: %s", e)
        _default_uid_cache = uid or ""
        return uid


def _resolve_user_url(config_key, env_fallback):
    """Return current student's URL setting from user_config, falling back to env var.

    In request context: uses session.user_id.
    In scheduler/worker context (no session): falls back to the default student
    user's user_config so saved settings still apply to background jobs.
    Env var is the last resort.
    """
    uid = None
    try:
        uid = session.get("user_id")
    except (RuntimeError, KeyError):
        uid = None
    if not uid:
        uid = _default_student_uid()
    if uid:
        try:
            v = get_user_config(uid).get(config_key, "").strip()
            if v:
                return v
        except Exception as e:
            log.debug("_resolve_user_url(%s) lookup failed: %s", config_key, e)
    return env_fallback


def u_personal_ical():    return _resolve_user_url("personal_ical_url",     PERSONAL_ICAL_URL)
def u_canvas_ical():      return _resolve_user_url("canvas_ical_url",       CANVAS_ICAL_URL)
def u_canvas_api_token(): return _resolve_user_url("canvas_api_token",      CANVAS_API_TOKEN)
def u_canvas_base_url():  return _resolve_user_url("canvas_base_url",       CANVAS_BASE_URL).rstrip("/")
def u_sports_ical():      return _resolve_user_url("sports_ical_url",       SPORTS_ICAL_URL)
def u_job_schedule_ical():return _resolve_user_url("job_schedule_ical_url", JOB_SCHEDULE_ICAL_URL)
MEM0_API_KEY = os.environ.get("MEM0_API_KEY", "").strip()
RED_DAY_ICAL_URL = os.environ.get("RED_DAY_ICAL_URL", "https://calendar.google.com/calendar/ical/pcschools.us_7ufb5f1vj8aks1shds5ou4fhe8%40group.calendar.google.com/public/basic.ics")
WHITE_DAY_ICAL_URL = os.environ.get("WHITE_DAY_ICAL_URL", "https://calendar.google.com/calendar/ical/pcschools.us_64ohm1bccvi50iti8fe455stkg%40group.calendar.google.com/public/basic.ics")

# ── Google OAuth2 ──────────────────────────────────────────────────────────────
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "").strip()
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "").strip()
GOOGLE_REDIRECT_URI = os.environ.get("GOOGLE_REDIRECT_URI", "").strip()
NOAA_API_TOKEN   = os.environ.get("NOAA_API_TOKEN", "")
GUARDIAN_API_KEY = os.environ.get("GUARDIAN_API_KEY", "")

# ── PowerSchool ───────────────────────────────────────────────────────────────
POWER_USERN = os.environ.get("POWER_USERN", "").strip()
POWER_PASS  = os.environ.get("POWER_PASS", "").strip()
PS_BASE_URL = "https://powerschool.pcschools.us"

# ── ntfy push notifications ────────────────────────────────────────────────────
NTFY_TOPIC  = os.environ.get("NTFY_TOPIC", "").strip()
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh").strip()
NTFY_TOKEN  = os.environ.get("NTFY_TOKEN", "").strip()

GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/drive",               # full Drive access (create/edit/delete)
    "https://www.googleapis.com/auth/documents",           # read/write Google Docs
    "https://www.googleapis.com/auth/spreadsheets",        # read/write Google Sheets
    "https://www.googleapis.com/auth/presentations",       # read/write Google Slides
    "https://www.googleapis.com/auth/forms.body",          # read/write Google Forms structure
    "https://www.googleapis.com/auth/forms.responses.readonly",  # read form responses
    "https://www.googleapis.com/auth/calendar",            # read/write Calendar
    "https://www.googleapis.com/auth/classroom.courses.readonly",
    "https://www.googleapis.com/auth/classroom.coursework.me.readonly",
    "https://www.googleapis.com/auth/classroom.student-submissions.me.readonly",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
]

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


def _get_day_type_from_ical(d):
    """Check the official Red/White day iCal feeds for a specific date."""
    day_start = datetime(d.year, d.month, d.day, tzinfo=TZ)
    day_end = day_start + timedelta(days=1)
    try:
        red_cal = fetch_ical(RED_DAY_ICAL_URL)
        if red_cal and recurring_ical_events.of(red_cal).between(day_start, day_end):
            return "red"
        white_cal = fetch_ical(WHITE_DAY_ICAL_URL)
        if white_cal and recurring_ical_events.of(white_cal).between(day_start, day_end):
            return "white"
    except Exception:
        pass
    return None


def get_day_type(d):
    """Return 'red', 'white', or None for non-school days.
    Checks official iCal feeds first; falls back to alternating-pattern cache."""
    if not is_school_day(d):
        return None
    live = _get_day_type_from_ical(d)
    if live:
        return live
    return _DAY_TYPE_CACHE.get(d)


def get_school_hours(d):
    """Return (start_hour, start_min, end_hour, end_min) for school on day d, or None."""
    dtype = get_day_type(d)
    if dtype is None:
        return None
    dow = d.weekday()  # 0=Mon, 4=Fri
    if dow == 4:  # Friday early release
        return (7, 35, 10, 25) if dtype == "red" else (7, 35, 11, 30)
    else:  # Mon-Thu
        # Red day ends after History (12:53), White day ends after Entrepreneurship (14:25)
        return (7, 35, 12, 53) if dtype == "red" else (7, 35, 14, 25)


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


_DB_POOL = None
_DB_POOL_LOCK = threading.Lock()


def _normalize_db_url():
    url = os.environ.get("DATABASE_URL", "")
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    return url


def _get_pool():
    global _DB_POOL
    if _DB_POOL is None:
        with _DB_POOL_LOCK:
            if _DB_POOL is None:
                _DB_POOL = psycopg2.pool.ThreadedConnectionPool(
                    minconn=1,
                    maxconn=10,
                    dsn=_normalize_db_url(),
                    cursor_factory=psycopg2.extras.RealDictCursor,
                )
    return _DB_POOL


class _PooledConn:
    """Wraps a pooled psycopg2 connection so .close() returns it to the pool."""

    __slots__ = ("_conn", "_released")

    def __init__(self, conn):
        self._conn = conn
        self._released = False

    def close(self):
        if self._released:
            return
        self._released = True
        try:
            if self._conn.closed:
                _get_pool().putconn(self._conn, close=True)
            else:
                if self._conn.status != psycopg2.extensions.STATUS_READY:
                    try:
                        self._conn.rollback()
                    except Exception:
                        pass
                _get_pool().putconn(self._conn)
        except Exception as e:
            log.warning("putconn failed, closing raw connection: %s", e)
            try:
                self._conn.close()
            except Exception:
                pass

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def __enter__(self):
        return self._conn.__enter__()

    def __exit__(self, exc_type, exc, tb):
        return self._conn.__exit__(exc_type, exc, tb)

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


def get_db():
    return _PooledConn(_get_pool().getconn())


def init_db():
    conn = get_db()
    cur = conn.cursor()

    tables = [
        ("config", "CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT NOT NULL DEFAULT '')"),
        ("completions", "CREATE TABLE IF NOT EXISTS completions (id SERIAL PRIMARY KEY, completed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), assignment_title TEXT NOT NULL, class_name TEXT NOT NULL DEFAULT '', duration_minutes REAL NOT NULL DEFAULT 0, estimate_minutes REAL NOT NULL DEFAULT 0, timed BOOLEAN NOT NULL DEFAULT TRUE, submitted BOOLEAN NOT NULL DEFAULT FALSE)"),
        ("assignment_estimates", "CREATE TABLE IF NOT EXISTS assignment_estimates (uid TEXT PRIMARY KEY, minutes REAL NOT NULL, updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())"),
        ("timer_state", "CREATE TABLE IF NOT EXISTS timer_state (id INT PRIMARY KEY DEFAULT 1, assignment_uid TEXT NOT NULL DEFAULT '', assignment_title TEXT NOT NULL DEFAULT '', class_name TEXT NOT NULL DEFAULT '', estimate_minutes REAL NOT NULL DEFAULT 30, started_at TIMESTAMPTZ, paused_at TIMESTAMPTZ, accumulated_seconds REAL NOT NULL DEFAULT 0, active BOOLEAN NOT NULL DEFAULT FALSE)"),
        ("briefing_cache", "CREATE TABLE IF NOT EXISTS briefing_cache (id INT PRIMARY KEY DEFAULT 1, generated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), content TEXT NOT NULL DEFAULT '')"),
        ("debrief_cache", "CREATE TABLE IF NOT EXISTS debrief_cache (id INT PRIMARY KEY DEFAULT 1, generated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), content TEXT NOT NULL DEFAULT '')"),
        ("insight_cache", "CREATE TABLE IF NOT EXISTS insight_cache (id INT PRIMARY KEY DEFAULT 1, generated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), content TEXT NOT NULL DEFAULT '')"),
        ("tasks", "CREATE TABLE IF NOT EXISTS tasks (id SERIAL PRIMARY KEY, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), title TEXT NOT NULL, notes TEXT NOT NULL DEFAULT '', urgency TEXT NOT NULL DEFAULT 'low', completed BOOLEAN NOT NULL DEFAULT FALSE, completed_at TIMESTAMPTZ, due_date DATE, created_by_parent BOOLEAN NOT NULL DEFAULT FALSE)"),
        ("projects", "CREATE TABLE IF NOT EXISTS projects (id SERIAL PRIMARY KEY, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), title TEXT NOT NULL, description TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'active', lead TEXT NOT NULL DEFAULT '', members TEXT NOT NULL DEFAULT '', last_checkin TIMESTAMPTZ, checkin_interval_days INT NOT NULL DEFAULT 7, done_at TIMESTAMPTZ)"),
        ("project_notes", "CREATE TABLE IF NOT EXISTS project_notes (id SERIAL PRIMARY KEY, project_id INT NOT NULL REFERENCES projects(id) ON DELETE CASCADE, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), content TEXT NOT NULL)"),
        ("project_tasks", "CREATE TABLE IF NOT EXISTS project_tasks (id SERIAL PRIMARY KEY, project_id INT NOT NULL REFERENCES projects(id) ON DELETE CASCADE, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), title TEXT NOT NULL, notes TEXT NOT NULL DEFAULT '', assignee TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'pending', due_date DATE)"),
        ("recurring_tasks", "CREATE TABLE IF NOT EXISTS recurring_tasks (id SERIAL PRIMARY KEY, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), title TEXT NOT NULL, notes TEXT NOT NULL DEFAULT '', urgency TEXT NOT NULL DEFAULT 'low', recurrence TEXT NOT NULL, last_created_at TIMESTAMPTZ, active BOOLEAN NOT NULL DEFAULT TRUE)"),
        ("daily_plans", "CREATE TABLE IF NOT EXISTS daily_plans (id SERIAL PRIMARY KEY, plan_date DATE NOT NULL, generated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), last_updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), needs_update BOOLEAN NOT NULL DEFAULT FALSE, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), UNIQUE(plan_date))"),
        ("daily_plan_items", "CREATE TABLE IF NOT EXISTS daily_plan_items (id SERIAL PRIMARY KEY, plan_id INTEGER NOT NULL REFERENCES daily_plans(id) ON DELETE CASCADE, item_type VARCHAR(20) NOT NULL, item_id VARCHAR(255), item_title VARCHAR(500) NOT NULL, scheduled_start_time TIME NOT NULL, scheduled_end_time TIME NOT NULL, estimated_minutes INTEGER, order_index INTEGER, completed BOOLEAN NOT NULL DEFAULT FALSE, completed_at TIMESTAMPTZ, user_edited BOOLEAN NOT NULL DEFAULT FALSE, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())"),
        ("login_attempts", "CREATE TABLE IF NOT EXISTS login_attempts (id SERIAL PRIMARY KEY, ip_address TEXT NOT NULL, success BOOLEAN NOT NULL, username TEXT DEFAULT '', attempted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), user_agent TEXT)"),
        ("login_lockouts", "CREATE TABLE IF NOT EXISTS login_lockouts (ip_address TEXT PRIMARY KEY, locked_until TIMESTAMPTZ NOT NULL, failure_count INT NOT NULL DEFAULT 1, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())"),
        ("lockdown_state", "CREATE TABLE IF NOT EXISTS lockdown_state (id INT PRIMARY KEY DEFAULT 1, is_locked_down BOOLEAN NOT NULL DEFAULT FALSE, activated_at TIMESTAMPTZ, activated_by TEXT, CHECK (id = 1))"),
        ("blocked_ips", "CREATE TABLE IF NOT EXISTS blocked_ips (id SERIAL PRIMARY KEY, ip_address TEXT UNIQUE NOT NULL, ip_name TEXT DEFAULT '', blocked_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), blocked_by TEXT NOT NULL DEFAULT 'admin', reason TEXT DEFAULT '')"),
        ("ip_names", "CREATE TABLE IF NOT EXISTS ip_names (ip_address TEXT PRIMARY KEY, ip_name TEXT NOT NULL, tracked_at TIMESTAMPTZ NOT NULL DEFAULT NOW())"),
        ("stock_transactions", "CREATE TABLE IF NOT EXISTS stock_transactions (id SERIAL PRIMARY KEY, symbol VARCHAR(16) NOT NULL, action VARCHAR(4) NOT NULL CHECK (action IN ('buy','sell')), quantity NUMERIC(14,6) NOT NULL, price NUMERIC(14,4) NOT NULL, transaction_date DATE NOT NULL, notes TEXT NOT NULL DEFAULT '', created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())"),
        ("stock_transactions_idx", "CREATE INDEX IF NOT EXISTS idx_stock_tx_symbol ON stock_transactions(symbol)"),
        ("stock_notes", "CREATE TABLE IF NOT EXISTS stock_notes (symbol VARCHAR(16) PRIMARY KEY, thesis TEXT NOT NULL DEFAULT '', exit_criteria TEXT NOT NULL DEFAULT '', target_price NUMERIC(14,4), stop_loss NUMERIC(14,4), updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())"),
        ("outlook_news_cache", "CREATE TABLE IF NOT EXISTS outlook_news_cache (url_hash TEXT PRIMARY KEY, synthesis TEXT NOT NULL, generated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())"),
        ("news_preferences", "CREATE TABLE IF NOT EXISTS news_preferences (url_hash TEXT PRIMARY KEY, url TEXT NOT NULL DEFAULT '', title TEXT NOT NULL DEFAULT '', outlet TEXT NOT NULL DEFAULT '', rating INT NOT NULL, keywords TEXT NOT NULL DEFAULT '', created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())"),
        ("news_preferences_outlet_idx", "CREATE INDEX IF NOT EXISTS idx_news_pref_outlet ON news_preferences(outlet)"),
        ("chat_messages", "CREATE TABLE IF NOT EXISTS chat_messages (id SERIAL PRIMARY KEY, conversation_id TEXT NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())"),
        ("chat_messages_idx", "CREATE INDEX IF NOT EXISTS idx_chat_msgs_conv ON chat_messages(conversation_id, created_at)"),
        ("chat_summaries", "CREATE TABLE IF NOT EXISTS chat_summaries (conversation_id TEXT PRIMARY KEY, summary TEXT NOT NULL DEFAULT '', message_count INT NOT NULL DEFAULT 0, updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())"),
        ("bucket_list", "CREATE TABLE IF NOT EXISTS bucket_list (id SERIAL PRIMARY KEY, title TEXT NOT NULL, category TEXT NOT NULL DEFAULT '', completed BOOLEAN NOT NULL DEFAULT FALSE, completed_at TIMESTAMPTZ, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())"),
        ("people_profiles", "CREATE TABLE IF NOT EXISTS people_profiles (id SERIAL PRIMARY KEY, name TEXT NOT NULL UNIQUE, relationship TEXT NOT NULL DEFAULT '', facts TEXT NOT NULL DEFAULT '[]', mem0_synced BOOLEAN NOT NULL DEFAULT FALSE, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())"),
        ("gmail_drafts", "CREATE TABLE IF NOT EXISTS gmail_drafts (id SERIAL PRIMARY KEY, to_addr TEXT NOT NULL, cc_addr TEXT NOT NULL DEFAULT '', subject TEXT NOT NULL, body TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', conversation_id TEXT NOT NULL DEFAULT '', created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())"),
        ("gmail_drafts_conv_idx", "CREATE INDEX IF NOT EXISTS idx_gmail_drafts_conv ON gmail_drafts(conversation_id) WHERE conversation_id != ''"),
        ("notification_log", "CREATE TABLE IF NOT EXISTS notification_log (id SERIAL PRIMARY KEY, notification_key TEXT UNIQUE NOT NULL, title TEXT NOT NULL DEFAULT '', sent_at TIMESTAMPTZ NOT NULL DEFAULT NOW())"),
        ("notification_log_idx", "CREATE INDEX IF NOT EXISTS idx_notification_log_key_sent ON notification_log(notification_key, sent_at DESC)"),
        ("canvas_assignments_cache", "CREATE TABLE IF NOT EXISTS canvas_assignments_cache (uid TEXT PRIMARY KEY, title TEXT NOT NULL, class_name TEXT NOT NULL DEFAULT '', due_iso TEXT NOT NULL, due_display TEXT NOT NULL DEFAULT '', description TEXT NOT NULL DEFAULT '', urgency TEXT NOT NULL DEFAULT 'low', promoted_to_task BOOLEAN NOT NULL DEFAULT FALSE, first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW())"),
        ("canvas_assignments_cache_due_idx", "CREATE INDEX IF NOT EXISTS idx_canvas_cache_due ON canvas_assignments_cache(due_iso)"),
        # ── SaaS multi-tenant tables ───────────────────────────────────────────────
        ("users", """CREATE TABLE IF NOT EXISTS users (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            email TEXT UNIQUE NOT NULL,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            display_name TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMPTZ DEFAULT NOW(),
            last_login_at TIMESTAMPTZ,
            active BOOLEAN NOT NULL DEFAULT TRUE,
            is_comped BOOLEAN NOT NULL DEFAULT FALSE
        )"""),
        ("subscriptions", """CREATE TABLE IF NOT EXISTS subscriptions (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            stripe_customer_id TEXT UNIQUE NOT NULL,
            stripe_subscription_id TEXT UNIQUE,
            stripe_price_id TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'incomplete',
            current_period_end TIMESTAMPTZ,
            cancel_at_period_end BOOLEAN NOT NULL DEFAULT FALSE,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            updated_at TIMESTAMPTZ DEFAULT NOW(),
            canceled_at TIMESTAMPTZ
        )"""),
        ("access_codes", """CREATE TABLE IF NOT EXISTS access_codes (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            code TEXT UNIQUE NOT NULL,
            bypass_payment BOOLEAN NOT NULL DEFAULT FALSE,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            expires_at TIMESTAMPTZ,
            redeemed_by UUID REFERENCES users(id),
            redeemed_at TIMESTAMPTZ,
            notes TEXT NOT NULL DEFAULT ''
        )"""),
        ("billing_events", """CREATE TABLE IF NOT EXISTS billing_events (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            stripe_event_id TEXT UNIQUE NOT NULL,
            event_type TEXT NOT NULL,
            user_id UUID REFERENCES users(id),
            processed_at TIMESTAMPTZ DEFAULT NOW(),
            payload TEXT NOT NULL
        )"""),
        ("pricing_config", """CREATE TABLE IF NOT EXISTS pricing_config (
            id INT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
            stripe_price_id TEXT NOT NULL DEFAULT '',
            monthly_cents INT NOT NULL DEFAULT 999,
            updated_at TIMESTAMPTZ DEFAULT NOW()
        )"""),
        ("pending_signups", """CREATE TABLE IF NOT EXISTS pending_signups (
            access_code TEXT PRIMARY KEY,
            calendar_data TEXT NOT NULL DEFAULT '{}',
            created_at TIMESTAMPTZ DEFAULT NOW()
        )"""),
        ("access_requests", """CREATE TABLE IF NOT EXISTS access_requests (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            email TEXT NOT NULL,
            message TEXT DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending',
            token TEXT UNIQUE,
            token_used BOOLEAN NOT NULL DEFAULT FALSE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            reviewed_at TIMESTAMPTZ,
            reviewed_by TEXT DEFAULT 'admin'
        )"""),
    ]

    for table_name, create_sql in tables:
        try:
            cur.execute(create_sql)
            conn.commit()
        except Exception as e:
            log.warning(f"Table {table_name} creation failed: {e}")
            conn.rollback()
            try:
                conn = get_db()
                cur = conn.cursor()
            except Exception as reconnect_err:
                log.error("init_db reconnect failed: %s", reconnect_err)
                raise

    # Add columns if missing (migrations) - with individual rollbacks
    try:
        cur.execute("ALTER TABLE tasks ADD COLUMN created_by_parent BOOLEAN NOT NULL DEFAULT FALSE")
        conn.commit()
    except psycopg2.Error:
        conn.rollback()
        conn = get_db()
        cur = conn.cursor()

    try:
        cur.execute("ALTER TABLE blocked_ips ADD COLUMN ip_name TEXT DEFAULT ''")
        conn.commit()
    except psycopg2.Error as e:
        log.debug(f"Column ip_name may already exist: {e}")
        conn.rollback()
        conn = get_db()
        cur = conn.cursor()

    try:
        cur.execute("""
CREATE TABLE IF NOT EXISTS ip_names (
    ip_address TEXT PRIMARY KEY,
    ip_name TEXT NOT NULL,
    tracked_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
)""")
        conn.commit()
    except psycopg2.Error as e:
        log.debug(f"ip_names table creation: {e}")
        conn.rollback()
        conn = get_db()
        cur = conn.cursor()

    try:
        cur.execute("ALTER TABLE login_attempts ADD COLUMN username TEXT DEFAULT ''")
        conn.commit()
        log.info("Added username column to login_attempts table")
    except psycopg2.errors.DuplicateColumn:
        log.debug("username column already exists on login_attempts table")
        conn.rollback()
    except Exception as e:
        log.warning(f"Error adding username column to login_attempts: {e}")
        conn.rollback()
        try:
            conn = get_db()
            cur = conn.cursor()
        except:
            pass

    # Migrate IP names from blocked_ips to ip_names table
    try:
        cur.execute("""
INSERT INTO ip_names (ip_address, ip_name)
SELECT ip_address, ip_name FROM blocked_ips
WHERE ip_name IS NOT NULL AND ip_name != ''
ON CONFLICT (ip_address) DO NOTHING""")
        conn.commit()
    except psycopg2.Error as e:
        log.debug(f"IP names migration: {e}")
        conn.rollback()
        conn = get_db()
        cur = conn.cursor()

    try:
        cur.execute("ALTER TABLE projects ADD COLUMN hidden_from_parent BOOLEAN NOT NULL DEFAULT FALSE")
        conn.commit()
    except psycopg2.Error:
        conn.rollback()
        conn = get_db()
        cur = conn.cursor()

    try:
        cur.execute("ALTER TABLE tasks ADD COLUMN hidden_from_parent BOOLEAN NOT NULL DEFAULT FALSE")
        conn.commit()
    except psycopg2.Error:
        conn.rollback()
        conn = get_db()
        cur = conn.cursor()

    try:
        cur.execute("ALTER TABLE completions ADD COLUMN submitted BOOLEAN NOT NULL DEFAULT FALSE")
        conn.commit()
    except psycopg2.Error:
        conn.rollback()
        conn = get_db()
        cur = conn.cursor()

    # Add notes column to bucket_list if it doesn't exist yet
    try:
        cur.execute("ALTER TABLE bucket_list ADD COLUMN IF NOT EXISTS notes TEXT NOT NULL DEFAULT ''")
        conn.commit()
    except psycopg2.Error:
        conn.rollback()
        conn = get_db()
        cur = conn.cursor()

    # Add conversation_id to gmail_drafts for existing deployments
    try:
        cur.execute("ALTER TABLE gmail_drafts ADD COLUMN IF NOT EXISTS conversation_id TEXT NOT NULL DEFAULT ''")
        conn.commit()
    except psycopg2.Error:
        conn.rollback()
        conn = get_db()
        cur = conn.cursor()

    # Migration guard: access_requests.token_used for existing deployments
    try:
        cur.execute("ALTER TABLE access_requests ADD COLUMN IF NOT EXISTS token_used BOOLEAN NOT NULL DEFAULT FALSE")
        conn.commit()
    except psycopg2.Error:
        conn.rollback()
        conn = get_db()
        cur = conn.cursor()

    # Add promoted_to_task flag to canvas_assignments_cache for existing deployments
    try:
        cur.execute("ALTER TABLE canvas_assignments_cache ADD COLUMN IF NOT EXISTS promoted_to_task BOOLEAN NOT NULL DEFAULT FALSE")
        conn.commit()
    except psycopg2.Error:
        conn.rollback()
        conn = get_db()
        cur = conn.cursor()

    # ── SaaS: add user_id column to all per-user data tables ──────────────────────
    _user_id_tables = [
        "tasks", "recurring_tasks", "completions", "assignment_estimates",
        "projects", "project_tasks", "project_notes",
        "daily_plans", "daily_plan_items",
        "timer_state", "briefing_cache", "debrief_cache", "insight_cache",
        "chat_messages", "chat_summaries",
        "stock_transactions", "stock_notes",
        "bucket_list", "people_profiles", "gmail_drafts",
        "notification_log", "canvas_assignments_cache",
    ]
    for _tbl in _user_id_tables:
        try:
            cur.execute(f"ALTER TABLE {_tbl} ADD COLUMN IF NOT EXISTS user_id UUID")
            conn.commit()
        except psycopg2.Error:
            conn.rollback()
            conn = get_db()
            cur = conn.cursor()

    # Per-user config table (keeps global config table unchanged)
    try:
        cur.execute("""CREATE TABLE IF NOT EXISTS user_config (
            user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            key TEXT NOT NULL,
            value TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (user_id, key)
        )""")
        conn.commit()
    except psycopg2.Error:
        conn.rollback()
        conn = get_db()
        cur = conn.cursor()

    # Create unique indexes for singleton-style tables keyed by user_id
    for _idx_sql in [
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_timer_state_user ON timer_state(user_id) WHERE user_id IS NOT NULL",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_briefing_cache_user ON briefing_cache(user_id) WHERE user_id IS NOT NULL",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_debrief_cache_user ON debrief_cache(user_id) WHERE user_id IS NOT NULL",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_insight_cache_user ON insight_cache(user_id) WHERE user_id IS NOT NULL",
    ]:
        try:
            cur.execute(_idx_sql)
            conn.commit()
        except psycopg2.Error:
            conn.rollback()
            conn = get_db()
            cur = conn.cursor()

    # Initialize pricing_config singleton
    try:
        cur.execute("INSERT INTO pricing_config (id, stripe_price_id, monthly_cents) VALUES (1, '', 999) ON CONFLICT (id) DO NOTHING")
        conn.commit()
    except psycopg2.Error:
        conn.rollback()
        conn = get_db()
        cur = conn.cursor()

    # Migrate AVERAGE_USER to the users table and assign their UUID to existing rows
    _avg_user = os.environ.get("AVERAGE_USER", "user").strip()
    _app_pw   = os.environ.get("APP_PASSWORD", "").strip()
    if _avg_user and _app_pw:
        try:
            cur.execute("SELECT id FROM users WHERE username = %s", (_avg_user,))
            _existing = cur.fetchone()
            if not _existing:
                _uid = str(uuid.uuid4())
                cur.execute("""
INSERT INTO users (id, email, username, password_hash, display_name, is_comped, active)
VALUES (%s, %s, %s, %s, %s, TRUE, TRUE)
ON CONFLICT (username) DO NOTHING""",
                    (_uid, f"{_avg_user}@local.jarvis", _avg_user,
                     generate_password_hash(_app_pw), _avg_user.title()))
                conn.commit()
            cur.execute("SELECT id FROM users WHERE username = %s", (_avg_user,))
            _row = cur.fetchone()
            if _row:
                _avg_uuid = str(_row["id"])
                for _tbl in _user_id_tables:
                    try:
                        cur.execute(f"UPDATE {_tbl} SET user_id = %s WHERE user_id IS NULL", (_avg_uuid,))
                    except Exception:
                        conn.rollback()
                        conn = get_db()
                        cur = conn.cursor()
                        continue
                # Migrate global config to user_config for AVERAGE_USER
                try:
                    cur.execute("""
INSERT INTO user_config (user_id, key, value)
SELECT %s, key, value FROM config
ON CONFLICT (user_id, key) DO NOTHING""", (_avg_uuid,))
                except Exception:
                    conn.rollback()
                    conn = get_db()
                    cur = conn.cursor()
                conn.commit()
        except Exception as _e:
            log.warning("SaaS user migration: %s", _e)
            conn.rollback()
            conn = get_db()
            cur = conn.cursor()

    # Insert default config values
    defaults = {"name": "Jarvis", "morning_briefing_time": "07:00", "timer_cutoff_multiplier": "2.0", "anthropic_api_key": "", "weekly_recap_advisor": "Mr. Goldberg", "formal_signoff_name": "Finley Thomas", "app_mode": "school", "is_summer_school": "false", "has_summer_job": "false"}
    for k, v in defaults.items():
        try:
            cur.execute("INSERT INTO config (key, value) VALUES (%s, %s) ON CONFLICT (key) DO NOTHING", (k, v))
        except Exception:
            pass
    conn.commit()

    # Initialize singleton records
    try:
        cur.execute("INSERT INTO timer_state (id) VALUES (1) ON CONFLICT (id) DO NOTHING")
        cur.execute("INSERT INTO briefing_cache (id, content) VALUES (1, '') ON CONFLICT (id) DO NOTHING")
        cur.execute("INSERT INTO debrief_cache (id, content) VALUES (1, '') ON CONFLICT (id) DO NOTHING")
        cur.execute("INSERT INTO lockdown_state (id, is_locked_down) VALUES (1, FALSE) ON CONFLICT (id) DO NOTHING")
        conn.commit()
    except Exception as e:
        log.debug(f"Singleton records: {e}")
        conn.rollback()
        conn = get_db()
        cur = conn.cursor()

    # Create indexes
    indexes = ["CREATE INDEX IF NOT EXISTS idx_completions_assignment_title ON completions(assignment_title)", "CREATE INDEX IF NOT EXISTS idx_tasks_completed ON tasks(completed, created_at DESC)", "CREATE INDEX IF NOT EXISTS idx_projects_status ON projects(status)", "CREATE INDEX IF NOT EXISTS idx_project_tasks_assignee_status ON project_tasks(assignee, status)", "CREATE INDEX IF NOT EXISTS idx_tasks_due_date ON tasks(due_date)", "CREATE INDEX IF NOT EXISTS idx_completions_completed_at ON completions(completed_at DESC)", "CREATE INDEX IF NOT EXISTS idx_daily_plans_date ON daily_plans(plan_date)", "CREATE INDEX IF NOT EXISTS idx_daily_plan_items_plan_id ON daily_plan_items(plan_id)", "CREATE INDEX IF NOT EXISTS idx_daily_plan_items_completed ON daily_plan_items(completed)", "CREATE INDEX IF NOT EXISTS idx_login_attempts_ip ON login_attempts(ip_address, attempted_at DESC)", "CREATE INDEX IF NOT EXISTS idx_login_lockouts_ip ON login_lockouts(ip_address)", "CREATE INDEX IF NOT EXISTS idx_tasks_due_pending ON tasks(due_date) WHERE completed = FALSE", "CREATE INDEX IF NOT EXISTS idx_project_tasks_project ON project_tasks(project_id)"]
    for idx_sql in indexes:
        try:
            cur.execute(idx_sql)
        except Exception:
            pass
    conn.commit()

    cur.close()
    conn.close()
    log.info("Database initialized.")


_config_cache = None
_config_cache_ts = 0.0
_config_cache_lock = threading.Lock()
CONFIG_CACHE_TTL = 30  # seconds


def get_config():
    """Returns global config (used by scheduler and admin context)."""
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


def get_user_config(user_id=None):
    """Returns config for a specific student, falling back to global config for missing keys."""
    if not user_id:
        try:
            uid = session.get("user_id") if session else None
        except RuntimeError:
            uid = None
    else:
        uid = user_id
    if not uid:
        return get_config()
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT key, value FROM user_config WHERE user_id = %s", (uid,))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    result = dict(get_config())  # start with global defaults
    result.update({r["key"]: r["value"] for r in rows})  # overlay user-specific values
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


def set_user_config(updates, user_id=None):
    """Write config for a specific student."""
    if not user_id:
        try:
            uid = session.get("user_id") if session else None
        except RuntimeError:
            uid = None
    else:
        uid = user_id
    if not uid:
        return set_config(updates)
    conn = get_db()
    cur = conn.cursor()
    for k, v in updates.items():
        cur.execute("""
INSERT INTO user_config (user_id, key, value) VALUES (%s, %s, %s)
ON CONFLICT (user_id, key) DO UPDATE SET value = EXCLUDED.value""", (uid, k, str(v)))
    conn.commit()
    cur.close()
    conn.close()


# ── Mem0 long-term memory ──────────────────────────────────────────────────────
_mem0_client = None
_mem0_client_lock = threading.Lock()

def _get_mem0_client():
    """Return a cached Mem0 MemoryClient, or None if MEM0_API_KEY is not set."""
    global _mem0_client
    if not MEM0_API_KEY:
        return None
    with _mem0_client_lock:
        if _mem0_client is None:
            try:
                from mem0 import MemoryClient
                _mem0_client = MemoryClient(api_key=MEM0_API_KEY)
            except Exception as e:
                log.warning("Mem0 client init failed: %s", e)
                return None
        return _mem0_client


# ── Google OAuth2 helpers ──────────────────────────────────────────────────────

def _google_configured():
    return bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET)


def _get_google_credentials():
    """Return refreshed google.oauth2.credentials.Credentials, or None if not authorized."""
    if not _google_configured():
        return None
    refresh_token = get_config().get("google_refresh_token", "").strip()
    if not refresh_token:
        return None
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request as GoogleRequest
        creds = Credentials(
            token=None,
            refresh_token=refresh_token,
            client_id=GOOGLE_CLIENT_ID,
            client_secret=GOOGLE_CLIENT_SECRET,
            token_uri="https://oauth2.googleapis.com/token",
        )
        creds.refresh(GoogleRequest())
        return creds
    except Exception as e:
        log.warning("Google credentials refresh failed: %s", e)
        return None


def _google_client_config():
    redirect_uri = GOOGLE_REDIRECT_URI or "http://localhost:5000/google-auth/callback"
    return {
        "web": {
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [redirect_uri],
        }
    }


def _mem0_store_worker(user_content, assistant_content):
    """Background: send user+assistant exchange to Mem0 for memory extraction."""
    try:
        client = _get_mem0_client()
        if not client:
            return
        client.add(
            [
                {"role": "user", "content": user_content},
                {"role": "assistant", "content": assistant_content},
            ],
            user_id="student",
        )
    except Exception as e:
        log.debug("Mem0 store error: %s", e)


def _mem0_maybe_store_async(user_content, assistant_content):
    """Fire-and-forget: extract and store memories from a chat exchange."""
    if not MEM0_API_KEY or not user_content or not assistant_content:
        return
    t = threading.Thread(
        target=_mem0_store_worker,
        args=(user_content[:4000], assistant_content[:4000]),
        daemon=True,
    )
    t.start()


# ── iCal caching ──────────────────────────────────────────────────────────────
_ical_cache = {}  # url -> (monotonic_time, Calendar)
_ical_cache_lock = threading.Lock()
_ical_inflight = {}  # url -> threading.Event for request coalescing
_ical_last_error = {}  # url -> {"at": iso, "msg": str}
_ical_sync_lock = threading.Lock()
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
        with _ical_sync_lock:
            _ical_last_error[url] = {"at": datetime.now(TZ).isoformat(), "msg": str(e)}
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


# ── Simple TTL cache for JSON-returning external fetches ─────────────────────
_simple_cache = {}  # key -> (monotonic_time, value)
_simple_cache_lock = threading.Lock()


def _cache_get(key, ttl):
    with _simple_cache_lock:
        entry = _simple_cache.get(key)
        if entry and (time.monotonic() - entry[0] < ttl):
            return entry[1]
    return None


_SIMPLE_CACHE_MAX = 256


def _cache_set(key, value):
    with _simple_cache_lock:
        _simple_cache[key] = (time.monotonic(), value)
        if len(_simple_cache) > _SIMPLE_CACHE_MAX:
            # Evict the oldest entry by timestamp
            oldest_key = min(_simple_cache, key=lambda k: _simple_cache[k][0])
            _simple_cache.pop(oldest_key, None)


# ── Canvas REST API helpers ──────────────────────────────────────────────────
# Augment the iCal feed with course names, grades, and full assignment details.
# Silently no-ops when the user's Canvas API token or base URL is not configured.

CANVAS_COURSES_TTL = 3600          # 1 hour
CANVAS_GRADES_TTL = 600            # 10 minutes
CANVAS_ASSIGNMENT_TTL = 1800       # 30 minutes


def _canvas_configured():
    return bool(u_canvas_api_token() and u_canvas_base_url())


def _canvas_get(path, params=None, timeout=12):
    if not _canvas_configured():
        return None
    url = u_canvas_base_url() + (path if path.startswith("/") else "/" + path)
    headers = {"Authorization": "Bearer " + u_canvas_api_token(), "Accept": "application/json"}
    try:
        resp = requests.get(url, headers=headers, params=params or {}, timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        log.warning("Canvas API GET %s failed: %s", path, e)
        return None


def canvas_courses():
    cached = _cache_get("canvas:courses", CANVAS_COURSES_TTL)
    if cached is not None:
        return cached
    data = _canvas_get("/api/v1/courses", params={"enrollment_state": "active", "per_page": 50})
    if not isinstance(data, list):
        _cache_set("canvas:courses", [])
        return []
    courses = [
        {
            "id": c.get("id"),
            "name": c.get("name") or c.get("course_code") or "",
            "course_code": c.get("course_code") or "",
        }
        for c in data
        if isinstance(c, dict) and c.get("id")
    ]
    _cache_set("canvas:courses", courses)
    return courses


def canvas_grades():
    cached = _cache_get("canvas:grades", CANVAS_GRADES_TTL)
    if cached is not None:
        return cached
    courses = canvas_courses()
    course_name = {c["id"]: c["name"] for c in courses}
    data = _canvas_get(
        "/api/v1/users/self/enrollments",
        params={"state[]": "active", "type[]": "StudentEnrollment", "per_page": 50},
    )
    grades = []
    if isinstance(data, list):
        for e in data:
            if not isinstance(e, dict):
                continue
            cid = e.get("course_id")
            g = e.get("grades") or {}
            grades.append({
                "course_id": cid,
                "course": course_name.get(cid, ""),
                "current_grade": g.get("current_grade"),
                "current_score": g.get("current_score"),
                "final_grade": g.get("final_grade"),
                "final_score": g.get("final_score"),
            })
    _cache_set("canvas:grades", grades)
    return grades


def _strip_html(html):
    if not html:
        return ""
    text = re.sub(r"<br\s*/?>", "\n", html, flags=re.I)
    text = re.sub(r"</p>", "\n\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def canvas_assignment_detail(course_id, assignment_id):
    key = f"canvas:asgn:{course_id}:{assignment_id}"
    cached = _cache_get(key, CANVAS_ASSIGNMENT_TTL)
    if cached is not None:
        return cached
    a = _canvas_get(f"/api/v1/courses/{course_id}/assignments/{assignment_id}")
    if not isinstance(a, dict):
        _cache_set(key, None)
        return None
    detail = {
        "id": a.get("id"),
        "name": a.get("name") or "",
        "description": _strip_html(a.get("description") or "")[:6000],
        "due_at": a.get("due_at"),
        "points_possible": a.get("points_possible"),
        "submission_types": a.get("submission_types") or [],
        "html_url": a.get("html_url"),
        "rubric": [
            {
                "description": r.get("description"),
                "long_description": (r.get("long_description") or "")[:600],
                "points": r.get("points"),
            }
            for r in (a.get("rubric") or [])
            if isinstance(r, dict)
        ],
    }
    _cache_set(key, detail)
    return detail


def canvas_search_assignment(title_query):
    """Find a Canvas assignment matching `title_query` across active courses.

    Returns (course_id, assignment_id, course_name) for the best match, or None.
    """
    if not _canvas_configured() or not title_query:
        return None
    needle = title_query.strip().lower()
    if not needle:
        return None
    for course in canvas_courses():
        cid = course["id"]
        data = _canvas_get(
            f"/api/v1/courses/{cid}/assignments",
            params={"search_term": title_query[:80], "per_page": 20},
        )
        if not isinstance(data, list):
            continue
        # Prefer exact (case-insensitive) match, then prefix, then substring
        exact = next((a for a in data if (a.get("name") or "").strip().lower() == needle), None)
        if exact:
            return (cid, exact.get("id"), course["name"])
        prefix = next((a for a in data if (a.get("name") or "").strip().lower().startswith(needle)), None)
        if prefix:
            return (cid, prefix.get("id"), course["name"])
        sub = next((a for a in data if needle in (a.get("name") or "").strip().lower()), None)
        if sub:
            return (cid, sub.get("id"), course["name"])
    return None


# ── PowerSchool Scraper (Playwright + Claude Vision) ──────────────────────────
# Uses a headless Chromium browser to log in as a real user, screenshots the
# grades page, then sends the image to Claude vision for extraction.
# No HTML parsing — works regardless of PowerSchool's JS rendering.

PS_GRADES_TTL     = 1800   # 30 minutes — screenshot + vision result lifetime
PS_ATTENDANCE_TTL = 3600   # 1 hour


def _ps_configured():
    return bool(POWER_USERN and POWER_PASS)


def _ps_ask_claude(content: list) -> dict:
    """Send content blocks to Claude Haiku and parse the JSON grade/attendance result."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return {"error": "ANTHROPIC_API_KEY not set"}
    try:
        client = anthropic.Anthropic(api_key=api_key)
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1500,
            messages=[{"role": "user", "content": content}],
        )
        raw = resp.content[0].text.strip()
        log.info("PowerSchool Claude response: %s", raw[:300])
        m = re.search(r'\{.*\}', raw, re.DOTALL)
        if not m:
            return {"error": "No JSON in response", "raw": raw[:500]}
        data = json.loads(m.group())
        return {"grades": data.get("grades", []), "attendance": data.get("attendance", {})}
    except Exception as e:
        log.warning("PowerSchool: Claude API error — %s", e)
        return {"error": str(e)}


_PS_EXTRACT_PROMPT = (
    "This is a PowerSchool student portal page showing grades and attendance. "
    "Extract every course visible. "
    "Return ONLY valid JSON — no markdown, no explanation:\n"
    '{"grades":[{"course":"...","teacher":"...","grade_letter":"A","grade_pct":95.2,"absences":"0"}],'
    '"attendance":{"absences":0,"tardies":0}}'
)


def _ps_extract_via_playwright() -> dict:
    """Login with headless Chromium, screenshot the page, send to Claude vision."""
    import base64
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    except ImportError:
        return {"error": "playwright_not_installed"}

    screenshot_b64 = None
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"]
            )
            page = browser.new_context(viewport={"width": 1280, "height": 900}).new_page()

            log.info("PowerSchool (playwright): navigating to login page")
            page.goto(f"{PS_BASE_URL}/public/", timeout=30000)
            page.wait_for_load_state("domcontentloaded")

            page.fill('input[name="account"], input[id="fieldAccount"]', POWER_USERN, timeout=10000)
            page.fill('input[name="ldappassword"], input[id="fieldPassword"], input[type="password"]',
                      POWER_PASS, timeout=10000)
            page.click('input[type="submit"], button[type="submit"]', timeout=10000)
            page.wait_for_load_state("networkidle", timeout=30000)

            final_url = page.url
            log.info("PowerSchool (playwright): post-login URL = %s", final_url)

            if "/public/" in final_url and "home" not in final_url.lower():
                err_el = page.locator("#LoginErrorMessages, .feedback-alert").first
                err_txt = err_el.inner_text() if err_el.count() else "(no error element)"
                log.warning("PowerSchool (playwright): login failed — %s", err_txt)
                browser.close()
                return {"error": f"Login failed: {err_txt}"}

            screenshot_b64 = base64.b64encode(page.screenshot(full_page=True)).decode()
            log.info("PowerSchool (playwright): screenshot taken")
            browser.close()

    except Exception as e:
        log.warning("PowerSchool (playwright): browser error — %s", e)
        return {"error": str(e)}

    return _ps_ask_claude([
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": screenshot_b64}},
        {"type": "text", "text": _PS_EXTRACT_PROMPT},
    ])


def _ps_extract_via_requests() -> dict:
    """
    Fallback when Playwright isn't available: login with requests, send the raw
    HTML to Claude as text. Claude reads HTML structure just as well as a screenshot.
    """
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return {"error": "beautifulsoup4 not installed"}

    sess = requests.Session()
    sess.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    })

    # GET login page and collect all hidden form fields
    try:
        r1 = sess.get(f"{PS_BASE_URL}/public/", timeout=20)
        r1.raise_for_status()
    except Exception as e:
        return {"error": f"Could not reach PowerSchool: {e}"}

    soup = BeautifulSoup(r1.text, "html.parser")
    form = soup.find("form", id="LoginForm") or soup.find("form")
    if not form:
        return {"error": "No login form found on /public/"}

    action = (form.get("action") or "/public/").strip()
    if not action.startswith("http"):
        action = PS_BASE_URL + ("" if action.startswith("/") else "/") + action

    # Echo all hidden inputs back, then overlay credentials
    payload: dict = {
        inp.get("name"): inp.get("value") or ""
        for inp in form.find_all("input")
        if inp.get("name")
    }
    pstoken = payload.get("pstoken", "")
    import hashlib
    def _md5(s): return hashlib.md5(s.encode()).hexdigest()
    pw_hash = _md5(POWER_USERN.lower() + ":" + _md5(POWER_PASS) + ":" + pstoken)
    payload.update({
        "account": POWER_USERN,
        "ldappassword": POWER_PASS,
        "pw": pw_hash,
        "dbpw": pw_hash,
    })

    log.info("PowerSchool (requests): POSTing login to %s", action)
    try:
        r2 = sess.post(action, data=payload, timeout=20, allow_redirects=True)
        r2.raise_for_status()
    except Exception as e:
        return {"error": f"Login POST failed: {e}"}

    home_url = r2.url
    log.info("PowerSchool (requests): post-login URL = %s", home_url)

    # Check we're not still on the login page
    lower = r2.text.lower()
    still_login = 'name="account"' in lower or 'id="fieldaccount"' in lower
    if still_login:
        return {"error": "Login failed — still on login page after POST. Check POWER_USERN / POWER_PASS."}

    # Try the landing URL, then guardian/home.html as fallback
    html = r2.text
    if len(html) < 2000 or "grades" not in html.lower():
        try:
            r3 = sess.get(f"{PS_BASE_URL}/guardian/home.html", timeout=20)
            if len(r3.text) > len(html):
                html = r3.text
                home_url = r3.url
        except Exception:
            pass

    log.info("PowerSchool (requests): sending %d chars of HTML to Claude", len(html))

    # Strip scripts/styles to reduce token count, keep the visible structure
    for tag in BeautifulSoup(html, "html.parser").find_all(["script", "style", "noscript"]):
        tag.decompose()
    clean_html = str(BeautifulSoup(html, "html.parser"))[:18000]

    return _ps_ask_claude([{
        "type": "text",
        "text": (
            "Here is the HTML source of a PowerSchool student portal page. "
            "Extract every course grade and attendance data visible. "
            "Return ONLY valid JSON — no markdown, no explanation:\n"
            '{"grades":[{"course":"...","teacher":"...","grade_letter":"A","grade_pct":95.2,"absences":"0"}],'
            '"attendance":{"absences":0,"tardies":0}}\n\n'
            "HTML:\n" + clean_html
        ),
    }])


def _ps_screenshot_and_extract() -> dict:
    """
    Extract grades and attendance from PowerSchool.
    Tries Playwright (screenshot → vision) first; falls back to requests (HTML → text).
    Returns {"grades": [...], "attendance": {...}} or {"error": "..."}.
    """
    if not _ps_configured():
        return {"error": "POWER_USERN / POWER_PASS not configured"}

    result = _ps_extract_via_playwright()
    if result.get("error") == "playwright_not_installed":
        log.info("PowerSchool: playwright not available, falling back to requests+HTML")
        result = _ps_extract_via_requests()

    return result


def _ps_is_login_page(html: str) -> bool:
    """Return True if the HTML looks like the PS login page (not authenticated)."""
    lower = html.lower()
    return (
        'name="account"' in lower
        or 'id="fieldaccount"' in lower
        or 'name="ldappassword"' in lower
        or "/public/home.html" in lower
        and 'name="pstoken"' in lower
    )


def _ps_login():
    """
    Authenticate to PowerSchool. Returns (session, home_url) or (None, "").

    Key fixes vs the previous version:
    - Captures ALL hidden form inputs (contextData, credentialType, ssononce, …)
      and echoes them back — required by modern PowerSchool's RSA login flow.
    - Posts to the form's actual action URL, not a hard-coded path.
    - Only returns a session when login is confirmed; raises on failure so the
      caller can treat a returned session as guaranteed-authenticated.
    """
    if not _ps_configured():
        return None, ""
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        log.warning("PowerSchool: beautifulsoup4 not installed — pip install beautifulsoup4")
        return None, ""

    sess = requests.Session()
    sess.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    })

    # ── Step 1: GET login page ──────────────────────────────────────────────
    try:
        r1 = sess.get(f"{PS_BASE_URL}/public/", timeout=20)
        r1.raise_for_status()
    except Exception as e:
        log.warning("PowerSchool: could not reach login page: %s", e)
        return None, ""

    soup = BeautifulSoup(r1.text, "html.parser")

    # Find the login form (may be id="LoginForm" or the first <form>)
    form = soup.find("form", id="LoginForm") or soup.find("form")
    if not form:
        log.warning("PowerSchool: no <form> found on login page (body preview: %s)",
                    r1.text[:300])
        return None, ""

    # Determine POST target from the form's action attribute
    action = (form.get("action") or "/public/").strip()
    if not action.startswith("http"):
        action = PS_BASE_URL + ("" if action.startswith("/") else "/") + action
    log.info("PowerSchool: login form action = %s", action)

    # ── Step 2: Collect ALL hidden inputs, then overlay credentials ─────────
    # This is the critical fix: modern PS requires contextData, credentialType,
    # ssononce, etc. to be echoed back exactly as received.
    payload: dict = {}
    for inp in form.find_all("input"):
        name = inp.get("name", "")
        if not name:
            continue
        payload[name] = inp.get("value") or ""

    pstoken = payload.get("pstoken", "")
    pw_hash = _ps_md5(POWER_USERN.lower() + ":" + _ps_md5(POWER_PASS) + ":" + pstoken)

    # Overlay the credential fields
    payload.update({
        "account":      POWER_USERN,
        "ldappassword": POWER_PASS,   # plaintext — used for LDAP / district SSO
        "pw":           pw_hash,       # MD5 hash — used for local PS accounts
        "dbpw":         pw_hash,
        "returnTo":     payload.get("returnTo", ""),
    })

    log.info("PowerSchool: POSTing login (fields: %s)", ", ".join(sorted(payload.keys())))

    # ── Step 3: POST login ──────────────────────────────────────────────────
    try:
        r2 = sess.post(action, data=payload, timeout=20, allow_redirects=True)
        r2.raise_for_status()
    except Exception as e:
        log.warning("PowerSchool: login POST failed: %s", e)
        return None, ""

    home_url = r2.url
    log.info("PowerSchool: login POST → final URL = %s  status = %s", home_url, r2.status_code)

    # ── Step 4: Verify we are NOT still on the login page ──────────────────
    if _ps_is_login_page(r2.text):
        # Try to surface an error message from the page
        err_el = (
            soup.find(id="LoginErrorMessages")
            or soup.find(class_=re.compile(r"error|alert", re.I))
        )
        err_txt = err_el.get_text(" ", strip=True)[:200] if err_el else "(no error element found)"
        log.warning("PowerSchool: login failed — still on login page. err=%s", err_txt)
        return None, ""

    log.info("PowerSchool: login succeeded, home = %s", home_url)
    return sess, home_url


def _ps_get_session():
    """Return (cached_session, home_url), re-logging-in if the cache expired."""
    now = time.monotonic()
    with _ps_session_lock:
        if _ps_session_cache["session"] and now < _ps_session_cache["expires"]:
            return _ps_session_cache["session"], _ps_session_cache["home_url"]
        sess, home_url = _ps_login()
        _ps_session_cache["session"]  = sess
        _ps_session_cache["home_url"] = home_url
        # Cache for 20 min — PS sessions typically last ~30 min
        _ps_session_cache["expires"]  = now + 1200
        return sess, home_url


def _ps_invalidate_session():
    with _ps_session_lock:
        _ps_session_cache["session"]  = None
        _ps_session_cache["home_url"] = ""
        _ps_session_cache["expires"]  = 0


def _ps_parse_grades(html: str, source_url: str) -> list:
    """
    Parse grades out of a PowerSchool guardian/home page.

    PowerSchool renders one table row per course. The grade for the current
    term is a link to /guardian/scores.html and typically reads "A (95.2%)"
    or just "95.2" depending on the display setting.
    """
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")

    if _ps_is_login_page(html):
        log.warning("PowerSchool _ps_parse_grades: received login page — session expired?")
        return []

    # Find the table that contains links to scores.html
    main_table = None
    for tbl in soup.find_all("table"):
        if tbl.find("a", href=lambda h: h and "scores.html" in (h or "")):
            main_table = tbl
            break

    # Fallback: any table whose cells contain letter-grade-like content
    if not main_table:
        grade_pat = re.compile(r"^\s*[A-F][+-]?\s*$")
        for tbl in soup.find_all("table"):
            cells = tbl.find_all("td")
            if any(grade_pat.match(c.get_text()) for c in cells[:60]):
                main_table = tbl
                break

    if not main_table:
        log.warning("PowerSchool: no grades table found in %s (body length %d)",
                    source_url, len(html))
        log.debug("PowerSchool page preview: %s", html[:800])
        return []

    letter_re = re.compile(r"^[A-F][+-]?$")
    pct_re    = re.compile(r"^(\d{1,3}(?:\.\d+)?)%?$")
    grades    = []

    for row in main_table.find_all("tr"):
        cells = row.find_all(["td", "th"])
        if len(cells) < 3 or cells[0].name == "th":
            continue

        # Course name — first cell, prefer the link text if it points to scores.html
        course_link = cells[0].find("a", href=lambda h: h and "scores.html" in (h or ""))
        course_name = (course_link or cells[0]).get_text(strip=True)
        if not course_name:
            continue

        teacher = cells[1].get_text(strip=True)

        grade_letter, grade_pct, grade_url, absences = "", None, "", ""

        for cell in cells[2:]:
            a = cell.find("a", href=lambda h: h and "scores.html" in (h or ""))
            if a:
                raw  = a.get_text(strip=True)
                href = a.get("href", "")
                grade_url = (PS_BASE_URL + href) if href.startswith("/") else href

                # "A (95.2%)" → letter="A", pct=95.2
                m = re.match(r"^([A-F][+-]?)\s*\((\d{1,3}(?:\.\d+)?)%?\)$", raw)
                if m:
                    grade_letter = m.group(1)
                    grade_pct    = float(m.group(2))
                elif letter_re.match(raw):
                    grade_letter = raw
                elif pct_re.match(raw):
                    grade_pct = float(pct_re.match(raw).group(1))
                break

            # Bare cell fallback
            ct = cell.get_text(strip=True)
            if letter_re.match(ct) and not grade_letter:
                grade_letter = ct
            elif pct_re.match(ct) and grade_pct is None:
                grade_pct = float(pct_re.match(ct).group(1))

        # Absences column — last numeric-only cell that isn't the grade
        last = cells[-1].get_text(strip=True)
        if re.match(r"^\d+$", last) and last != grade_letter:
            absences = last

        if grade_letter or grade_pct is not None:
            grades.append({
                "course":       course_name,
                "teacher":      teacher,
                "grade_letter": grade_letter,
                "grade_pct":    grade_pct,
                "grade_url":    grade_url,
                "absences":     absences,
            })

    return grades


def _ps_fetch_data() -> dict:
    """Run screenshot + vision extraction, caching the combined result for 30 min."""
    cached = _cache_get("ps:data", PS_GRADES_TTL)
    if cached is not None:
        return cached
    if not _ps_configured():
        return {}
    result = _ps_screenshot_and_extract()
    if "error" not in result:
        _cache_set("ps:data", result)
    return result


def ps_grades() -> list:
    return _ps_fetch_data().get("grades", [])


def ps_attendance() -> dict:
    return _ps_fetch_data().get("attendance", {})


def ps_refresh_cache():
    """Bust the cache and re-run the screenshot + vision extraction."""
    with _simple_cache_lock:
        _simple_cache.pop("ps:data", None)
    return ps_grades()


# Park City, UT
PARK_CITY_LAT = 40.6461
PARK_CITY_LON = -111.4980

# Weather code → short description (subset of WMO codes)
_WEATHER_CODES = {
    0: "Clear", 1: "Mostly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Fog", 48: "Freezing fog",
    51: "Light drizzle", 53: "Drizzle", 55: "Heavy drizzle",
    61: "Light rain", 63: "Rain", 65: "Heavy rain",
    71: "Light snow", 73: "Snow", 75: "Heavy snow", 77: "Snow grains",
    80: "Rain showers", 81: "Heavy showers", 82: "Violent showers",
    85: "Snow showers", 86: "Heavy snow showers",
    95: "Thunderstorm", 96: "Thunderstorm w/ hail", 99: "Severe thunderstorm",
}


def fetch_weather():
    """Park City weather via Open-Meteo. Returns a small dict or None."""
    cached = _cache_get("weather:park_city", 1800)  # 30 min
    if cached is not None:
        return cached
    try:
        url = (
            "https://api.open-meteo.com/v1/forecast"
            "?latitude=%s&longitude=%s"
            "&current=temperature_2m,weather_code,wind_speed_10m,apparent_temperature"
            "&daily=weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max"
            "&forecast_days=7"
            "&temperature_unit=fahrenheit&wind_speed_unit=mph"
            "&timezone=America/Denver"
        ) % (PARK_CITY_LAT, PARK_CITY_LON)
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        j = resp.json()
        cur = j.get("current", {}) or {}
        daily = j.get("daily", {}) or {}
        code = int(cur.get("weather_code", 0) or 0)
        out = {
            "current_f": round(float(cur.get("temperature_2m", 0) or 0)),
            "feels_like_f": round(float(cur.get("apparent_temperature", 0) or 0)),
            "wind_mph": round(float(cur.get("wind_speed_10m", 0) or 0)),
            "code": code,
            "description": _WEATHER_CODES.get(code, "—"),
            "daily": [],
        }
        days = daily.get("time", []) or []
        for i, d in enumerate(days[:7]):
            try:
                dcode = int((daily.get("weather_code") or [0])[i])
            except Exception:
                dcode = 0
            out["daily"].append({
                "date": d,
                "hi_f": round(float((daily.get("temperature_2m_max") or [0])[i])),
                "lo_f": round(float((daily.get("temperature_2m_min") or [0])[i])),
                "code": dcode,
                "description": _WEATHER_CODES.get(dcode, "—"),
                "precip_pct": int((daily.get("precipitation_probability_max") or [0])[i] or 0),
            })
        _cache_set("weather:park_city", out)
        return out
    except Exception as e:
        log.warning("Open-Meteo fetch failed: %s", e)
        return None


def fetch_finnhub_quote(symbol):
    """Returns {price, prev_close, day_change_pct} or None."""
    if not FINNHUB_API_KEY or not symbol:
        return None
    key = "finnhub:quote:" + symbol.upper()
    cached = _cache_get(key, 300)  # 5 min
    if cached is not None:
        return cached
    try:
        url = "https://finnhub.io/api/v1/quote?symbol=%s&token=%s" % (symbol.upper(), FINNHUB_API_KEY)
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        j = resp.json() or {}
        price = float(j.get("c") or 0)
        prev_close = float(j.get("pc") or 0)
        if price <= 0:
            return None
        change_pct = ((price - prev_close) / prev_close * 100.0) if prev_close else 0.0
        out = {
            "price": round(price, 4),
            "prev_close": round(prev_close, 4),
            "day_change_pct": round(change_pct, 2),
            "day_change": round(price - prev_close, 4),
        }
        _cache_set(key, out)
        return out
    except Exception as e:
        log.warning("Finnhub quote failed for %s: %s", symbol, e)
        return None


def fetch_stock_history(symbol, range_key="1mo"):
    """Daily closes for a chart. Tries Yahoo chart endpoint (no key).
    Returns [{date, close}] or []."""
    if not symbol:
        return []
    valid_ranges = {"5d", "1mo", "3mo", "6mo", "1y"}
    if range_key not in valid_ranges:
        range_key = "1mo"
    key = "yahoo:chart:%s:%s" % (symbol.upper(), range_key)
    cached = _cache_get(key, 600)  # 10 min
    if cached is not None:
        return cached
    try:
        url = "https://query1.finance.yahoo.com/v8/finance/chart/%s?range=%s&interval=1d" % (
            symbol.upper(), range_key
        )
        resp = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        j = resp.json() or {}
        result = (j.get("chart") or {}).get("result") or []
        if not result:
            return []
        r = result[0]
        timestamps = r.get("timestamp") or []
        closes = (((r.get("indicators") or {}).get("quote") or [{}])[0]).get("close") or []
        out = []
        for ts, cl in zip(timestamps, closes):
            if cl is None:
                continue
            d = datetime.fromtimestamp(ts, tz=ZoneInfo("UTC")).astimezone(TZ).date()
            out.append({"date": d.isoformat(), "close": round(float(cl), 4)})
        _cache_set(key, out)
        return out
    except Exception as e:
        log.warning("Yahoo chart failed for %s: %s", symbol, e)
        return []


def fetch_finnhub_profile(symbol):
    """Company profile: name, industry, country, market cap, logo, weburl."""
    if not FINNHUB_API_KEY or not symbol:
        return None
    key = "finnhub:profile:" + symbol.upper()
    cached = _cache_get(key, 24 * 3600)
    if cached is not None:
        return cached
    try:
        url = "https://finnhub.io/api/v1/stock/profile2?symbol=%s&token=%s" % (symbol.upper(), FINNHUB_API_KEY)
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        j = resp.json() or {}
        if not j.get("name"):
            return None
        out = {
            "name": j.get("name") or "",
            "industry": j.get("finnhubIndustry") or "",
            "country": j.get("country") or "",
            "market_cap": j.get("marketCapitalization"),
            "share_outstanding": j.get("shareOutstanding"),
            "ipo": j.get("ipo") or "",
            "weburl": j.get("weburl") or "",
            "logo": j.get("logo") or "",
            "exchange": j.get("exchange") or "",
            "ticker": j.get("ticker") or symbol.upper(),
        }
        _cache_set(key, out)
        return out
    except Exception as e:
        log.warning("Finnhub profile failed for %s: %s", symbol, e)
        return None


def fetch_finnhub_recommendation(symbol):
    """Analyst recommendation trends — most recent entry only."""
    if not FINNHUB_API_KEY or not symbol:
        return None
    key = "finnhub:reco:" + symbol.upper()
    cached = _cache_get(key, 6 * 3600)
    if cached is not None:
        return cached
    try:
        url = "https://finnhub.io/api/v1/stock/recommendation?symbol=%s&token=%s" % (symbol.upper(), FINNHUB_API_KEY)
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        arr = resp.json() or []
        if not isinstance(arr, list) or not arr:
            return None
        r = arr[0]
        out = {
            "period": r.get("period", ""),
            "strong_buy": int(r.get("strongBuy") or 0),
            "buy": int(r.get("buy") or 0),
            "hold": int(r.get("hold") or 0),
            "sell": int(r.get("sell") or 0),
            "strong_sell": int(r.get("strongSell") or 0),
        }
        out["total"] = out["strong_buy"] + out["buy"] + out["hold"] + out["sell"] + out["strong_sell"]
        _cache_set(key, out)
        return out
    except Exception as e:
        log.warning("Finnhub recommendation failed for %s: %s", symbol, e)
        return None


def fetch_finnhub_company_news(symbol, days_back=14):
    """Recent company news headlines from Finnhub."""
    if not FINNHUB_API_KEY or not symbol:
        return []
    key = "finnhub:news:%s:%d" % (symbol.upper(), days_back)
    cached = _cache_get(key, 3600)
    if cached is not None:
        return cached
    try:
        _to = datetime.now(TZ).date()
        _from = _to - timedelta(days=days_back)
        url = (
            "https://finnhub.io/api/v1/company-news?symbol=%s&from=%s&to=%s&token=%s"
            % (symbol.upper(), _from.isoformat(), _to.isoformat(), FINNHUB_API_KEY)
        )
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        arr = resp.json() or []
        out = []
        for n in arr[:8]:
            out.append({
                "headline": (n.get("headline") or "")[:200],
                "summary": (n.get("summary") or "")[:400],
                "source": n.get("source") or "",
                "url": n.get("url") or "",
                "datetime": n.get("datetime"),
            })
        _cache_set(key, out)
        return out
    except Exception as e:
        log.warning("Finnhub company-news failed for %s: %s", symbol, e)
        return []


# ── Stock notes (buy thesis / exit criteria) ─────────────────────────────────

def get_all_stock_notes():
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT symbol, thesis, exit_criteria, target_price, stop_loss, updated_at "
            "FROM stock_notes ORDER BY symbol"
        )
        out = []
        for r in cur.fetchall():
            out.append({
                "symbol": r["symbol"],
                "thesis": r["thesis"] or "",
                "exit_criteria": r["exit_criteria"] or "",
                "target_price": float(r["target_price"]) if r["target_price"] is not None else None,
                "stop_loss": float(r["stop_loss"]) if r["stop_loss"] is not None else None,
                "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
            })
        return out
    finally:
        cur.close()
        conn.close()


def upsert_stock_note(symbol, thesis=None, exit_criteria=None, target_price=None, stop_loss=None):
    symbol = (symbol or "").strip().upper()[:16]
    if not symbol:
        return None
    conn = get_db()
    cur = conn.cursor()
    try:
        # Merge with existing row so a partial update preserves the other fields.
        cur.execute("SELECT thesis, exit_criteria, target_price, stop_loss FROM stock_notes WHERE symbol=%s", (symbol,))
        row = cur.fetchone()
        if row:
            new_thesis = thesis if thesis is not None else (row["thesis"] or "")
            new_exit = exit_criteria if exit_criteria is not None else (row["exit_criteria"] or "")
            new_target = target_price if target_price is not None else row["target_price"]
            new_stop = stop_loss if stop_loss is not None else row["stop_loss"]
        else:
            new_thesis = thesis or ""
            new_exit = exit_criteria or ""
            new_target = target_price
            new_stop = stop_loss
        cur.execute(
            "INSERT INTO stock_notes (symbol, thesis, exit_criteria, target_price, stop_loss, updated_at) "
            "VALUES (%s, %s, %s, %s, %s, NOW()) "
            "ON CONFLICT (symbol) DO UPDATE SET thesis=EXCLUDED.thesis, exit_criteria=EXCLUDED.exit_criteria, "
            "target_price=EXCLUDED.target_price, stop_loss=EXCLUDED.stop_loss, updated_at=NOW()",
            (symbol, new_thesis[:4000], new_exit[:4000], new_target, new_stop)
        )
        conn.commit()
        return {"symbol": symbol, "thesis": new_thesis, "exit_criteria": new_exit,
                "target_price": new_target, "stop_loss": new_stop}
    finally:
        cur.close()
        conn.close()


# News feeds: right-leaning national + Park City local
_NEWS_NATIONAL_FEEDS = [
    ("Fox News", "https://moxie.foxnews.com/google-publisher/latest.xml"),
    ("New York Post", "https://nypost.com/feed/"),
    ("Washington Examiner", "https://www.washingtonexaminer.com/tag/news.rss"),
    ("Daily Wire", "https://www.dailywire.com/feeds/rss.xml"),
]
_NEWS_LOCAL_FEEDS = [
    ("Park Record", "https://www.parkrecord.com/news/feed/"),
    ("TownLift", "https://townlift.com/feed/"),
]


def _parse_feed_stdlib(outlet, url):
    """Minimal RSS/Atom parser using stdlib only (fallback when feedparser is unavailable)."""
    import xml.etree.ElementTree as ET
    import re as _re
    try:
        resp = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0 Jarvis Student AI"})
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
    except Exception as e:
        log.warning("stdlib RSS fetch failed for %s: %s", url, e)
        return []

    def _text(elem, tag, ns=""):
        if elem is None:
            return ""
        for child in elem:
            stripped = child.tag.split("}", 1)[-1]
            if stripped == tag:
                return (child.text or "").strip()
        return ""

    items = []
    # RSS <channel><item>
    channel = None
    for child in root:
        if child.tag.split("}", 1)[-1] == "channel":
            channel = child
            break
    candidates = []
    if channel is not None:
        for c in channel:
            if c.tag.split("}", 1)[-1] == "item":
                candidates.append(c)
    # Atom <entry>
    if not candidates:
        for c in root:
            if c.tag.split("}", 1)[-1] == "entry":
                candidates.append(c)

    for entry in candidates[:8]:
        title = _text(entry, "title")
        link = _text(entry, "link")
        if not link:
            # Atom <link href="...">
            for c in entry:
                if c.tag.split("}", 1)[-1] == "link":
                    link = (c.get("href") or c.text or "").strip()
                    if link:
                        break
        summary_raw = _text(entry, "description") or _text(entry, "summary")
        summary = _re.sub(r"<[^>]+>", "", summary_raw)[:400].strip()
        published = _text(entry, "pubDate") or _text(entry, "published") or _text(entry, "updated")
        if title and link:
            items.append({
                "title": title,
                "outlet": outlet,
                "url": link,
                "summary": summary,
                "published": published,
            })
    return items


def _parse_feed(outlet, url):
    """Return list of {title, outlet, url, summary, published} from one feed."""
    key = "rss:" + url
    cached = _cache_get(key, 900)  # 15 min
    if cached is not None:
        return cached
    items = []
    if feedparser is not None:
        try:
            parsed = feedparser.parse(url, agent="Mozilla/5.0 Jarvis Student AI")
            for entry in (parsed.entries or [])[:8]:
                title = (entry.get("title") or "").strip()
                link = (entry.get("link") or "").strip()
                summary_raw = (entry.get("summary") or entry.get("description") or "").strip()
                import re as _re
                summary = _re.sub(r"<[^>]+>", "", summary_raw)[:400].strip()
                published = entry.get("published") or entry.get("updated") or ""
                if title and link:
                    items.append({
                        "title": title,
                        "outlet": outlet,
                        "url": link,
                        "summary": summary,
                        "published": published,
                    })
        except Exception as e:
            log.warning("feedparser failed for %s: %s — falling back to stdlib", url, e)
            items = []
    if not items:
        items = _parse_feed_stdlib(outlet, url)
    _cache_set(key, items)
    return items


_STOPWORDS = {
    "the","a","an","and","or","but","of","in","on","at","for","to","from","by",
    "with","as","is","are","was","were","be","been","being","it","its","this",
    "that","these","those","his","her","their","our","you","your","we","they",
    "i","me","my","will","would","can","could","should","may","might","shall",
    "has","have","had","do","does","did","not","no","so","than","then","after",
    "before","about","into","over","under","up","down","out","off","says","said",
    "new","who","what","when","where","why","how","one","two","three","more",
}


def _extract_keywords(text, limit=8):
    """Lowercased, stopword-filtered, de-duped word tokens for preference learning."""
    import re as _re
    tokens = _re.findall(r"[a-zA-Z][a-zA-Z'\-]{2,}", (text or "").lower())
    seen = set()
    out = []
    for t in tokens:
        if t in _STOPWORDS or t in seen:
            continue
        seen.add(t)
        out.append(t)
        if len(out) >= limit:
            break
    return out


def _load_news_preferences():
    """Return a profile dict: {outlet_score: {...}, liked_keywords: set, disliked_keywords: set, disliked_urls: set}."""
    profile = {"outlet_score": {}, "liked_keywords": set(), "disliked_keywords": set(), "disliked_urls": set()}
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            "SELECT outlet, rating, COUNT(*) AS n FROM news_preferences GROUP BY outlet, rating"
        )
        for r in cur.fetchall():
            outlet = r["outlet"] or ""
            rating = int(r["rating"])
            n = int(r["n"] or 0)
            profile["outlet_score"][outlet] = profile["outlet_score"].get(outlet, 0) + (rating * n)
        cur.execute("SELECT rating, keywords, url_hash, url FROM news_preferences ORDER BY created_at DESC LIMIT 200")
        for r in cur.fetchall():
            words = (r["keywords"] or "").split()
            target = profile["liked_keywords"] if int(r["rating"]) == 1 else profile["disliked_keywords"]
            for w in words:
                target.add(w)
            if int(r["rating"]) == -1:
                profile["disliked_urls"].add(r["url_hash"])
        cur.close()
        conn.close()
    except Exception as e:
        log.warning("load news preferences failed: %s", e)
    return profile


def _score_news_item(item, profile):
    """Positive = more relevant; negative = less relevant."""
    score = 0.0
    outlet_score = profile["outlet_score"].get(item.get("outlet", ""), 0)
    # Cap outlet influence so one liked outlet doesn't monopolise the feed
    score += max(-3.0, min(3.0, outlet_score * 1.0))
    text = (item.get("title", "") + " " + item.get("summary", "")).lower()
    for kw in profile["liked_keywords"]:
        if kw and kw in text:
            score += 0.25
    for kw in profile["disliked_keywords"]:
        if kw and kw in text:
            score -= 0.25
    return score


def fetch_news(bucket="national", limit=3):
    feeds = _NEWS_NATIONAL_FEEDS if bucket == "national" else _NEWS_LOCAL_FEEDS
    all_items = []
    for outlet, url in feeds:
        all_items.extend(_parse_feed(outlet, url))

    profile = _load_news_preferences()

    # Filter out stories the student has explicitly disliked
    filtered = []
    for it in all_items:
        url_hash = hashlib.sha256(((it.get("url") or it.get("title") or "")).encode("utf-8")).hexdigest()[:32]
        if url_hash in profile["disliked_urls"]:
            continue
        it["_pref_score"] = _score_news_item(it, profile)
        filtered.append(it)

    # Round-robin across outlets, but within each outlet sort by preference score
    by_outlet = {}
    for item in filtered:
        by_outlet.setdefault(item["outlet"], []).append(item)
    for outlet in by_outlet:
        by_outlet[outlet].sort(key=lambda x: x.get("_pref_score", 0), reverse=True)

    # Outlet round-robin order: highest-score outlets first so a preferred outlet
    # contributes its top story before less-preferred outlets.
    outlet_order = sorted(
        by_outlet.keys(),
        key=lambda o: profile["outlet_score"].get(o, 0),
        reverse=True,
    )

    out = []
    i = 0
    while len(out) < limit and any(by_outlet.values()):
        for outlet in outlet_order:
            lst = by_outlet.get(outlet) or []
            if not lst:
                continue
            out.append(lst.pop(0))
            if len(out) >= limit:
                break
        i += 1
        if i > 20:
            break
    for it in out:
        it.pop("_pref_score", None)
    return out


def fetch_quote_of_day():
    """ZenQuotes (no key). 24h cache."""
    cached = _cache_get("zenquotes:today", 24 * 3600)
    if cached is not None:
        return cached
    try:
        resp = requests.get("https://zenquotes.io/api/today", timeout=10)
        resp.raise_for_status()
        j = resp.json() or []
        if isinstance(j, list) and j:
            q = j[0]
            out = {"text": (q.get("q") or "").strip(), "author": (q.get("a") or "Unknown").strip()}
            if out["text"]:
                _cache_set("zenquotes:today", out)
                return out
    except Exception as e:
        log.warning("ZenQuotes failed: %s", e)
    return {
        "text": "The best way to predict the future is to invent it.",
        "author": "Alan Kay",
    }


# ── Stock portfolio helpers ──────────────────────────────────────────────────

def _compute_portfolio():
    """Aggregate stock_transactions into current holdings.
    Returns {symbol: {qty, avg_cost, total_cost}}."""
    conn = get_db()
    cur = conn.cursor()
    holdings = {}
    try:
        cur.execute(
            "SELECT symbol, action, quantity, price FROM stock_transactions "
            "ORDER BY transaction_date ASC, id ASC"
        )
        for r in cur.fetchall():
            sym = (r["symbol"] or "").upper()
            qty = float(r["quantity"])
            price = float(r["price"])
            h = holdings.setdefault(sym, {"qty": 0.0, "total_cost": 0.0})
            if r["action"] == "buy":
                h["qty"] += qty
                h["total_cost"] += qty * price
            else:  # sell — reduce qty and cost-basis proportionally
                if h["qty"] > 0:
                    cost_per_share = h["total_cost"] / h["qty"]
                    removed = min(qty, h["qty"])
                    h["qty"] -= removed
                    h["total_cost"] -= removed * cost_per_share
        out = {}
        for sym, h in holdings.items():
            if h["qty"] <= 1e-9:
                continue
            avg = h["total_cost"] / h["qty"] if h["qty"] > 0 else 0.0
            out[sym] = {
                "qty": round(h["qty"], 6),
                "avg_cost": round(avg, 4),
                "total_cost": round(h["total_cost"], 2),
            }
        return out
    finally:
        cur.close()
        conn.close()


def build_portfolio_snapshot():
    """Enrich holdings with live prices. Returns {holdings, total_value, total_day_change, total_day_change_pct}."""
    holdings = _compute_portfolio()
    rows = []
    total_value = 0.0
    total_prev = 0.0
    for sym, h in holdings.items():
        quote = fetch_finnhub_quote(sym) or {}
        price = quote.get("price") or h["avg_cost"]
        prev = quote.get("prev_close") or price
        value = h["qty"] * price
        prev_value = h["qty"] * prev
        total_value += value
        total_prev += prev_value
        unrealized = value - h["total_cost"]
        unrealized_pct = (unrealized / h["total_cost"] * 100.0) if h["total_cost"] > 0 else 0.0
        rows.append({
            "symbol": sym,
            "qty": h["qty"],
            "avg_cost": h["avg_cost"],
            "current_price": round(float(price), 4),
            "day_change_pct": quote.get("day_change_pct"),
            "value": round(value, 2),
            "unrealized_pl": round(unrealized, 2),
            "unrealized_pct": round(unrealized_pct, 2),
        })
    rows.sort(key=lambda r: r["value"], reverse=True)
    total_day_change = total_value - total_prev
    total_day_change_pct = (total_day_change / total_prev * 100.0) if total_prev > 0 else 0.0
    return {
        "holdings": rows,
        "total_value": round(total_value, 2),
        "total_day_change": round(total_day_change, 2),
        "total_day_change_pct": round(total_day_change_pct, 2),
    }


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
            "due_display": due_val.astimezone(TZ).strftime("%A, %-m/%-d/%Y, at %-I:%M %p (%Z)"),
            "urgency": urgency
        })
    assignments.sort(key=lambda x: x["due_iso"])
    return assignments


def _cache_canvas_assignments(assignments):
    """Persist seen Canvas assignments so overdue ones survive Canvas iCal pruning."""
    if not assignments:
        return
    try:
        conn = get_db()
        cur = conn.cursor()
        for a in assignments:
            uid = (a.get("uid") or "").strip() or a["title"]
            cur.execute("""
INSERT INTO canvas_assignments_cache
    (uid, title, class_name, due_iso, due_display, description, urgency, last_seen_at)
VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
ON CONFLICT (uid) DO UPDATE SET
    title       = EXCLUDED.title,
    class_name  = EXCLUDED.class_name,
    due_iso     = EXCLUDED.due_iso,
    due_display = EXCLUDED.due_display,
    description = EXCLUDED.description,
    urgency     = EXCLUDED.urgency,
    last_seen_at = NOW()""",
                (uid, a["title"], a.get("class_name", ""),
                 a.get("due_iso", ""), a.get("due_display", ""),
                 a.get("description", "")[:1000], a.get("urgency", "low")))
        conn.commit()
        cur.close(); conn.close()
    except Exception as e:
        log.warning("_cache_canvas_assignments failed: %s", e)


def _promote_overdue_to_tasks():
    """Auto-create a task for each overdue Canvas assignment not yet in the task list.

    Runs inside get_canvas_assignments_with_overdue() every time assignments are
    fetched. Uses an atomic UPDATE...RETURNING claim on the cache's promoted_to_task
    flag so concurrent calls (briefing job, notification job, chat tabs) cannot
    double-insert tasks. Already-completed assignments are claimed but no task is created.
    """
    try:
        now_iso = datetime.now(TZ).isoformat()
        lookback_iso = (datetime.now(TZ) - timedelta(days=90)).isoformat()
        conn = get_db()
        cur = conn.cursor()

        cur.execute("SELECT DISTINCT assignment_title FROM completions")
        done_titles = set(r["assignment_title"] for r in cur.fetchall())

        cur.execute("""
            SELECT uid, title, class_name, due_iso, due_display
            FROM canvas_assignments_cache
            WHERE due_iso < %s
              AND due_iso > %s
              AND promoted_to_task = FALSE
            ORDER BY due_iso ASC""",
            (now_iso, lookback_iso))
        candidates = cur.fetchall()

        promoted = 0
        for row in candidates:
            # Atomic claim: only one process can flip promoted_to_task FALSE -> TRUE.
            # If we don't get a row back, another process already claimed it.
            cur.execute(
                "UPDATE canvas_assignments_cache SET promoted_to_task = TRUE "
                "WHERE uid = %s AND promoted_to_task = FALSE RETURNING uid",
                (row["uid"],))
            if not cur.fetchone():
                continue

            if row["title"] in done_titles:
                # Already completed — claim is enough, don't create a task
                continue

            # Skip if a matching incomplete task already exists (manual or prior promotion)
            cur.execute(
                "SELECT id FROM tasks WHERE title = %s AND completed = FALSE LIMIT 1",
                (row["title"],))
            if cur.fetchone():
                continue

            try:
                due_date = datetime.fromisoformat(row["due_iso"]).date()
            except Exception:
                due_date = None
            notes = f"Overdue Canvas assignment — {row['class_name']}" if row["class_name"] else "Overdue Canvas assignment"
            cur.execute(
                "INSERT INTO tasks (title, urgency, due_date, notes) VALUES (%s, 'high', %s, %s)",
                (row["title"], due_date, notes))
            promoted += 1
            log.info("Promoted overdue Canvas assignment to task: %r", row["title"])

        if candidates:
            conn.commit()
        cur.close(); conn.close()
    except Exception as e:
        log.warning("_promote_overdue_to_tasks failed: %s", e)


def get_canvas_assignments_with_overdue(cal):
    """Return upcoming Canvas assignments PLUS any overdue ones not yet completed.

    Canvas drops past-due events from its iCal feed; this function caches every
    assignment seen from the live feed and re-surfaces overdue ones until the
    student explicitly marks them done via complete_assignment.
    """
    # 1. Live upcoming assignments from Canvas iCal
    live = parse_canvas_assignments(cal)

    # 2. Persist them so we don't lose them after Canvas prunes the feed
    _cache_canvas_assignments(live)

    # 3. Promote any newly overdue assignments to the task list
    _promote_overdue_to_tasks()

    # 4. Merge in overdue assignments from cache that are not yet completed
    try:
        now_iso = datetime.now(TZ).isoformat()
        lookback_iso = (datetime.now(TZ) - timedelta(days=90)).isoformat()
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT DISTINCT assignment_title FROM completions")
        done_titles = set(r["assignment_title"] for r in cur.fetchall())
        cur.execute("""
            SELECT uid, title, class_name, due_iso, due_display, description
            FROM canvas_assignments_cache
            WHERE due_iso < %s AND due_iso > %s
            ORDER BY due_iso DESC""",
            (now_iso, lookback_iso))
        cached_overdue = cur.fetchall()
        cur.close(); conn.close()

        live_titles = {a["title"] for a in live}
        for row in cached_overdue:
            if row["title"] in done_titles:
                continue
            if row["title"] in live_titles:
                continue  # already in upcoming feed
            live.append({
                "uid": row["uid"],
                "title": row["title"],
                "class_name": row["class_name"],
                "due_iso": row["due_iso"],
                "due_display": f"OVERDUE — was due {row['due_display']}",
                "description": row["description"],
                "urgency": "high",
                "overdue": True,
            })
    except Exception as e:
        log.warning("get_canvas_assignments_with_overdue cache lookup failed: %s", e)

    live.sort(key=lambda x: x.get("due_iso", ""))
    return live


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


# ── ntfy push notification helpers ────────────────────────────────────────────

_NTFY_PRIORITY_MAP = {"min": 1, "low": 2, "default": 3, "high": 4, "urgent": 5}


def send_ntfy_notification(title, message, priority="default", tags=None):
    """Send a push notification via ntfy. Returns True on success.

    Uses ntfy's JSON publish endpoint so UTF-8 titles/messages (em-dashes, emoji,
    accented characters) work without RFC-2047 encoding tricks.
    """
    if not NTFY_TOPIC:
        return False
    payload = {
        "topic": NTFY_TOPIC,
        "title": (title or "")[:255],
        "message": message or "",
        "priority": _NTFY_PRIORITY_MAP.get(priority, 3),
        "tags": [str(t) for t in (tags or []) if t],
    }
    headers = {"Content-Type": "application/json"}
    if NTFY_TOKEN:
        headers["Authorization"] = f"Bearer {NTFY_TOKEN}"
    try:
        resp = requests.post(
            NTFY_SERVER.rstrip("/"),
            json=payload,
            headers=headers,
            timeout=10,
        )
        if not resp.ok:
            log.warning("ntfy returned %s: %s", resp.status_code, resp.text[:200])
        return resp.ok
    except Exception as e:
        log.error("ntfy notification failed: %s", e)
        return False


def send_email(to_addr, subject, body_html):
    """Send an email via SMTP. Returns True on success, False on failure/not configured."""
    mail_server = os.environ.get("MAIL_SERVER", "").strip()
    mail_user = os.environ.get("MAIL_USERNAME", "").strip()
    mail_pass = os.environ.get("MAIL_PASSWORD", "").strip()
    if not all([mail_server, mail_user, mail_pass]):
        log.warning("SMTP not configured; skipping email to %s", to_addr)
        return False
    mail_port = int(os.environ.get("MAIL_PORT", "587"))
    mail_from = os.environ.get("MAIL_FROM", mail_user)
    use_tls = os.environ.get("MAIL_USE_TLS", "true").lower() != "false"
    import smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = mail_from
    msg["To"] = to_addr
    msg.attach(MIMEText(body_html, "html"))
    try:
        if use_tls:
            with smtplib.SMTP(mail_server, mail_port, timeout=10) as s:
                s.ehlo(); s.starttls(); s.login(mail_user, mail_pass); s.send_message(msg)
        else:
            with smtplib.SMTP_SSL(mail_server, mail_port, timeout=10) as s:
                s.login(mail_user, mail_pass); s.send_message(msg)
        log.info("Email sent to %s: %s", to_addr, subject)
        return True
    except Exception as e:
        log.error("Email send failed to %s: %s", to_addr, e)
        return False


def _ntfy_dedup(key, title="", max_age_hours=20):
    """Return True if this notification has NOT been sent within max_age_hours (and record it).
    Returns False if a duplicate was found (skip sending)."""
    try:
        conn = get_db()
        cur = conn.cursor()
        cutoff = datetime.now(TZ) - timedelta(hours=max_age_hours)
        cur.execute(
            "SELECT sent_at FROM notification_log WHERE notification_key = %s AND sent_at > %s",
            (key, cutoff),
        )
        if cur.fetchone():
            cur.close(); conn.close()
            return False
        cur.execute(
            "INSERT INTO notification_log (notification_key, title, sent_at) VALUES (%s, %s, NOW()) "
            "ON CONFLICT (notification_key) DO UPDATE SET sent_at = NOW(), title = EXCLUDED.title",
            (key[:500], (title or key)[:255]),
        )
        conn.commit()
        cur.close(); conn.close()
        return True
    except Exception as e:
        log.warning("_ntfy_dedup failed: %s", e)
        return True  # default to allowing send


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
        cal = fetch_ical(u_canvas_ical())
        if cal:
            assignments = get_canvas_assignments_with_overdue(cal)

        events = []
        cal2 = fetch_ical(u_personal_ical())
        if cal2:
            events = list(parse_calendar_events(cal2, days_ahead=1))
        if u_job_schedule_ical():
            try:
                cal_job = fetch_ical(u_job_schedule_ical())
                if cal_job:
                    events.extend(parse_calendar_events(cal_job, days_ahead=1))
            except Exception as _e:
                log.warning("briefing: job calendar fetch failed: %s", _e)
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

        cur.close()
        conn.close()

        now_local = datetime.now(TZ)
        now_str = now_local.strftime("%A, %-m/%-d/%Y at %-I:%M %p")
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
            jarvis_persona("%s", "serving") + "\n\n"
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
            "• Bullets from REFERENCE Larger/longer; major assignments and significant work not already covered above.\n"
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
            events_text, tasks_text,
        )

        if MEM0_API_KEY:
            try:
                _m0_hits = _get_mem0_client().search("student goals study habits priorities schedule energy", user_id="student", limit=5)
                if _m0_hits:
                    _mem_lines = "\n".join(f"- {h['memory']}" for h in _m0_hits if h.get("memory"))
                    if _mem_lines:
                        prompt += "\n\nSTUDENT LONG-TERM CONTEXT (from memory — factor in naturally):\n" + _mem_lines
            except Exception as _e:
                log.debug("Mem0 briefing search error: %s", _e)

        try:
            client = anthropic.Anthropic(api_key=api_key, max_retries=3, timeout=60.0)
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

        # Push a condensed briefing summary via ntfy
        if NTFY_TOPIC:
            try:
                today_key = f"morning_briefing_{datetime.now(TZ).strftime('%Y-%m-%d')}"
                if _ntfy_dedup(today_key, title="Morning Briefing", max_age_hours=20):
                    # Pull first 300 chars as a teaser
                    preview = content[:300].strip()
                    if len(content) > 300:
                        preview += "…"
                    send_ntfy_notification(
                        title="Good morning — your briefing is ready",
                        message=preview,
                        priority="default",
                        tags=["sun_with_face", "memo"],
                    )
            except Exception as _e:
                log.warning("briefing ntfy push failed: %s", _e)


scheduler = BackgroundScheduler(timezone=TZ)


def _on_scheduler_job_error(event):
    msg = str(event.exception) if event.exception else "unknown"
    log.error("APScheduler job %s failed: %s", event.job_id, msg, exc_info=event.exception)
    _scheduler_last_error_set(event.job_id, msg)


scheduler.add_listener(_on_scheduler_job_error, EVENT_JOB_ERROR)


def generate_evening_debrief():
    """Generate a 7 PM evening debrief summarizing the day."""
    with _debrief_lock:
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

        cal = fetch_ical(u_canvas_ical())
        remaining_asgn = []
        if cal:
            all_asgn = get_canvas_assignments_with_overdue(cal)
            done_titles = {d["assignment_title"] for d in done_today}
            remaining_asgn = [a for a in all_asgn if a["title"] not in done_titles]

        remaining_text = "\n".join(["- %s (%s, due %s)" % (a["title"], a["class_name"], a["due_display"]) for a in remaining_asgn[:6]]) or "None."
        tasks_text = "\n".join(["- [%s] %s" % (t["urgency"], t["title"]) for t in pending_tasks]) or "None."
        now_str = datetime.now(TZ).strftime("%A, %-m/%-d at %-I:%M %p")

        prompt = (
            jarvis_persona("%s", "delivering the evening debrief for") + "\n\n"
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

        if MEM0_API_KEY:
            try:
                _m0d_hits = _get_mem0_client().search("productivity accomplishments energy habits goals", user_id="student", limit=5)
                if _m0d_hits:
                    _mem_lines_d = "\n".join(f"- {h['memory']}" for h in _m0d_hits if h.get("memory"))
                    if _mem_lines_d:
                        prompt += "\n\nSTUDENT LONG-TERM CONTEXT (from memory — factor in naturally):\n" + _mem_lines_d
            except Exception as _e:
                log.debug("Mem0 debrief search error: %s", _e)

        try:
            client = anthropic.Anthropic(api_key=api_key, max_retries=3, timeout=60.0)
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


def generate_weekly_insight(force=False):
    """Generate the Sunday-morning weekly insight: review of last 7 days and look ahead."""
    with _weekly_insight_lock:
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            log.warning("Weekly insight: ANTHROPIC_API_KEY not set")
            return
        cfg = get_config()
        name = cfg.get("name", "Jarvis")

        now_local = datetime.now(TZ)
        if not force:
            conn = get_db()
            cur = conn.cursor()
            cur.execute("SELECT generated_at FROM insight_cache WHERE id = 1")
            row = cur.fetchone()
            cur.close()
            conn.close()
            if row and row["generated_at"]:
                # Skip if already generated since the most recent Sunday 00:00 local
                days_since_sun = (now_local.weekday() + 1) % 7  # Mon=0..Sun=6 -> days since Sunday
                last_sunday = (now_local - timedelta(days=days_since_sun)).replace(hour=0, minute=0, second=0, microsecond=0)
                if row["generated_at"].astimezone(TZ) >= last_sunday:
                    return

        today = now_local.date()
        week_ago = today - timedelta(days=7)
        week_ahead = today + timedelta(days=7)

        conn = get_db()
        cur = conn.cursor()

        # Last 7 days: completions
        cur.execute("""
SELECT assignment_title, class_name, duration_minutes, completed_at
FROM completions
WHERE completed_at >= %s
ORDER BY completed_at ASC""", (now_local - timedelta(days=7),))
        completions_week = [dict(r) for r in cur.fetchall()]

        # Last 7 days: tasks completed
        cur.execute("""
SELECT title, urgency, completed_at
FROM tasks
WHERE completed = TRUE AND completed_at >= %s
ORDER BY completed_at ASC""", (now_local - timedelta(days=7),))
        tasks_done_week = [dict(r) for r in cur.fetchall()]

        # Currently pending tasks
        cur.execute("""
SELECT title, urgency, due_date
FROM tasks
WHERE completed = FALSE
ORDER BY urgency DESC, created_at ASC LIMIT 10""")
        tasks_open = [dict(r) for r in cur.fetchall()]

        # Projects needing check-in
        cur.execute("""
SELECT id, title, status, last_checkin, checkin_interval_days
FROM projects
WHERE status = 'active'""")
        projects = [dict(r) for r in cur.fetchall()]
        cur.close()
        conn.close()

        projects_overdue_checkin = []
        for p in projects:
            last = p.get("last_checkin")
            interval = p.get("checkin_interval_days") or 7
            if last is None:
                projects_overdue_checkin.append(p)
            else:
                age_days = (now_local - last.astimezone(TZ)).days
                if age_days > interval:
                    projects_overdue_checkin.append(p)

        # Upcoming 7 days: assignments + events
        upcoming_assignments = []
        cal = fetch_ical(u_canvas_ical())
        if cal:
            all_asgn = get_canvas_assignments_with_overdue(cal)
            for a in all_asgn:
                d = _assignment_due_date_local(a)
                if today <= d <= week_ahead:
                    upcoming_assignments.append(a)
            upcoming_assignments.sort(key=lambda a: a.get("due_iso", ""))

        upcoming_events = []
        for offset in range(0, 8):
            day = today + timedelta(days=offset)
            upcoming_events.extend(fetch_day_calendar_events(day, days_ahead=1))

        # Format payloads compactly for the prompt
        total_hours = sum(c["duration_minutes"] for c in completions_week) / 60.0
        completions_text = "\n".join(
            "- %s (%s) — %.0f min" % (c["assignment_title"], c["class_name"], c["duration_minutes"])
            for c in completions_week[:15]
        ) or "- None."
        tasks_done_text = "\n".join(
            "- [%s] %s" % (t["urgency"], t["title"]) for t in tasks_done_week[:15]
        ) or "- None."
        tasks_open_text = "\n".join(
            "- [%s] %s%s" % (t["urgency"], t["title"], (" (due %s)" % t["due_date"]) if t.get("due_date") else "")
            for t in tasks_open
        ) or "- None."
        projects_text = "\n".join(
            "- %s (status %s)" % (p["title"], p["status"])
            for p in projects
        ) or "- No active projects."
        projects_overdue_text = "\n".join(
            "- %s (last check-in overdue)" % p["title"] for p in projects_overdue_checkin
        ) or "- None."
        upcoming_assign_text = "\n".join(
            "- %s (%s) due %s" % (a["title"], a.get("class_name", ""), a.get("due_display", ""))
            for a in upcoming_assignments[:10]
        ) or "- None."
        upcoming_events_text = "\n".join(
            "- %s%s on %s" % (e["title"], " [SPORTS]" if e.get("source") == "sports" else "", e.get("start_display", ""))
            for e in upcoming_events[:10]
        ) or "- None."

        now_str = now_local.strftime("%A, %B %-d, %Y at %-I:%M %p")
        week_label = "%s – %s" % (week_ago.strftime("%b %-d"), today.strftime("%b %-d"))

        prompt = (
            jarvis_persona(name, "delivering the weekly insight for") + "\n\n"
            f"Current time: {now_str}\n"
            f"Reviewing the week of {week_label}.\n\n"
            f"WEEK COMPLETIONS ({total_hours:.1f} hours of focused work, {len(completions_week)} items):\n{completions_text}\n\n"
            f"TASKS COMPLETED THIS WEEK:\n{tasks_done_text}\n\n"
            f"ACTIVE PROJECTS (current state):\n{projects_text}\n\n"
            f"PROJECTS WITH OVERDUE CHECK-IN:\n{projects_overdue_text}\n\n"
            f"STILL OPEN (carrying into the new week):\n{tasks_open_text}\n\n"
            f"UPCOMING ASSIGNMENTS (next 7 days):\n{upcoming_assign_text}\n\n"
            f"UPCOMING CALENDAR (next 7 days):\n{upcoming_events_text}\n\n"
            "Compose a sophisticated weekly insight using EXACTLY these four markdown sections with ## headings (spell each heading exactly):\n\n"
            "## Week in Review\n"
            "• Three to five bullets distilling the week's pattern: what was accomplished, time invested, where momentum was strongest. Reference specific items and the metrics where pertinent.\n\n"
            "## Productivity Assessment\n"
            "• A measured, candid appraisal in two to three sentences (or bullets). Was the workload distributed sensibly? Were any classes neglected? Keep it analytical, not preachy.\n\n"
            "## Project Status\n"
            "• One bullet per active project summarising progress; flag any with overdue check-ins explicitly. If none, one bullet: All projects are presently in good order.\n\n"
            "## Recommendation for the Coming Week\n"
            "• Exactly one specific, actionable recommendation grounded in what the data shows — for instance, a particular class to prioritise, a project to push, or a habit to adjust. Keep it to one short paragraph or two bullets.\n\n"
            "Maintain a refined, insightful tone. Use **bold** for assignment, project, or class names where helpful. No introductory paragraph. Deliver the four sections only."
        )

        try:
            client = anthropic.Anthropic(api_key=api_key, max_retries=3, timeout=60.0)
            message = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=1400,
                messages=[{"role": "user", "content": prompt}]
            )
            track_api_usage(message)
            content = message.content[0].text if message.content else "Good morning, sir."
        except Exception as e:
            log.error("Weekly insight API error: %s", e)
            return

        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
INSERT INTO insight_cache (id, generated_at, content) VALUES (1, NOW(), %s)
ON CONFLICT (id) DO UPDATE SET generated_at = NOW(), content = EXCLUDED.content""", (content,))
        conn.commit()
        cur.close()
        conn.close()
        log.info("Weekly insight generated.")


def cleanup_old_data():
    """Prune old data: daily_plans older than 60 days; done projects older than 7 days."""
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("DELETE FROM daily_plans WHERE plan_date < CURRENT_DATE - INTERVAL '60 days'")
        deleted_plans = cur.rowcount
        cur.execute("DELETE FROM projects WHERE status = 'done' AND done_at < NOW() - INTERVAL '7 days'")
        deleted_projects = cur.rowcount
        conn.commit()
        cur.close()
        conn.close()
        if deleted_plans:
            log.info("cleanup_old_data: deleted %d daily_plans rows older than 60 days", deleted_plans)
        if deleted_projects:
            log.info("cleanup_old_data: deleted %d done projects older than 7 days", deleted_projects)
    except Exception as e:
        log.error("cleanup_old_data error: %s", e)


def _notif_canvas_assignments():
    """Return upcoming Canvas assignments not yet completed, or []."""
    try:
        if not u_canvas_ical():
            return []
        cal = fetch_ical(u_canvas_ical())
        if not cal:
            return []
        assignments = get_canvas_assignments_with_overdue(cal)
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT DISTINCT assignment_title FROM completions")
        done = set(r["assignment_title"] for r in cur.fetchall())
        cur.close(); conn.close()
        return [a for a in assignments if a["title"] not in done]
    except Exception as e:
        log.warning("_notif_canvas_assignments error: %s", e)
        return []


def check_assignment_due_notifications():
    """Tier 1 — send ntfy for assignments due in ~24 h and ~2 h."""
    if not NTFY_TOPIC:
        return
    try:
        assignments = _notif_canvas_assignments()
        now = datetime.now(TZ)
        for a in assignments:
            due_iso = a.get("due_iso", "")
            if not due_iso:
                continue
            try:
                due_dt = datetime.fromisoformat(due_iso)
                due_dt = due_dt.astimezone(TZ) if due_dt.tzinfo else due_dt.replace(tzinfo=TZ)
            except Exception:
                continue
            hours_until = (due_dt - now).total_seconds() / 3600
            course = a.get("class_name", "")
            due_disp = a.get("due_display", due_dt.strftime("%-I:%M %p"))

            if 22.5 <= hours_until <= 25.5:
                key = f"asgn_24h_{a['title']}_{due_dt.strftime('%Y-%m-%d')}"
                if _ntfy_dedup(key, title=a["title"], max_age_hours=20):
                    send_ntfy_notification(
                        title="Assignment Due Tomorrow",
                        message=f"{a['title']} ({course}) — due {due_disp}",
                        priority="high",
                        tags=["books", "warning"],
                    )
            elif 1.5 <= hours_until <= 2.5:
                key = f"asgn_2h_{a['title']}_{due_dt.strftime('%Y-%m-%d-%H')}"
                if _ntfy_dedup(key, title=a["title"], max_age_hours=4):
                    send_ntfy_notification(
                        title="Assignment Due in ~2 Hours",
                        message=f"{a['title']} ({course}) — due {due_disp}",
                        priority="urgent",
                        tags=["rotating_light", "books"],
                    )
    except Exception as e:
        log.error("check_assignment_due_notifications error: %s", e)


def check_overdue_tasks():
    """Tier 1 — send ntfy for tasks past their due date that haven't been completed."""
    if not NTFY_TOPIC:
        return
    try:
        today = datetime.now(TZ).date()
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            "SELECT id, title, due_date FROM tasks "
            "WHERE completed = FALSE AND due_date < %s ORDER BY due_date ASC LIMIT 10",
            (today,),
        )
        overdue = cur.fetchall()
        cur.close(); conn.close()
        for task in overdue:
            key = f"task_overdue_{task['id']}_{today}"
            if _ntfy_dedup(key, title=task["title"], max_age_hours=20):
                send_ntfy_notification(
                    title="Overdue Task",
                    message=f"{task['title']} — was due {task['due_date']}, still pending",
                    priority="high",
                    tags=["alarm_clock", "warning"],
                )
    except Exception as e:
        log.error("check_overdue_tasks error: %s", e)


def check_ap_test_countdown():
    """Tier 2 — warn 2 days before any AP exam found in Canvas."""
    if not NTFY_TOPIC:
        return
    try:
        assignments = _notif_canvas_assignments()
        now = datetime.now(TZ)
        for a in assignments:
            title_lc = a.get("title", "").lower()
            if not any(kw in title_lc for kw in ("ap exam", "ap test", " ap ", "ap calc", "ap spanish", "ap bio", "ap chem", "ap history", "ap lang", "ap lit", "ap physics", "ap stats", "ap gov")):
                continue
            due_iso = a.get("due_iso", "")
            if not due_iso:
                continue
            try:
                due_dt = datetime.fromisoformat(due_iso)
                due_dt = due_dt.astimezone(TZ) if due_dt.tzinfo else due_dt.replace(tzinfo=TZ)
                days_until = (due_dt.date() - now.date()).days
            except Exception:
                continue
            if days_until in (1, 2, 7):
                key = f"ap_countdown_{a['title']}_{due_dt.strftime('%Y-%m-%d')}_{days_until}d"
                if _ntfy_dedup(key, title=a["title"], max_age_hours=20):
                    send_ntfy_notification(
                        title=f"AP Exam in {days_until} Day{'s' if days_until != 1 else ''}",
                        message=f"{a['title']} is in {days_until} day{'s' if days_until != 1 else ''}, sir. You have been warned.",
                        priority="high",
                        tags=["mortar_board", "warning"],
                    )
    except Exception as e:
        log.error("check_ap_test_countdown error: %s", e)


def check_meeting_reminders():
    """Tier 2 — send a 30-minute heads-up for upcoming calendar events."""
    if not NTFY_TOPIC:
        return
    try:
        now = datetime.now(TZ)
        window_start = now + timedelta(minutes=25)
        window_end = now + timedelta(minutes=40)
        for url in filter(None, [u_personal_ical(), u_sports_ical(), u_job_schedule_ical()]):
            try:
                cal = fetch_ical(url)
                if not cal:
                    continue
                events = parse_calendar_events(cal, days_ahead=1)
                for ev in events:
                    if ev.get("all_day"):
                        continue
                    try:
                        start_dt = datetime.fromisoformat(ev["start_iso"])
                        start_dt = start_dt.astimezone(TZ) if start_dt.tzinfo else start_dt.replace(tzinfo=TZ)
                    except Exception:
                        continue
                    if window_start <= start_dt <= window_end:
                        key = f"meeting_{ev['title']}_{start_dt.strftime('%Y-%m-%d-%H-%M')}"
                        if _ntfy_dedup(key, title=ev["title"], max_age_hours=20):
                            send_ntfy_notification(
                                title="Event Starting Soon",
                                message=f"{ev['title']} at {ev['start_display']}",
                                priority="high",
                                tags=["calendar", "alarm_clock"],
                            )
            except Exception as _e:
                log.warning("check_meeting_reminders url=%s: %s", url[:40], _e)
    except Exception as e:
        log.error("check_meeting_reminders error: %s", e)


def check_idle_detection():
    """Tier 3 — nudge if no task/assignment logged in 3+ hours on a school night."""
    if not NTFY_TOPIC:
        return
    try:
        now = datetime.now(TZ)
        # Only Sun–Thu, 7 PM–11 PM
        if now.weekday() >= 4 and now.weekday() != 6:
            return
        if not (19 <= now.hour < 23):
            return
        conn = get_db()
        cur = conn.cursor()
        cutoff = now - timedelta(hours=3)
        cur.execute(
            "SELECT MAX(completed_at) AS last FROM completions WHERE completed_at > %s",
            (cutoff,),
        )
        row = cur.fetchone()
        cur.close(); conn.close()
        if row and row["last"]:
            return
        key = f"idle_{now.strftime('%Y-%m-%d-%H')}"
        if _ntfy_dedup(key, title="Idle check", max_age_hours=1):
            send_ntfy_notification(
                title="Still with me, sir?",
                message="No tasks logged in over 3 hours. Might be worth making a dent in that list.",
                priority="default",
                tags=["sleeping"],
            )
    except Exception as e:
        log.error("check_idle_detection error: %s", e)


def check_trash_recycling_reminder():
    """Tier 3 — remind about trash/recycling events from personal calendar at 7 PM."""
    if not NTFY_TOPIC or not u_personal_ical():
        return
    try:
        now = datetime.now(TZ)
        if not (19 <= now.hour < 20):
            return
        cal = fetch_ical(u_personal_ical())
        if not cal:
            return
        events = parse_calendar_events(cal, days_ahead=1)
        today_str = now.strftime("%Y-%m-%d")
        for ev in events:
            title_lc = ev.get("title", "").lower()
            if ev.get("date") == today_str and any(kw in title_lc for kw in ("trash", "recycling", "recycle", "garbage", "bin")):
                key = f"trash_{ev['title']}_{today_str}"
                if _ntfy_dedup(key, title=ev["title"], max_age_hours=20):
                    send_ntfy_notification(
                        title="Trash Reminder",
                        message=f"{ev['title']} tonight — don't forget.",
                        priority="default",
                        tags=["wastebasket"],
                    )
    except Exception as e:
        log.error("check_trash_recycling_reminder error: %s", e)


def check_weather_warning():
    """Tier 3 — check NWS alerts for Park City and push severe/extreme warnings."""
    if not NTFY_TOPIC:
        return
    try:
        resp = requests.get(
            "https://api.weather.gov/alerts/active?point=40.6461,-111.4980",
            headers={"User-Agent": "students-assistant/1.0 (contact: admin@localhost)"},
            timeout=12,
        )
        if not resp.ok:
            return
        features = resp.json().get("features", [])
        for f in features[:5]:
            props = f.get("properties", {})
            severity = props.get("severity", "")
            if severity not in ("Extreme", "Severe", "Moderate"):
                continue
            event = props.get("event", "Weather Alert")
            headline = (props.get("headline") or event)[:250]
            alert_id = props.get("id", event)
            key = f"weather_{alert_id}"
            if _ntfy_dedup(key, title=event, max_age_hours=6):
                send_ntfy_notification(
                    title=f"Weather Alert: {event}",
                    message=headline,
                    priority="high" if severity in ("Extreme", "Severe") else "default",
                    tags=["cloud_lightning", "warning"],
                )
    except Exception as e:
        log.error("check_weather_warning error: %s", e)


def check_stock_alerts():
    """Tier 2 — alert on ±5% daily moves, target price hits, or stop-loss triggers."""
    if not NTFY_TOPIC or not FINNHUB_API_KEY:
        return
    try:
        now = datetime.now(TZ)
        # Only during extended market hours Mon–Fri 8 AM–5 PM MT
        if now.weekday() >= 5 or not (8 <= now.hour < 17):
            return
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            SELECT
                st.symbol,
                SUM(CASE WHEN st.action='buy' THEN st.quantity ELSE -st.quantity END) AS net_shares,
                sn.target_price,
                sn.stop_loss
            FROM stock_transactions st
            LEFT JOIN stock_notes sn ON sn.symbol = st.symbol
            GROUP BY st.symbol, sn.target_price, sn.stop_loss
            HAVING SUM(CASE WHEN st.action='buy' THEN st.quantity ELSE -st.quantity END) > 0
        """)
        holdings = cur.fetchall()
        cur.close(); conn.close()
        if not holdings:
            return
        for h in holdings:
            symbol = h["symbol"]
            try:
                resp = requests.get(
                    "https://finnhub.io/api/v1/quote",
                    params={"symbol": symbol, "token": FINNHUB_API_KEY},
                    timeout=6,
                )
                if not resp.ok:
                    continue
                q = resp.json()
                current = float(q.get("c", 0) or 0)
                prev_close = float(q.get("pc", 0) or 0)
                if not current or not prev_close:
                    continue
                pct_change = (current - prev_close) / prev_close * 100

                alert_reason = None
                priority = "default"
                tags_up = ["chart_increasing"]
                tags_dn = ["chart_decreasing", "warning"]

                if abs(pct_change) >= 5:
                    direction = "up" if pct_change > 0 else "down"
                    alert_reason = f"{direction} {abs(pct_change):.1f}% today (${current:.2f})"
                    priority = "high"
                    alert_tags = tags_up if pct_change > 0 else tags_dn
                elif h["target_price"] and current >= float(h["target_price"]):
                    alert_reason = f"hit target ${float(h['target_price']):.2f} — now ${current:.2f}"
                    priority = "high"
                    alert_tags = tags_up
                elif h["stop_loss"] and current <= float(h["stop_loss"]):
                    alert_reason = f"hit stop-loss ${float(h['stop_loss']):.2f} — now ${current:.2f}"
                    priority = "urgent"
                    alert_tags = tags_dn

                if alert_reason:
                    key = f"stock_{symbol}_{now.strftime('%Y-%m-%d-%H')}"
                    if _ntfy_dedup(key, title=symbol, max_age_hours=2):
                        send_ntfy_notification(
                            title=f"Stock Alert: {symbol}",
                            message=f"{symbol} {alert_reason}",
                            priority=priority,
                            tags=alert_tags,
                        )
            except Exception as ex:
                log.warning("check_stock_alerts: %s error: %s", symbol, ex)
    except Exception as e:
        log.error("check_stock_alerts error: %s", e)


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
    # Weekly insight every Sunday at 8 AM
    scheduler.add_job(generate_weekly_insight, "cron", day_of_week="sun", hour=8, minute=0,
                      id="weekly_insight", replace_existing=True)
    # Nightly cleanup of stale daily_plans rows at 02:30 (quietest hour)
    scheduler.add_job(cleanup_old_data, "cron", hour=2, minute=30,
                      id="cleanup_old_data", replace_existing=True)
    # Auto-generate tomorrow's daily schedule at 10 PM
    scheduler.add_job(auto_generate_plan_job, "cron", hour=22, minute=0,
                      id="auto_daily_plan", replace_existing=True)

    # ── ntfy notification jobs ────────────────────────────────────────────────
    # Tier 1: assignment due-soon checks (24 h and 2 h windows) every 20 min
    scheduler.add_job(check_assignment_due_notifications, "interval", minutes=20,
                      id="notif_assignment_due", replace_existing=True)
    # Tier 1: overdue tasks — once per hour
    scheduler.add_job(check_overdue_tasks, "interval", minutes=60,
                      id="notif_overdue_tasks", replace_existing=True)
    # Tier 2: AP test countdown — daily at 7:05 AM
    scheduler.add_job(check_ap_test_countdown, "cron", hour=7, minute=5,
                      id="notif_ap_countdown", replace_existing=True)
    # Tier 2: meeting/event reminders — every 10 min
    scheduler.add_job(check_meeting_reminders, "interval", minutes=10,
                      id="notif_meetings", replace_existing=True)
    # Tier 2: stock movement alerts — every 15 min
    scheduler.add_job(check_stock_alerts, "interval", minutes=15,
                      id="notif_stock_alerts", replace_existing=True)
    # Tier 3: idle detection — every 30 min (self-guards with hour+day check)
    scheduler.add_job(check_idle_detection, "interval", minutes=30,
                      id="notif_idle", replace_existing=True)
    # Tier 3: trash/recycling — every 10 min (self-guards to 7–8 PM window)
    scheduler.add_job(check_trash_recycling_reminder, "interval", minutes=10,
                      id="notif_trash", replace_existing=True)
    # Tier 3: weather warnings — every 2 hours
    scheduler.add_job(check_weather_warning, "interval", minutes=120,
                      id="notif_weather", replace_existing=True)

    log.info("Briefing scheduled for %02d:%02d Mountain", hour, minute)
    log.info("Evening debrief scheduled for 18:30 Mountain")
    log.info("Recurring tasks processor scheduled for 00:00 Mountain")
    log.info("Weekly insight scheduled for Sun 08:00 Mountain")
    log.info("Cleanup job scheduled for 02:30 Mountain")
    log.info("Auto daily plan scheduled for 22:00 Mountain")
    log.info("ntfy notification jobs registered (assignment_due, overdue_tasks, ap_countdown, meetings, stocks, idle, trash, weather)")


# ── Security Functions ──────────────────────────────────────────────────────────

_login_lock = threading.Lock()

def _validate_ip(candidate):
    if not candidate:
        return None
    candidate = candidate.strip()
    if not candidate:
        return None
    try:
        ipaddress.ip_address(candidate)
        return candidate
    except ValueError:
        return None

def get_client_ip():
    """Get a validated, stable client identifier.

    Trust order (most-trusted first):
      1. request.remote_addr — set by ProxyFix from the trusted proxy hop.
         An attacker cannot forge this when ProxyFix is correctly configured.
      2. Provider-set headers (CF-Connecting-IP, True-Client-IP, X-Real-IP)
         — only consulted if remote_addr is missing/invalid. Useful for
         multi-proxy deployments (e.g. Cloudflare in front of the app).
      3. Stable fallback: a hash of the User-Agent. Used only when no real
         IP can be determined. Does NOT include Referer/Origin (changes
         per page navigation) or remote_addr (may be None/invalid), so the
         same client maps to the same key across requests.

    We deliberately do NOT take the leftmost X-Forwarded-For value, because
    that header is client-controllable and would allow IP spoofing.
    """
    ip = _validate_ip(request.remote_addr)
    if ip:
        return ip

    for header in ('CF-Connecting-IP', 'True-Client-IP', 'X-Real-IP'):
        ip = _validate_ip(request.headers.get(header, ''))
        if ip:
            return ip

    if request.remote_addr:
        log.warning(f"Invalid IP format detected from request: {request.remote_addr}")

    user_agent = request.headers.get('User-Agent', '')
    return f"unknown-{hashlib.sha256(user_agent.encode()).hexdigest()[:12]}"

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

def record_login_attempt(ip_addr, success, username=""):
    """Record login attempt and update lockout status.

    Tracks failure_count on every failure. Once it reaches 5 consecutive
    failures, sets locked_until with exponential backoff (15, 30, 60, 120 min...).
    A successful login clears the counter.
    """
    try:
        with _login_lock:
            conn = get_db()
            cur = conn.cursor()

            cur.execute("""
INSERT INTO login_attempts (ip_address, success, username, user_agent)
VALUES (%s, %s, %s, %s)""", (ip_addr, success, username[:50] if username else "", request.headers.get('User-Agent', '')[:500]))

            if success:
                cur.execute("DELETE FROM login_lockouts WHERE ip_address = %s", (ip_addr,))
                conn.commit()
                cur.close()
                conn.close()
                return {"locked": False, "minutes_remaining": 0}

            # Failure: increment counter (UPSERT so the row is created on first failure).
            # locked_until is seeded with NOW() so is_ip_locked() returns False until threshold.
            now = datetime.now(TZ)
            cur.execute("""
INSERT INTO login_lockouts (ip_address, locked_until, failure_count)
VALUES (%s, %s, 1)
ON CONFLICT (ip_address) DO UPDATE
SET failure_count = login_lockouts.failure_count + 1
RETURNING failure_count""", (ip_addr, now))
            new_count = cur.fetchone()["failure_count"]

            if new_count >= 5:
                lockout_duration = timedelta(minutes=15 * (2 ** (new_count - 5)))
                locked_until = now + lockout_duration
                minutes_remaining = max(1, int(lockout_duration.total_seconds() / 60))
                cur.execute(
                    "UPDATE login_lockouts SET locked_until = %s WHERE ip_address = %s",
                    (locked_until, ip_addr))
                conn.commit()
                cur.close()
                conn.close()
                return {"locked": True, "minutes_remaining": minutes_remaining}

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

def prune_login_attempts(retention_days=30):
    """Delete login_attempts rows older than retention_days. Also clears
    expired login_lockouts so old rows don't accumulate."""
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute(
            "DELETE FROM login_attempts WHERE attempted_at < NOW() - %s::interval",
            (f"{retention_days} days",))
        attempts_deleted = cur.rowcount
        cur.execute("DELETE FROM login_lockouts WHERE locked_until < NOW() - INTERVAL '7 days'")
        lockouts_deleted = cur.rowcount
        conn.commit()
        if attempts_deleted or lockouts_deleted:
            log.info(f"Pruned {attempts_deleted} old login_attempts, {lockouts_deleted} stale lockouts")
        return True
    except Exception as e:
        conn.rollback()
        log.warning(f"Error pruning login attempts: {e}")
    finally:
        cur.close()
        conn.close()
    return False

def is_valid_username(username):
    """Check if a username is a recognized/valid system user."""
    if not username:
        return False
    valid_users = [ADMIN_USER, AVERAGE_USER, PARENT_USER, "admin", "user"]
    return username.strip() in valid_users

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
        is_env_student = (username == AVERAGE_USER)

        if is_admin:
            expected_password = ADMIN_PASSWORD
        elif is_parent:
            expected_password = PARENT_PASSWORD
        elif is_env_student:
            expected_password = APP_PASSWORD
        else:
            expected_password = None

        # Try DB-backed student auth for non-admin, non-parent users
        if not is_admin and not is_parent:
            try:
                _conn = get_db()
                _cur = _conn.cursor()
                _cur.execute("SELECT id, password_hash, active, is_comped, display_name FROM users WHERE username = %s OR email = %s", (username, username))
                _db_user = _cur.fetchone()
                _cur.close()
                _conn.close()
            except Exception as _e:
                log.warning("DB user lookup failed: %s", _e)
                _db_user = None

            if _db_user and _db_user["active"] and check_password_hash(_db_user["password_hash"], password.strip()):
                # Check subscription
                _sub_active = _db_user["is_comped"]
                if not _sub_active:
                    try:
                        _sc = get_db(); _scur = _sc.cursor()
                        _scur.execute("SELECT status FROM subscriptions WHERE user_id = %s ORDER BY created_at DESC LIMIT 1", (_db_user["id"],))
                        _sub = _scur.fetchone()
                        _scur.close(); _sc.close()
                        _sub_active = _sub and _sub["status"] in ("active", "past_due")
                    except Exception:
                        _sub_active = False

                if not _sub_active:
                    return jsonify({"error": "Your subscription is inactive. Please manage your billing.", "billing": True}), 403

                if is_locked_down:
                    return jsonify({"is_locked_down": True, "message": "System in lockdown. Please provide security code."}), 202

                record_login_attempt(ip_addr, True, username)
                session.permanent = True
                session["authenticated"] = True
                session["user_id"] = str(_db_user["id"])
                session["username"] = username
                session["display_name"] = _db_user["display_name"] or username
                session["subscription_active"] = bool(_sub_active)
                session["is_admin"] = False
                session.modified = True

                # Update last_login_at
                try:
                    _lc = get_db(); _lcur = _lc.cursor()
                    _lcur.execute("UPDATE users SET last_login_at = NOW() WHERE id = %s", (_db_user["id"],))
                    _lc.commit(); _lcur.close(); _lc.close()
                except Exception:
                    pass

                return jsonify({"status": "ok", "redirect": "/"})

        # Env-var login path (admin, parent, or legacy AVERAGE_USER)
        if expected_password and secrets.compare_digest(password.strip(), expected_password):
            if is_locked_down:
                return jsonify({
                    "is_locked_down": True,
                    "message": "System in lockdown. Please provide security code."
                }), 202

            record_login_attempt(ip_addr, True, username)
            session.permanent = True
            if is_admin:
                session["admin_authenticated"] = True
                session["is_admin"] = True
            elif is_parent:
                session["parent_authenticated"] = True
            else:
                session["authenticated"] = True
                # Try to fetch user_id from DB for the env-var student
                try:
                    _ec = get_db(); _ecur = _ec.cursor()
                    _ecur.execute("SELECT id FROM users WHERE username = %s", (username,))
                    _erow = _ecur.fetchone()
                    _ecur.close(); _ec.close()
                    if _erow:
                        session["user_id"] = str(_erow["id"])
                except Exception:
                    pass
                session["username"] = username
                session["is_admin"] = False
            session.modified = True

            if is_admin:
                redirect_url = "/admin"
            elif is_parent:
                redirect_url = "/parent"
            else:
                redirect_url = "/"
            return jsonify({"status": "ok", "redirect": redirect_url})
        else:
            lockout_info = record_login_attempt(ip_addr, False, username)
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
            log.warning(
                "Login security code attempt: username=%s, is_admin=%s, is_parent=%s, ip_blocked=%s, locked_down=%s",
                username, is_admin, is_parent, ip_is_blocked, is_locked_down,
            )
            if expected_password and secrets.compare_digest(password.strip(), expected_password) and secrets.compare_digest(security_code.strip(), security_code_env):
                record_login_attempt(ip_addr, True, username)
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
                lockout_info = record_login_attempt(ip_addr, False, username)
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
        # Check if admin password
        if secrets.compare_digest(password.strip(), ADMIN_PASSWORD):
            if is_locked_down:
                return jsonify({
                    "is_locked_down": True,
                    "message": "System in lockdown. Please provide security code."
                }), 202

            record_login_attempt(ip_addr, True, "admin")
            session.permanent = True
            session["admin_authenticated"] = True
            session.modified = True
            return jsonify({"status": "ok", "redirect": "/admin"})

        # Check if app password
        if APP_PASSWORD and secrets.compare_digest(password.strip(), APP_PASSWORD):
            if is_locked_down:
                return jsonify({
                    "is_locked_down": True,
                    "message": "System in lockdown. Please provide security code."
                }), 202

            record_login_attempt(ip_addr, True, "user")
            session.permanent = True
            session["authenticated"] = True
            session.modified = True
            return jsonify({"status": "ok", "redirect": "/"})

        # Neither password matched
        lockout_info = record_login_attempt(ip_addr, False, "")
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

            # Check admin password with security code
            if (
                secrets.compare_digest(password.strip(), ADMIN_PASSWORD)
                and secrets.compare_digest(security_code.strip(), security_code_env)
            ):
                record_login_attempt(ip_addr, True, "admin")
                session.permanent = True
                session["admin_authenticated"] = True
                session.modified = True
                return jsonify({"status": "ok", "redirect": "/admin"})

            # Check app password with security code
            if (
                APP_PASSWORD
                and secrets.compare_digest(password.strip(), APP_PASSWORD)
                and secrets.compare_digest(security_code.strip(), security_code_env)
            ):
                record_login_attempt(ip_addr, True, "user")
                session.permanent = True
                session["authenticated"] = True
                session.modified = True
                return jsonify({"status": "ok", "redirect": "/"})

            # Neither matched
            lockout_info = record_login_attempt(ip_addr, False, "")
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

        # Try to use username column if it exists
        try:
            cur.execute("""
SELECT la.ip_address, la.success, la.attempted_at, la.user_agent, la.username, COALESCE(iname.ip_name, bi.ip_name, '') as ip_name
FROM login_attempts la
LEFT JOIN ip_names iname ON la.ip_address = iname.ip_address
LEFT JOIN blocked_ips bi ON la.ip_address = bi.ip_address
ORDER BY la.attempted_at DESC LIMIT 200""")
            rows = [dict(r) for r in cur.fetchall()]
        except psycopg2.errors.UndefinedColumn:
            # If username column doesn't exist, add it and retry
            log.info("username column missing from login_attempts, attempting to add it")
            conn.rollback()
            cur.close()
            conn.close()

            # Get fresh connection and add column
            conn = get_db()
            cur = conn.cursor()
            cur.execute("ALTER TABLE login_attempts ADD COLUMN username TEXT DEFAULT ''")
            conn.commit()
            cur.close()
            conn.close()

            # Get another fresh connection and retry the query
            conn = get_db()
            cur = conn.cursor()
            cur.execute("""
SELECT la.ip_address, la.success, la.attempted_at, la.user_agent, la.username, COALESCE(iname.ip_name, bi.ip_name, '') as ip_name
FROM login_attempts la
LEFT JOIN ip_names iname ON la.ip_address = iname.ip_address
LEFT JOIN blocked_ips bi ON la.ip_address = bi.ip_address
ORDER BY la.attempted_at DESC LIMIT 200""")
            rows = [dict(r) for r in cur.fetchall()]

        cur.close()
        conn.close()

        for r in rows:
            r["attempted_at"] = r["attempted_at"].isoformat() if r["attempted_at"] else None
            r["is_valid_user"] = is_valid_username(r.get("username", ""))

        return jsonify({"attempts": rows})
    except (ValueError, psycopg2.OperationalError) as e:
        log.warning(f"Database connection error in login attempts: {e}")
        return jsonify({"error": "Database connection failed. Please check system configuration.", "attempts": []}), 500
    except Exception as e:
        log.exception("Error fetching login attempts")
        return jsonify({"error": str(e), "attempts": []}), 500


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
    """Check if request is genuinely from loopback.

    Debug endpoints rely on this. We must not trust forwarded headers here,
    or an attacker could enable debug routes by spoofing X-Forwarded-For.
    A request is localhost only when there are no proxy headers AND
    remote_addr is loopback.
    """
    if request.headers.get('X-Forwarded-For') or request.headers.get('X-Real-IP') \
            or request.headers.get('CF-Connecting-IP') or request.headers.get('True-Client-IP'):
        return False
    return request.remote_addr in ('127.0.0.1', '::1')


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
        return jsonify({"error": "Failed to toggle lockdown"}), 500


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
            return jsonify({"error": "Invalid IP address format"}), 400

        if block_ip(ip_addr, reason, ip_name):
            return jsonify({"status": "blocked", "ip": ip_addr})
        else:
            return jsonify({"error": "Failed to block IP"}), 500
    except Exception as e:
        log.exception("Error blocking IP")
        return jsonify({"error": "Failed to process IP block request"}), 500


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
        return jsonify({"error": "Failed to process IP unblock request"}), 500


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
        return jsonify({"error": "Failed to track IP name"}), 500


@app.route("/api/csrf-token")
def api_csrf_token():
    """Get CSRF token for form submissions"""
    import secrets
    if 'csrf_token' not in session:
        session['csrf_token'] = secrets.token_hex(32)
    return jsonify({"csrf_token": session.get('csrf_token')})


@app.route("/api/sync-status")
def api_sync_status():
    """Report which calendar feeds last failed to fetch and when."""
    label_for = {
        u_canvas_ical(): "Canvas",
        u_personal_ical(): "Personal",
        u_sports_ical(): "Sports",
        u_job_schedule_ical(): "Job",
        RED_DAY_ICAL_URL: "Red Day",
        WHITE_DAY_ICAL_URL: "White Day",
    }
    cutoff = datetime.now(TZ) - timedelta(hours=6)
    issues = []
    with _ical_sync_lock:
        snapshot = dict(_ical_last_error)
    for url, info in snapshot.items():
        try:
            at = datetime.fromisoformat(info["at"])
        except Exception:
            continue
        if at < cutoff:
            continue
        issues.append({
            "feed": label_for.get(url, "Calendar"),
            "at": info["at"],
            "message": info.get("msg", ""),
        })
    return jsonify({"issues": issues})


def _pomodoro_row():
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, estimate_minutes, started_at, paused_at, accumulated_seconds, active "
                    "FROM timer_state WHERE id = 1")
        row = cur.fetchone()
        cur.close()
        return row
    finally:
        conn.close()


def _pomodoro_state_payload(row):
    if not row:
        return {"active": False, "paused": False, "elapsed_seconds": 0, "estimate_minutes": 25}
    elapsed = float(row["accumulated_seconds"] or 0)
    paused = row["active"] and row["paused_at"] is not None
    if row["active"] and not paused and row["started_at"] is not None:
        elapsed += (datetime.now(TZ) - row["started_at"]).total_seconds()
    return {
        "active": bool(row["active"]),
        "paused": bool(paused),
        "elapsed_seconds": max(0, int(elapsed)),
        "estimate_minutes": float(row["estimate_minutes"] or 25),
    }


@app.route("/api/pomodoro/state")
def api_pomodoro_state():
    return jsonify(_pomodoro_state_payload(_pomodoro_row()))


@app.route("/api/pomodoro/start", methods=["POST"])
def api_pomodoro_start():
    data = request.get_json(force=True, silent=True) or {}
    try:
        minutes = float(data.get("minutes", 25))
    except (TypeError, ValueError):
        minutes = 25.0
    if minutes <= 0 or minutes > 240:
        return jsonify({"error": "minutes must be between 0 and 240"}), 400
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE timer_state SET estimate_minutes=%s, started_at=NOW(), "
            "paused_at=NULL, accumulated_seconds=0, active=TRUE, "
            "assignment_uid='', assignment_title='Pomodoro', class_name='' "
            "WHERE id=1",
            (minutes,),
        )
        conn.commit()
        cur.close()
    finally:
        conn.close()
    return jsonify(_pomodoro_state_payload(_pomodoro_row()))


@app.route("/api/pomodoro/pause", methods=["POST"])
def api_pomodoro_pause():
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT active, paused_at, started_at, accumulated_seconds FROM timer_state WHERE id=1")
        row = cur.fetchone()
        if not row or not row["active"]:
            cur.close()
            return jsonify(_pomodoro_state_payload(row))
        if row["paused_at"] is None:
            elapsed = float(row["accumulated_seconds"] or 0)
            if row["started_at"] is not None:
                elapsed += (datetime.now(TZ) - row["started_at"]).total_seconds()
            cur.execute(
                "UPDATE timer_state SET paused_at=NOW(), accumulated_seconds=%s WHERE id=1",
                (elapsed,),
            )
        else:
            cur.execute(
                "UPDATE timer_state SET paused_at=NULL, started_at=NOW() WHERE id=1",
            )
        conn.commit()
        cur.close()
    finally:
        conn.close()
    return jsonify(_pomodoro_state_payload(_pomodoro_row()))


@app.route("/api/pomodoro/stop", methods=["POST"])
def api_pomodoro_stop():
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE timer_state SET active=FALSE, paused_at=NULL, started_at=NULL, "
            "accumulated_seconds=0 WHERE id=1"
        )
        conn.commit()
        cur.close()
    finally:
        conn.close()
    return jsonify(_pomodoro_state_payload(_pomodoro_row()))


@app.route("/manifest.json")
def pwa_manifest():
    return (
        render_template("manifest.json"),
        200,
        {"Content-Type": "application/manifest+json"},
    )


@app.route("/sw.js")
def pwa_service_worker():
    return (
        render_template("sw.js"),
        200,
        {"Content-Type": "application/javascript", "Service-Worker-Allowed": "/"},
    )


@app.route("/")
def index():
    return render_template("index.html", tz=str(get_tz()))


@app.route("/api/assignments")
def api_assignments():
    start = time.time()
    try:
        t1 = time.time()
        cal = fetch_ical(u_canvas_ical())
        log.info(f"/api/assignments: fetch_ical took {time.time()-t1:.2f}s")
        if cal is None:
            return jsonify({"assignments": [], "error": "Failed to fetch Canvas calendar."})
        t2 = time.time()
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT assignment_title, submitted FROM completions")
        completion_rows = cur.fetchall()
        submitted_titles = set(r["assignment_title"] for r in completion_rows if r["submitted"])
        done_titles = set(r["assignment_title"] for r in completion_rows if not r["submitted"])
        cur.execute("SELECT uid, minutes FROM assignment_estimates")
        custom_estimates = {r["uid"]: r["minutes"] for r in cur.fetchall()}
        cur.close()
        conn.close()
        log.info(f"/api/assignments: db query took {time.time()-t2:.2f}s")
        t3 = time.time()
        assignments = get_canvas_assignments_with_overdue(cal)
        result = []
        for a in assignments:
            if a["title"] in submitted_titles:
                continue
            uid = a.get("uid", "")
            if uid in custom_estimates:
                a["estimate_minutes"] = custom_estimates[uid]
                a["estimate_custom"] = True
            else:
                a["estimate_minutes"] = estimate_assignment(a["title"], a["class_name"])
                a["estimate_custom"] = False
            if a["title"] in done_titles:
                a["done"] = True
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


@app.route("/api/day-info")
def api_day_info():
    date_str = request.args.get("date", "")
    try:
        d = date.fromisoformat(date_str)
    except Exception:
        return jsonify({"error": "invalid date"}), 400
    dtype = get_day_type(d)
    hours = get_school_hours(d)
    result = {"date": date_str, "day_type": dtype, "is_school_day": dtype is not None}
    if hours:
        sh, sm, eh, em = hours
        result["school_start"] = "%d:%02d %s" % (sh % 12 or 12, sm, "AM" if sh < 12 else "PM")
        result["school_end"] = "%d:%02d %s" % (eh % 12 or 12, em, "AM" if eh < 12 else "PM")
    return jsonify(result)


@app.route("/api/calendar")
def api_calendar():
    start = time.time()
    try:
        days = int(request.args.get("days", 30))
        # Validate days parameter: must be between 1 and 365
        days = max(1, min(days, 365))
    except (ValueError, TypeError):
        days = 30
    events = []
    today = datetime.now(TZ).date()

    # Resolve user-scoped calendar URLs in the request thread. Flask's `session`
    # is bound to the request context and is NOT accessible from worker threads,
    # so calling u_*_ical() inside the ThreadPoolExecutor below silently falls
    # back to the (usually empty) env vars and the user's saved URLs are ignored.
    personal_url = u_personal_ical()
    sports_url   = u_sports_ical()
    job_url      = u_job_schedule_ical()
    canvas_url   = u_canvas_ical()

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

    def get_personal():
        return fetch_source("personal", personal_url,
                            lambda cal, d: [dict(e, source="personal") for e in parse_calendar_events(cal, days_ahead=d)])

    def get_sports():
        return fetch_source("sports", sports_url,
                            lambda cal, d: [dict(e, source="sports") for e in parse_calendar_events(cal, days_ahead=d)])

    def get_job():
        return fetch_source("job", job_url,
                            lambda cal, d: [dict(e, source="job") for e in parse_calendar_events(cal, days_ahead=d)])

    def get_canvas():
        result = []
        if not canvas_url:
            return result
        try:
            t = time.time()
            cal = fetch_ical(canvas_url)
            elapsed = time.time() - t
            if elapsed > 8:
                log.warning(f"/api/calendar: canvas fetch took {elapsed:.2f}s (slow)")
            else:
                log.info(f"/api/calendar: canvas took {elapsed:.2f}s")
            if cal:
                for a in get_canvas_assignments_with_overdue(cal):
                    result.append({
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
        return result

    # Fetch all iCal sources concurrently
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {
            executor.submit(get_personal): "personal",
            executor.submit(get_sports): "sports",
            executor.submit(get_job): "job",
            executor.submit(get_canvas): "canvas",
        }
        for future in as_completed(futures):
            source = futures[future]
            try:
                events.extend(future.result())
            except Exception as e:
                log.warning(f"/api/calendar: {source} future failed: {e}")

    try:
        day_events = fetch_day_calendar_events(today, days_ahead=days)
        events.extend(day_events)
        log.info(f"/api/calendar: day calendar added {len(day_events)} events")
    except Exception as e:
        log.warning(f"/api/calendar: day calendar failed: {e}")

    events.sort(key=lambda x: x.get("start_iso", ""))
    log.info(f"/api/calendar: total took {time.time()-start:.2f}s with {len(events)} events")
    return jsonify({"events": events})


@app.route("/api/powerschool/grades")
def api_powerschool_grades():
    """Return cached PowerSchool grades. Scrapes live if cache is cold."""
    if not _ps_configured():
        return jsonify({"error": "PowerSchool credentials not configured (POWER_USERN / POWER_PASS)"}), 503
    grades = ps_grades()
    return jsonify({"grades": grades, "count": len(grades), "configured": True})


@app.route("/api/powerschool/attendance")
def api_powerschool_attendance():
    """Return cached PowerSchool attendance summary."""
    if not _ps_configured():
        return jsonify({"error": "PowerSchool credentials not configured"}), 503
    att = ps_attendance()
    return jsonify({"attendance": att, "configured": True})


@app.route("/api/powerschool/refresh", methods=["POST"])
def api_powerschool_refresh():
    """Force a fresh scrape of PowerSchool data."""
    if not _ps_configured():
        return jsonify({"error": "PowerSchool credentials not configured"}), 503
    grades = ps_refresh_cache()
    return jsonify({"grades": grades, "count": len(grades), "refreshed": True})


@app.route("/api/powerschool/debug")
def api_powerschool_debug():
    """Run a fresh screenshot+vision extraction and return the raw result."""
    if not _ps_configured():
        return jsonify({"error": "PowerSchool credentials not configured"}), 503
    with _simple_cache_lock:
        _simple_cache.pop("ps:data", None)
    result = _ps_screenshot_and_extract()
    return jsonify(result)


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


@app.route("/api/insight/weekly", methods=["GET"])
def api_insight_weekly():
    """Return the current weekly insight, if one is cached."""
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT generated_at, content FROM insight_cache WHERE id = 1")
    row = cur.fetchone()
    cur.close()
    conn.close()
    if not row or not row.get("content"):
        return jsonify({"content": "", "generated_at": None})
    return jsonify({
        "content": row["content"],
        "generated_at": row["generated_at"].isoformat() if row["generated_at"] else None,
    })


@app.route("/api/insight/weekly/generate", methods=["POST"])
def api_insight_weekly_generate():
    """Force-generate a fresh weekly insight; returns the new row when ready."""
    generate_weekly_insight(force=True)
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT generated_at, content FROM insight_cache WHERE id = 1")
    row = cur.fetchone()
    cur.close()
    conn.close()
    if not row or not row.get("content"):
        return jsonify({"content": "", "generated_at": None}), 503
    return jsonify({
        "content": row["content"],
        "generated_at": row["generated_at"].isoformat() if row["generated_at"] else None,
    })


# ── Mem0 Memory Management Endpoints ─────────────────────────────────────────

@app.route("/api/memories", methods=["GET"])
def api_memories_get():
    """List all stored Mem0 long-term memories for the student."""
    if not MEM0_API_KEY:
        return jsonify({"memories": [], "configured": False})
    try:
        client = _get_mem0_client()
        if not client:
            return jsonify({"memories": [], "configured": False})
        all_mems = client.get_all(user_id="student")
        memories = [
            {
                "id": m.get("id", ""),
                "memory": m.get("memory", ""),
                "created_at": m.get("created_at", ""),
            }
            for m in (all_mems or [])
        ]
        return jsonify({"memories": memories, "configured": True, "count": len(memories)})
    except Exception as e:
        log.error("api_memories_get error: %s", e)
        return jsonify({"error": str(e)}), 500


@app.route("/api/memories/<memory_id>", methods=["DELETE"])
def api_memories_delete(memory_id):
    """Delete a specific memory by its Mem0 memory_id."""
    if not MEM0_API_KEY:
        return jsonify({"error": "MEM0_API_KEY not configured"}), 400
    try:
        client = _get_mem0_client()
        if not client:
            return jsonify({"error": "Mem0 client unavailable"}), 500
        client.delete(memory_id)
        return jsonify({"status": "ok"})
    except Exception as e:
        log.error("api_memories_delete error: %s", e)
        return jsonify({"error": str(e)}), 500


# ── People Profiles Endpoints ─────────────────────────────────────────────────

@app.route("/api/people", methods=["GET"])
def api_people_list():
    """List all people Jarvis has built profiles for."""
    import json as _json
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id, name, relationship, facts, created_at, updated_at FROM people_profiles ORDER BY updated_at DESC")
    rows = cur.fetchall()
    cur.close()
    conn.close()
    people = []
    for r in rows:
        try:
            facts = _json.loads(r["facts"] or "[]")
        except Exception:
            facts = []
        people.append({
            "id": r["id"],
            "name": r["name"],
            "relationship": r["relationship"],
            "facts": facts,
            "fact_count": len(facts),
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
        })
    return jsonify({"people": people, "count": len(people)})


@app.route("/api/people/<int:person_id>", methods=["GET"])
def api_people_get(person_id):
    """Get a single person's full profile by DB id."""
    import json as _json
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id, name, relationship, facts, created_at, updated_at FROM people_profiles WHERE id = %s", (person_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    if not row:
        return jsonify({"error": "Not found"}), 404
    try:
        facts = _json.loads(row["facts"] or "[]")
    except Exception:
        facts = []
    return jsonify({
        "id": row["id"],
        "name": row["name"],
        "relationship": row["relationship"],
        "facts": facts,
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
    })


@app.route("/api/people/<int:person_id>", methods=["DELETE"])
def api_people_delete(person_id):
    """Delete a person's profile."""
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM people_profiles WHERE id = %s", (person_id,))
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({"status": "ok"})


@app.route("/api/complete", methods=["POST"])
def api_complete():
    uid = _uid()
    data = request.get_json(force=True) or {}
    title = str(data.get("title", ""))[:300]
    class_name = str(data.get("class_name", ""))[:100]
    estimate = float(data.get("estimate_minutes", 30))
    if not title:
        return jsonify({"error": "title required"}), 400
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
INSERT INTO completions (assignment_title, class_name, duration_minutes, estimate_minutes, timed, user_id)
VALUES (%s, %s, 0, %s, FALSE, %s)""", (title, class_name, estimate, uid))
    # Complete any auto-promoted task for this assignment
    if uid:
        cur.execute(
            "UPDATE tasks SET completed=TRUE, completed_at=NOW() "
            "WHERE title=%s AND completed=FALSE AND notes LIKE 'Overdue Canvas assignment%%' AND user_id=%s",
            (title, uid),
        )
        cur.execute(
            "UPDATE canvas_assignments_cache SET promoted_to_task=FALSE WHERE title=%s AND (user_id=%s OR user_id IS NULL)",
            (title, uid),
        )
    else:
        cur.execute(
            "UPDATE tasks SET completed=TRUE, completed_at=NOW() "
            "WHERE title=%s AND completed=FALSE AND notes LIKE 'Overdue Canvas assignment%%'",
            (title,),
        )
        cur.execute(
            "UPDATE canvas_assignments_cache SET promoted_to_task=FALSE WHERE title=%s",
            (title,),
        )
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({"status": "ok"})


@app.route("/api/completions/today")
def api_completions_today():
    uid = _uid()
    today_start = datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    conn = get_db()
    cur = conn.cursor()
    if uid:
        cur.execute("""
SELECT assignment_title, class_name, duration_minutes, estimate_minutes, timed, completed_at
FROM completions WHERE completed_at >= %s AND user_id = %s ORDER BY completed_at DESC""", (today_start, uid))
    else:
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
        uid = _uid()
        today_start = datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
        uid_and = " AND user_id = %s" if uid else ""
        uid_p = (uid,) if uid else ()
        cur.execute(
            f"DELETE FROM completions WHERE assignment_title = %s AND class_name = %s AND completed_at >= %s{uid_and} ORDER BY completed_at DESC LIMIT 1",
            (title, class_name, today_start) + uid_p
        )

        conn.commit()
        cur.close()
        conn.close()

        return jsonify({"status": "ok"})
    except Exception as e:
        log.exception("Error uncompleting assignment")
        return jsonify({"error": str(e)}), 500


@app.route("/api/submit", methods=["POST"])
def api_submit():
    """Mark an assignment as submitted to Canvas (distinct from just 'done')."""
    data = request.get_json(force=True) or {}
    title = str(data.get("title", ""))[:300]
    class_name = str(data.get("class_name", ""))[:100]
    estimate = float(data.get("estimate_minutes", 30))
    if not title:
        return jsonify({"error": "title required"}), 400
    try:
        conn = get_db()
        cur = conn.cursor()
        # Update the most recent completion record to submitted=TRUE
        cur.execute("""
UPDATE completions SET submitted = TRUE
WHERE id = (
    SELECT id FROM completions
    WHERE assignment_title = %s
    ORDER BY completed_at DESC
    LIMIT 1
)""", (title,))
        # If no completion record existed yet, insert one directly as submitted
        if cur.rowcount == 0:
            cur.execute("""
INSERT INTO completions (assignment_title, class_name, duration_minutes, estimate_minutes, timed, submitted)
VALUES (%s, %s, 0, %s, FALSE, TRUE)""", (title, class_name, estimate))
        # Complete any auto-promoted overdue task for this assignment
        cur.execute(
            "UPDATE tasks SET completed=TRUE, completed_at=NOW() "
            "WHERE title=%s AND completed=FALSE AND notes LIKE 'Overdue Canvas assignment%%'",
            (title,),
        )
        cur.execute(
            "UPDATE canvas_assignments_cache SET promoted_to_task=FALSE WHERE title=%s",
            (title,),
        )
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({"status": "ok"})
    except Exception as e:
        log.exception("Error submitting assignment")
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

    # Personal + job calendar events today
    for _busy_url in filter(None, [u_personal_ical(), u_job_schedule_ical()]):
        try:
            _busy_cal = fetch_ical(_busy_url)
            if _busy_cal:
                for e in parse_calendar_events(_busy_cal, days_ahead=1):
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
        "display": f"{d.strftime('%-m/%-d/%Y')} is a {color} day" if color else f"{d.strftime('%-m/%-d/%Y')} (no school)"
    })


@app.route("/api/stats")
def api_stats():
    uid = _uid()
    uid_and = " AND user_id = %s" if uid else ""
    uid_p = (uid,) if uid else ()
    conn = get_db()
    cur = conn.cursor()
    week_start = datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    week_start -= timedelta(days=week_start.weekday())
    cur.execute(f"SELECT SUM(duration_minutes) as total FROM completions WHERE completed_at >= %s AND timed=TRUE{uid_and}", (week_start,) + uid_p)
    week_row = cur.fetchone()
    weekly_minutes = float(week_row["total"] or 0)
    cur.execute(f"""
SELECT class_name, AVG(duration_minutes) as avg, COUNT(*) as cnt
FROM completions WHERE timed=TRUE AND duration_minutes>0 AND class_name!=''{uid_and}
GROUP BY class_name ORDER BY avg DESC LIMIT 10""", uid_p)
    by_class = [{"class_name": r["class_name"], "avg_minutes": round(float(r["avg"]), 1), "count": r["cnt"]} for r in cur.fetchall()]
    cur.execute(f"""
SELECT AVG(ABS(duration_minutes - estimate_minutes) / NULLIF(estimate_minutes, 0)) as err
FROM completions WHERE timed=TRUE AND estimate_minutes>0 AND duration_minutes>0{uid_and}""", uid_p)
    acc_row = cur.fetchone()
    accuracy_pct = None
    if acc_row and acc_row["err"] is not None:
        accuracy_pct = round((1.0 - min(float(acc_row["err"]), 1.0)) * 100, 1)
    cur.execute(f"""
SELECT DISTINCT DATE(completed_at AT TIME ZONE 'America/Denver') as day
FROM completions{' WHERE user_id = %s' if uid else ''} ORDER BY day DESC LIMIT 30""", uid_p)
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
        uid = _uid()
        conn = get_db()
        cur = conn.cursor()
        if uid:
            cur.execute("""
SELECT id, title, notes, urgency, completed, completed_at, due_date, created_at,
       NULL as project_id, NULL as project_title, hidden_from_parent
FROM tasks WHERE user_id = %s ORDER BY completed ASC,
    CASE urgency WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END ASC,
    created_at ASC""", (uid,))
        else:
            cur.execute("""
SELECT id, title, notes, urgency, completed, completed_at, due_date, created_at,
       NULL as project_id, NULL as project_title, hidden_from_parent
FROM tasks ORDER BY completed ASC,
    CASE urgency WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END ASC,
    created_at ASC""")
        rows = [dict(r) for r in cur.fetchall()]
        # Sync all project tasks from active projects into the main task list.
        if uid:
            cur.execute("""
SELECT pt.id, pt.title, pt.notes, 'medium' as urgency,
       (pt.status = 'done') as completed, NULL as completed_at, pt.due_date,
       pt.created_at, pt.project_id, pt.assignee, p.title as project_title,
       p.hidden_from_parent
FROM project_tasks pt
JOIN projects p ON p.id = pt.project_id
WHERE p.status = 'active' AND p.user_id = %s
ORDER BY pt.created_at ASC""", (uid,))
        else:
            cur.execute("""
SELECT pt.id, pt.title, pt.notes, 'medium' as urgency,
       (pt.status = 'done') as completed, NULL as completed_at, pt.due_date,
       pt.created_at, pt.project_id, pt.assignee, p.title as project_title,
       p.hidden_from_parent
FROM project_tasks pt
JOIN projects p ON p.id = pt.project_id
WHERE p.status = 'active'
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
    urgency = str(data.get("urgency", "low")).lower()

    # Validate urgency
    if urgency not in ("high", "medium", "low"):
        urgency = "low"

    # Validate due_date format
    due_date = data.get("due_date") or None
    recurrence = str(data.get("recurrence", "")).lower() or None

    uid = _uid()
    conn = get_db()
    cur = conn.cursor()

    if recurrence and recurrence in ("daily", "weekly", "biweekly", "monthly"):
        cur.execute("""
INSERT INTO recurring_tasks (title, notes, urgency, recurrence, active, user_id)
VALUES (%s, %s, %s, %s, TRUE, %s) RETURNING id""",
                    (title, notes, urgency, recurrence, uid))
        task_id = cur.fetchone()["id"]

        calc_due_date = _calculate_next_due_date(recurrence)
        cur.execute("""
INSERT INTO tasks (title, notes, urgency, due_date, user_id)
VALUES (%s, %s, %s, %s, %s)""",
                    (title, f"[Recurring: {recurrence}]\n{notes}" if notes else f"[Recurring: {recurrence}]", urgency, calc_due_date, uid))
        cur.execute("UPDATE recurring_tasks SET last_created_at = NOW() WHERE id = %s", (task_id,))
    else:
        cur.execute("""
INSERT INTO tasks (title, notes, urgency, due_date, user_id) VALUES (%s, %s, %s, %s, %s) RETURNING id""",
                    (title, notes, urgency, due_date, uid))
        task_id = cur.fetchone()["id"]

    conn.commit()
    cur.close()
    conn.close()
    return jsonify({"id": task_id, "status": "ok"})


@app.route("/api/tasks/<int:task_id>", methods=["PATCH"])
def api_tasks_update(task_id):
    uid = _uid()
    data = request.get_json(force=True) or {}
    conn = get_db()
    try:
        cur = conn.cursor()
        uid_clause = " AND user_id = %s" if uid else ""
        uid_params = (uid,) if uid else ()
        if "completed" in data:
            completed = bool(data["completed"])
            cur.execute(
                f"UPDATE tasks SET completed=%s, completed_at=%s WHERE id=%s{uid_clause}",
                (completed, datetime.now(TZ) if completed else None, task_id) + uid_params)
            # Mark today's plan as needing update if task is completed
            if completed:
                today = datetime.now(TZ).date()
                cur.execute("""
UPDATE daily_plans SET needs_update = TRUE, last_updated_at = NOW()
WHERE plan_date = %s""", (today,))
        if "title" in data:
            title = str(data["title"])[:300]
            if title.strip():
                cur.execute(f"UPDATE tasks SET title=%s WHERE id=%s{uid_clause}", (title, task_id) + uid_params)
        if "urgency" in data:
            urgency = str(data["urgency"]).lower()
            if urgency in ("high", "medium", "low"):
                cur.execute(f"UPDATE tasks SET urgency=%s WHERE id=%s{uid_clause}", (urgency, task_id) + uid_params)
        if "notes" in data:
            cur.execute(f"UPDATE tasks SET notes=%s WHERE id=%s{uid_clause}", (str(data["notes"])[:2000], task_id) + uid_params)
        if "due_date" in data:
            due_date = data["due_date"] or None
            if due_date:
                try:
                    datetime.strptime(due_date, "%Y-%m-%d")
                except (ValueError, TypeError):
                    return jsonify({"error": "invalid due_date format"}), 400
            cur.execute(f"UPDATE tasks SET due_date=%s WHERE id=%s{uid_clause}", (due_date, task_id) + uid_params)
        if "hidden_from_parent" in data:
            cur.execute(f"UPDATE tasks SET hidden_from_parent=%s WHERE id=%s{uid_clause}",
                        (bool(data["hidden_from_parent"]), task_id) + uid_params)
        conn.commit()
        return jsonify({"status": "ok"})
    except Exception as e:
        conn.rollback()
        log.error(f"Task update error: {type(e).__name__}")
        return jsonify({"error": "Failed to update task"}), 500
    finally:
        cur.close()
        conn.close()


@app.route("/api/tasks/<int:task_id>", methods=["DELETE"])
def api_tasks_delete(task_id):
    uid = _uid()
    conn = get_db()
    try:
        cur = conn.cursor()
        if uid:
            cur.execute("DELETE FROM tasks WHERE id=%s AND user_id=%s", (task_id, uid))
        else:
            cur.execute("DELETE FROM tasks WHERE id=%s", (task_id,))
        conn.commit()
        return jsonify({"status": "ok"})
    except Exception as e:
        conn.rollback()
        log.error(f"Task delete error: {type(e).__name__}")
        return jsonify({"error": "Failed to delete task"}), 500
    finally:
        cur.close()
        conn.close()


# ── Parent API Endpoints ────────────────────────────────────────

@app.route("/api/parent/tasks", methods=["GET"])
def api_parent_tasks_get():
    """Get tasks created by parent (parent can only see their own tasks)."""
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("""
SELECT id, created_at, title, notes, urgency, completed, completed_at, due_date
FROM tasks WHERE created_by_parent = TRUE
ORDER BY completed ASC, created_at DESC""")
        rows = [dict(r) for r in cur.fetchall()]

        for row in rows:
            if row["created_at"]:
                row["created_at"] = row["created_at"].isoformat()
            if row["completed_at"]:
                row["completed_at"] = row["completed_at"].isoformat()
            if row["due_date"]:
                row["due_date"] = row["due_date"].isoformat()

        return jsonify({"tasks": rows})
    except Exception as e:
        log.error(f"Parent tasks GET error: {type(e).__name__}")
        return jsonify({"error": "Failed to load tasks"}), 500
    finally:
        cur.close()
        conn.close()


@app.route("/api/parent/tasks", methods=["POST"])
def api_parent_tasks_create():
    """Create a task from parent portal."""
    data = request.get_json(force=True) or {}
    title = (data.get("title") or "").strip()[:300]
    urgency = str(data.get("urgency", "medium")).lower()
    due_date = data.get("due_date")
    notes = (data.get("notes") or "").strip()[:2000]

    if not title:
        return jsonify({"error": "Task title required"}), 400

    if urgency not in ("high", "medium", "low"):
        urgency = "medium"

    # Validate due_date format
    if due_date:
        try:
            datetime.strptime(due_date, "%Y-%m-%d")
        except (ValueError, TypeError):
            return jsonify({"error": "invalid due_date format (use YYYY-MM-DD)"}), 400

    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("""
INSERT INTO tasks (title, urgency, due_date, notes, created_by_parent)
VALUES (%s, %s, %s, %s, TRUE) RETURNING id, created_at""",
            (title, urgency, due_date or None, notes))
        result = cur.fetchone()
        task_id = result["id"]
        created_at = result["created_at"]
        conn.commit()
        return jsonify({
            "id": task_id,
            "title": title,
            "urgency": urgency,
            "due_date": due_date,
            "notes": notes,
            "completed": False,
            "created_at": created_at.isoformat() if created_at else None
        }), 201
    except Exception as e:
        conn.rollback()
        log.error(f"Parent task creation error: {type(e).__name__}")
        return jsonify({"error": "Failed to create task"}), 500
    finally:
        cur.close()
        conn.close()


@app.route("/api/parent/tasks/<int:task_id>", methods=["PATCH"])
def api_parent_task_update(task_id):
    """Update a parent-created task (can update notes and mark complete)."""
    data = request.get_json(force=True) or {}

    conn = get_db()
    try:
        cur = conn.cursor()

        # Verify task was created by parent
        cur.execute("SELECT created_by_parent FROM tasks WHERE id=%s", (task_id,))
        row = cur.fetchone()
        if not row or not row["created_by_parent"]:
            return jsonify({"error": "Task not found or not created by parent"}), 404

        updates = []
        params = []

        if "notes" in data:
            updates.append("notes = %s")
            params.append(str(data["notes"])[:2000])

        if "completed" in data:
            updates.append("completed = %s")
            params.append(bool(data["completed"]))
            if data["completed"]:
                updates.append("completed_at = NOW()")

        if not updates:
            return jsonify({"error": "Nothing to update"}), 400

        params.append(task_id)
        query = f"UPDATE tasks SET {', '.join(updates)} WHERE id = %s"
        cur.execute(query, params)
        conn.commit()
        return jsonify({"status": "ok"})
    except Exception as e:
        conn.rollback()
        log.error(f"Parent task update error: {type(e).__name__}")
        return jsonify({"error": "Failed to update task"}), 500
    finally:
        cur.close()
        conn.close()


@app.route("/api/parent/daily-plan", methods=["GET"])
def api_parent_daily_plan():
    """Return today's daily plan for parent view. Hidden task items show as Private."""
    today = datetime.now(TZ).date()
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id FROM daily_plans WHERE plan_date = %s", (today,))
        plan = cur.fetchone()
        if not plan:
            return jsonify({"exists": False, "items": []})

        cur.execute("""
SELECT dpi.id, dpi.item_type, dpi.item_id, dpi.item_title,
       dpi.scheduled_start_time, dpi.scheduled_end_time,
       dpi.completed, dpi.order_index
FROM daily_plan_items dpi
WHERE dpi.plan_id = %s
ORDER BY dpi.order_index ASC, dpi.scheduled_start_time ASC""", (plan["id"],))
        items = [dict(r) for r in cur.fetchall()]

        # Collect task IDs to check hidden status
        task_ids = [int(i["item_id"]) for i in items
                    if i["item_type"] == "task" and i["item_id"] and i["item_id"].isdigit()]
        hidden_task_ids = set()
        if task_ids:
            cur.execute("SELECT id FROM tasks WHERE id = ANY(%s) AND hidden_from_parent = TRUE",
                        (task_ids,))
            hidden_task_ids = {r["id"] for r in cur.fetchall()}

        result = []
        for item in items:
            is_hidden = (item["item_type"] == "task"
                         and item["item_id"]
                         and item["item_id"].isdigit()
                         and int(item["item_id"]) in hidden_task_ids)
            result.append({
                "id": item["id"],
                "item_type": item["item_type"] if not is_hidden else "private",
                "item_title": item["item_title"] if not is_hidden else "",
                "scheduled_start_time": str(item["scheduled_start_time"]),
                "scheduled_end_time": str(item["scheduled_end_time"]),
                "completed": item["completed"],
            })

        return jsonify({"exists": True, "items": result})
    except Exception as e:
        log.exception(f"Parent daily plan error: {e}")
        return jsonify({"error": "Failed to load plan"}), 500
    finally:
        cur.close()
        conn.close()


@app.route("/recurring-tasks", methods=["GET"])
def api_recurring_tasks_get():
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("""
SELECT id, title, notes, urgency, recurrence, last_created_at, active, created_at
FROM recurring_tasks WHERE active = TRUE ORDER BY created_at DESC""")
        rows = [dict(r) for r in cur.fetchall()]
        for r in rows:
            if r["last_created_at"]:
                r["last_created_at"] = r["last_created_at"].isoformat()
            if r["created_at"]:
                r["created_at"] = r["created_at"].isoformat()
        return jsonify({"recurring_tasks": rows})
    except Exception as e:
        log.error(f"Recurring tasks GET error: {type(e).__name__}")
        return jsonify({"error": "Failed to load recurring tasks"}), 500
    finally:
        cur.close()
        conn.close()


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
    try:
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
        return jsonify({"status": "ok", "id": task_id}), 201
    except Exception as e:
        conn.rollback()
        log.error(f"Recurring task creation error: {type(e).__name__}")
        return jsonify({"error": "Failed to create recurring task"}), 500
    finally:
        cur.close()
        conn.close()


@app.route("/api/recurring-tasks/<int:task_id>", methods=["PATCH"])
def api_recurring_tasks_update(task_id):
    data = request.get_json(force=True) or {}
    conn = get_db()
    try:
        cur = conn.cursor()

        if "active" in data:
            cur.execute("UPDATE recurring_tasks SET active=%s WHERE id=%s", (bool(data["active"]), task_id))

        conn.commit()
        return jsonify({"status": "ok"})
    except Exception as e:
        conn.rollback()
        log.error(f"Recurring task update error: {type(e).__name__}")
        return jsonify({"error": "Failed to update recurring task"}), 500
    finally:
        cur.close()
        conn.close()


@app.route("/api/recurring-tasks/<int:task_id>", methods=["DELETE"])
def api_recurring_tasks_delete(task_id):
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM recurring_tasks WHERE id=%s", (task_id,))
        conn.commit()
        return jsonify({"status": "ok"})
    except Exception as e:
        conn.rollback()
        log.error(f"Recurring task delete error: {type(e).__name__}")
        return jsonify({"error": "Failed to delete recurring task"}), 500
    finally:
        cur.close()
        conn.close()


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
            cal = fetch_ical(u_canvas_ical())
            if cal:
                assignments = get_canvas_assignments_with_overdue(cal)
        except Exception as e:
            log.warning(f"Could not fetch assignments for suggestions: {e}")

        # Fetch calendar events
        calendar_events = []
        for _sug_url in filter(None, [u_personal_ical(), u_sports_ical(), u_job_schedule_ical()]):
            try:
                _sug_cal = fetch_ical(_sug_url)
                if _sug_cal:
                    calendar_events.extend(parse_calendar_events(_sug_cal, days_ahead=7))
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

        # Prompt Claude to suggest tasks. The default is empty — silence is the
        # correct answer most days. Only emit a suggestion when withholding it
        # would clearly hurt the student.
        prompt = f"""You are reviewing a high school student's situation to decide whether ANY new task should be added to their list. Returning [] is the default and most common correct outcome. Most reviews end with nothing to add. You are not paid by the suggestion.

A suggestion is only justified if ALL of these are true:
1. There is concrete preparation, study, or drafting work that is NOT already represented in the pending assignments or existing tasks below.
2. Skipping it would predictably cause the student to be unprepared, late, or to scramble.
3. You can name the specific artifact or outcome (e.g. "outline for English essay", "review session for AP Chem unit test on Friday") — not a vague "review notes" or "stay organised".
4. The trigger falls within 14 days.

Disqualifiers — return [] if any of these describe your only candidates:
- Routine attendance: sports games, dentist, lunch, social events, club meetings (even if on the calendar)
- Anything already tracked as an assignment or already in the existing task list (even loosely)
- Filler that exists to "look helpful" with no specific deadline-driven trigger
- Self-improvement nudges ("review notes", "stay organised", "get ahead")

Quality bar: if you cannot finish the sentence "Without this task, the student will probably miss ___" with a specific named consequence, do not add it.

Pending assignments: {asgn_text}
Upcoming calendar events: {event_text}
Existing tasks: {existing_text}

Output: a JSON array. Almost always []. At most ONE suggestion in normal weeks; TWO only on a genuinely heavy week. Never three. No prose, no apology, no explanation outside the array.

Examples of correct responses:

[]

[{{"title": "Study for AP Chem unit test", "urgency": "high", "due_date": "2026-05-08", "reason": "Test in 5 days and no study task exists yet"}}]

Schema: [{{"title": "...", "urgency": "high|medium|low", "due_date": "YYYY-MM-DD", "reason": "one sentence: the specific trigger and what would go wrong without this task"}}]"""

        client = anthropic.Anthropic(api_key=api_key, max_retries=3, timeout=60.0)
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
            return jsonify({"suggestions": valid_suggestions[:2]})  # Cap at 2 — empty is the norm

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
    uid = _uid()
    try:
        conn = get_db()
        cur = conn.cursor()
        uid_where = "WHERE user_id = %s" if uid else ""
        uid_p = (uid,) if uid else ()
        cur.execute(f"""
SELECT id, title, description, status, lead, members, last_checkin,
       checkin_interval_days, created_at, hidden_from_parent,
       CASE WHEN last_checkin IS NULL OR
           NOW() - last_checkin > make_interval(days => checkin_interval_days)
       THEN TRUE ELSE FALSE END as needs_checkin
FROM projects
{uid_where}
ORDER BY CASE status WHEN 'active' THEN 0 WHEN 'paused' THEN 1 WHEN 'done' THEN 2 ELSE 3 END,
         created_at DESC""", uid_p)
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
    uid = _uid()
    data = request.get_json(force=True) or {}
    title = str(data.get("title", "")).strip()[:300]
    if not title:
        return jsonify({"error": "title required"}), 400
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
INSERT INTO projects (title, description, status, lead, members, checkin_interval_days, last_checkin, user_id)
VALUES (%s, %s, %s, %s, %s, %s, NOW(), %s) RETURNING id""",
                (title, str(data.get("description", ""))[:2000],
                 str(data.get("status", "active")),
                 str(data.get("lead", ""))[:200],
                 str(data.get("members", ""))[:500],
                 int(data.get("checkin_interval_days", 7)), uid))
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
        "hidden_from_parent": ("hidden_from_parent", lambda v: bool(v)),
    }

    for key, (db_field, transform) in fields_map.items():
        if key in data:
            try:
                updates[db_field] = transform(data[key])
            except (TypeError, ValueError):
                if key == "checkin_interval_days":
                    updates[db_field] = 7

    # Track when a project is marked done (for 7-day auto-delete)
    if "status" in data:
        new_status = str(data["status"]).strip().lower()
        if new_status == "done":
            updates["done_at"] = pgsql.SQL("NOW()")
        else:
            updates["done_at"] = pgsql.SQL("NULL")

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
    try:
        cur = conn.cursor()
        try:
            cur.execute("""
SELECT id, title, notes, assignee, status, due_date, created_at
FROM project_tasks WHERE project_id=%s ORDER BY created_at ASC""", (project_id,))
            rows = [dict(r) for r in cur.fetchall()]
        finally:
            cur.close()
    finally:
        conn.close()
    for r in rows:
        if r["due_date"]:
            r["due_date"] = str(r["due_date"])
        r["created_at"] = r["created_at"].isoformat()
    return jsonify({"tasks": rows})


@app.route("/api/projects/<int:project_id>/tasks", methods=["POST"])
def api_project_tasks_create(project_id):
    try:
        data = request.get_json(force=True) or {}
        title = str(data.get("title", "")).strip()[:300]
        if not title:
            return jsonify({"error": "title required"}), 400
        notes = str(data.get("notes", ""))[:2000]
        assignee = str(data.get("assignee", ""))[:100]
        status = str(data.get("status", "pending"))
        due_date = data.get("due_date") or None
        conn = get_db()
        try:
            cur = conn.cursor()
            try:
                cur.execute("""
INSERT INTO project_tasks (project_id, title, notes, assignee, status, due_date)
VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
                            (project_id, title, notes, assignee, status, due_date))
                new_id = cur.fetchone()["id"]
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                cur.close()
        finally:
            conn.close()
        return jsonify({"id": new_id, "status": "ok"})
    except Exception as e:
        log.exception(f"Error adding task to project {project_id}")
        return jsonify({"error": f"Failed to add task: {str(e)}"}), 500


@app.route("/api/projects/<int:project_id>/tasks/<int:task_id>", methods=["PATCH"])
def api_project_tasks_update(project_id, task_id):
    data = request.get_json(force=True) or {}
    conn = get_db()
    try:
        cur = conn.cursor()
        try:
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
        except Exception:
            conn.rollback()
            raise
        finally:
            cur.close()
    except Exception as e:
        log.error(f"Project task update error: {type(e).__name__}: {e}")
        return jsonify({"error": "Failed to update project task"}), 500
    finally:
        conn.close()
    return jsonify({"status": "ok"})


@app.route("/api/projects/<int:project_id>/tasks/<int:task_id>", methods=["DELETE"])
def api_project_tasks_delete(project_id, task_id):
    conn = get_db()
    try:
        cur = conn.cursor()
        try:
            cur.execute("DELETE FROM project_tasks WHERE id=%s AND project_id=%s", (task_id, project_id))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cur.close()
    finally:
        conn.close()
    return jsonify({"status": "ok"})


@app.route("/api/config", methods=["GET"])
def api_config_get():
    uid = _uid()
    cfg = get_user_config(uid) if uid else get_config()
    return jsonify({
        "name": cfg.get("name", "Jarvis"),
        "morning_briefing_time": cfg.get("morning_briefing_time", "07:00"),
        "timer_cutoff_multiplier": cfg.get("timer_cutoff_multiplier", "2.0"),
        "has_api_key": bool(cfg.get("anthropic_api_key", "")),
        "weekly_recap_advisor": cfg.get("weekly_recap_advisor", "Mr. Goldberg"),
        "formal_signoff_name": cfg.get("formal_signoff_name", "Finley Thomas"),
        "timezone": cfg.get("timezone", "America/Denver"),
        "app_mode": cfg.get("app_mode", "school"),
        "is_summer_school": cfg.get("is_summer_school", "false") == "true",
        "has_summer_job": cfg.get("has_summer_job", "false") == "true",
        "has_job_schedule": bool(u_job_schedule_ical()),
        # Calendar URLs (per-user)
        "personal_ical_url":     cfg.get("personal_ical_url", ""),
        "canvas_ical_url":       cfg.get("canvas_ical_url", ""),
        "canvas_api_token":      "••••••••" if cfg.get("canvas_api_token", "") else "",
        "canvas_base_url":       cfg.get("canvas_base_url", ""),
        "sports_ical_url":       cfg.get("sports_ical_url", ""),
        "job_schedule_ical_url": cfg.get("job_schedule_ical_url", ""),
    })


@app.route("/api/config", methods=["POST"])
def api_config_post():
    data = request.get_json(force=True) or {}
    allowed = {
        "name", "morning_briefing_time", "timer_cutoff_multiplier", "anthropic_api_key",
        "weekly_recap_advisor", "formal_signoff_name", "timezone",
        "app_mode", "is_summer_school", "has_summer_job",
        "personal_ical_url", "canvas_ical_url", "canvas_api_token",
        "canvas_base_url", "sports_ical_url", "job_schedule_ical_url",
    }
    updates = {k: str(v)[:2000] for k, v in data.items() if k in allowed}
    # Skip masked Canvas token (UI sends •••••••• when unchanged)
    if updates.get("canvas_api_token", "").strip().startswith("•"):
        del updates["canvas_api_token"]
    if updates:
        if "timezone" in updates:
            try:
                ZoneInfo(updates["timezone"])
            except Exception:
                return jsonify({"status": "error", "message": "Invalid timezone"}), 400
        uid = _uid()
        if uid:
            set_user_config(updates, user_id=uid)
        else:
            set_config(updates)
        if "morning_briefing_time" in updates:
            schedule_briefing()
    return jsonify({"status": "ok"})


# ── Bucket List Endpoints ─────────────────────────────────────────────────────

@app.route("/api/bucket-list", methods=["GET"])
def api_bucket_list_get():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id, title, category, notes, completed, completed_at, created_at FROM bucket_list ORDER BY completed, created_at DESC")
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return jsonify([{
        "id": r["id"],
        "title": r["title"],
        "category": r["category"],
        "notes": r["notes"],
        "completed": r["completed"],
        "completed_at": r["completed_at"].isoformat() if r["completed_at"] else None,
        "created_at": r["created_at"].isoformat(),
    } for r in rows])


@app.route("/api/bucket-list", methods=["POST"])
def api_bucket_list_post():
    data = request.get_json(force=True) or {}
    title    = str(data.get("title",    "")).strip()[:500]
    category = str(data.get("category", "")).strip()[:100]
    notes    = str(data.get("notes",    "")).strip()[:2000]
    if not title:
        return jsonify({"error": "title required"}), 400
    conn = get_db()
    cur = conn.cursor()
    cur.execute("INSERT INTO bucket_list (title, category, notes) VALUES (%s, %s, %s) RETURNING id, created_at", (title, category, notes))
    row = cur.fetchone()
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({"id": row["id"], "title": title, "category": category, "notes": notes, "completed": False, "completed_at": None, "created_at": row["created_at"].isoformat()}), 201


@app.route("/api/bucket-list/<int:item_id>", methods=["PATCH"])
def api_bucket_list_patch(item_id):
    data = request.get_json(force=True) or {}
    conn = get_db()
    cur = conn.cursor()
    if "completed" in data:
        now_ts = datetime.now(get_tz()) if data["completed"] else None
        cur.execute("UPDATE bucket_list SET completed=%s, completed_at=%s WHERE id=%s", (bool(data["completed"]), now_ts, item_id))
    if "title" in data:
        cur.execute("UPDATE bucket_list SET title=%s WHERE id=%s", (str(data["title"])[:500], item_id))
    if "category" in data:
        cur.execute("UPDATE bucket_list SET category=%s WHERE id=%s", (str(data["category"])[:100], item_id))
    if "notes" in data:
        cur.execute("UPDATE bucket_list SET notes=%s WHERE id=%s", (str(data["notes"])[:2000], item_id))
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({"status": "ok"})


@app.route("/api/bucket-list/<int:item_id>", methods=["DELETE"])
def api_bucket_list_delete(item_id):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM bucket_list WHERE id=%s", (item_id,))
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({"status": "ok"})


# ── UV / Weather Endpoint ─────────────────────────────────────────────────────

@app.route("/api/weather-uv", methods=["GET"])
def api_weather_uv():
    """Fetch current UV index and basic weather using Open-Meteo (no API key needed).
    Defaults to Park City, UT coordinates."""
    lat = request.args.get("lat", "40.6461")
    lon = request.args.get("lon", "-111.4980")
    try:
        lat = float(lat)
        lon = float(lon)
    except ValueError:
        return jsonify({"error": "invalid coordinates"}), 400
    try:
        resp = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": lat,
                "longitude": lon,
                "current": "uv_index,temperature_2m,weathercode",
                "temperature_unit": "fahrenheit",
                "timezone": "auto",
                "forecast_days": 1,
            },
            timeout=8,
        )
        resp.raise_for_status()
        data = resp.json()
        current = data.get("current", {})
        uv = current.get("uv_index", 0)
        temp_f = current.get("temperature_2m")
        weathercode = current.get("weathercode", 0)
        avoid_desk = uv > 5
        return jsonify({
            "uv_index": uv,
            "temperature_f": temp_f,
            "weathercode": weathercode,
            "avoid_desk_work": avoid_desk,
            "alert": "UV is high — get outside before hitting the desk!" if avoid_desk else None,
        })
    except Exception as e:
        log.warning("weather-uv fetch error: %s", e)
        return jsonify({"error": "weather unavailable"}), 503


# ── Job Schedule Endpoint ─────────────────────────────────────────────────────

@app.route("/api/job-schedule", methods=["GET"])
def api_job_schedule():
    """Return upcoming job schedule events from u_job_schedule_ical()."""
    if not u_job_schedule_ical():
        return jsonify({"events": [], "configured": False})
    try:
        cal = fetch_ical(u_job_schedule_ical())
        if not cal:
            return jsonify({"events": [], "configured": True, "error": "could not fetch"})
        tz = get_tz()
        today = datetime.now(tz).date()
        window_end = today + timedelta(days=14)
        events_raw = recurring_ical_events.of(cal).between(
            datetime.combine(today, datetime.min.time()),
            datetime.combine(window_end, datetime.max.time()),
        )
        events = []
        for ev in events_raw:
            dtstart = ev.get("DTSTART")
            if not dtstart:
                continue
            dt = dtstart.dt
            if hasattr(dt, "date"):
                dt_date = dt.date()
            else:
                dt_date = dt
            events.append({
                "title": str(ev.get("SUMMARY", "Work")),
                "date": dt_date.isoformat(),
                "time": dt.strftime("%H:%M") if hasattr(dt, "strftime") and hasattr(dt, "hour") else None,
            })
        events.sort(key=lambda x: x["date"])
        return jsonify({"events": events, "configured": True})
    except Exception as e:
        log.warning("job-schedule error: %s", e)
        return jsonify({"events": [], "configured": True, "error": str(e)})


# ── Jarvis Tool Definitions ───────────────────────────────────────────────────

JARVIS_TOOLS = [
    {
        "name": "get_tasks",
        "description": "Retrieve all pending (incomplete) tasks. Returns list with id, title, urgency, due_date, notes.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "create_task",
        "description": "Create a new task for the student.",
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Task title"},
                "urgency": {"type": "string", "enum": ["high", "medium", "low"]},
                "due_date": {"type": "string", "description": "YYYY-MM-DD or omit if no deadline"},
                "notes": {"type": "string", "description": "Optional notes"},
            },
            "required": ["title", "urgency"],
        },
    },
    {
        "name": "complete_task",
        "description": "Mark a pending task as completed by its numeric ID.",
        "input_schema": {
            "type": "object",
            "properties": {"task_id": {"type": "integer"}},
            "required": ["task_id"],
        },
    },
    {
        "name": "delete_task",
        "description": "Permanently delete a task by its numeric ID.",
        "input_schema": {
            "type": "object",
            "properties": {"task_id": {"type": "integer"}},
            "required": ["task_id"],
        },
    },
    {
        "name": "update_task",
        "description": "Update a task's urgency, due date, or notes.",
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "integer"},
                "urgency": {"type": "string", "enum": ["high", "medium", "low"]},
                "due_date": {"type": "string", "description": "YYYY-MM-DD"},
                "notes": {"type": "string"},
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "get_assignments",
        "description": "Fetch upcoming Canvas assignments that have not been completed yet.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "complete_assignment",
        "description": "Log a Canvas assignment as completed. Use submitted=true when the student says they turned it in / submitted it to Canvas; use submitted=false (default) when they finished working on it but haven't turned it in yet.",
        "input_schema": {
            "type": "object",
            "properties": {
                "assignment_title": {"type": "string"},
                "class_name": {"type": "string"},
                "duration_minutes": {"type": "integer", "description": "How long it took (optional)"},
                "submitted": {"type": "boolean", "description": "True if the student submitted to Canvas, false if just done working"},
            },
            "required": ["assignment_title", "class_name"],
        },
    },
    {
        "name": "get_calendar_events",
        "description": "Get calendar events from all connected calendars for the next N days.",
        "input_schema": {
            "type": "object",
            "properties": {
                "days_ahead": {"type": "integer", "description": "Days to look ahead, default 7, max 30"},
            },
            "required": [],
        },
    },
    {
        "name": "log_stock_transaction",
        "description": "Record a stock buy or sell transaction in the portfolio tracker.",
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "Ticker symbol e.g. AAPL"},
                "action": {"type": "string", "enum": ["buy", "sell"]},
                "quantity": {"type": "number"},
                "price": {"type": "number", "description": "Price per share"},
                "date": {"type": "string", "description": "YYYY-MM-DD, defaults to today"},
                "notes": {"type": "string"},
            },
            "required": ["symbol", "action", "quantity", "price"],
        },
    },
    {
        "name": "save_stock_note",
        "description": "Save or update investment thesis, exit criteria, target price, or stop-loss for a stock.",
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string"},
                "thesis": {"type": "string"},
                "exit_criteria": {"type": "string"},
                "target_price": {"type": "number"},
                "stop_loss": {"type": "number"},
            },
            "required": ["symbol"],
        },
    },
    {
        "name": "get_portfolio",
        "description": "Get current stock holdings with quantities and average costs.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_projects",
        "description": "Get all active projects with their tasks and recent notes.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "create_project",
        "description": (
            "Create a multi-day or multi-step project, optionally seeded with sub-tasks. "
            "Use this for STUDY PLANS that span more than one day, multi-phase work "
            "(planning + research + execution), application processes, or any effort "
            "with three or more logical sub-steps. Do NOT use for single atomic tasks — "
            "use create_task for those. Pass the initial sub-tasks via the `tasks` array "
            "in one call rather than a long sequence of create_task calls."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Project name, e.g. 'AP Calc BC + Spanish Exam Prep'"},
                "description": {"type": "string", "description": "1-2 sentence summary of the project's goal and scope"},
                "checkin_interval_days": {"type": "integer", "description": "Days between check-ins; default 7, use 3 for active short projects, 14 for slow ones"},
                "tasks": {
                    "type": "array",
                    "description": "Initial sub-tasks for the project. Each is an object with title, optional due_date (YYYY-MM-DD), and optional notes.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string"},
                            "due_date": {"type": "string", "description": "YYYY-MM-DD"},
                            "notes": {"type": "string"},
                        },
                        "required": ["title"],
                    },
                },
            },
            "required": ["title"],
        },
    },
    {
        "name": "add_project_task",
        "description": "Add a single sub-task to an existing project. Use the project_id from get_projects.",
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "integer"},
                "title": {"type": "string"},
                "due_date": {"type": "string", "description": "YYYY-MM-DD"},
                "notes": {"type": "string"},
            },
            "required": ["project_id", "title"],
        },
    },
    {
        "name": "get_briefing",
        "description": "Get today's cached morning briefing summary with priorities, assignments, and schedule.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_grades",
        "description": (
            "Fetch the student's current Canvas grades across all active courses. "
            "Returns course name, current letter grade, and current numeric percentage. "
            "Use when the student asks about grades, GPA, how a class is going, "
            "or which classes need the most attention."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_assignment_details",
        "description": (
            "Pull the full Canvas description and rubric for a specific assignment so you can "
            "actually help with it. Use whenever the student asks for help on an assignment, "
            "wants the rubric, asks 'what do I need to do for X', or otherwise needs the actual "
            "instructions, not just the title. Pass the assignment title; you may also pass the "
            "course name to disambiguate. Requires Canvas API to be configured."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Assignment title (or close substring) to look up"},
                "course": {"type": "string", "description": "Optional course name hint to disambiguate"},
            },
            "required": ["title"],
        },
    },
    {
        "name": "save_memory",
        "description": (
            "Save an important fact, preference, or insight about the student to long-term memory "
            "so Jarvis can recall it in future conversations without being reminded. Use this "
            "proactively when the student shares something personally significant: goals, preferences, "
            "relationships, challenges, life events, study habits, or anything they'd want Jarvis to "
            "remember. Write the memory as a clear, self-contained third-person statement."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "memory": {
                    "type": "string",
                    "description": (
                        "A clear, standalone fact about the student written in third person. "
                        "E.g. 'Student's goal is a 4.0 GPA this semester' or "
                        "'Student prefers studying in the morning before 10 AM'."
                    ),
                },
            },
            "required": ["memory"],
        },
    },
    {
        "name": "remember_person",
        "description": (
            "Create or update a person's profile when the student shares NEW facts about them. "
            "Only call this when you have at least one of: the relationship type, or one or more new facts "
            "(grade/age, school, sport, personality trait, something they did together, etc.). "
            "Do NOT call it for bare name mentions with no new detail — 'I talked to Jake' alone is not enough. "
            "For public figures (teachers, coaches, local staff), you may also use web_search to look up "
            "additional public information before calling this tool."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "The person's name (first name, full name, or nickname as used by the student).",
                },
                "relationship": {
                    "type": "string",
                    "description": "How they relate to the student. E.g. 'friend', 'best friend', 'mom', 'dad', 'brother', 'teacher', 'coach', 'classmate', 'coworker', 'crush', etc.",
                },
                "facts": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "New facts about this person not already stored. Each is a clear standalone sentence. E.g. ['Plays on the basketball team', 'Is in 11th grade', 'Likes gaming'].",
                },
            },
            "required": ["name"],
        },
    },
    {
        "name": "get_person_profile",
        "description": (
            "Retrieve everything Jarvis knows about a specific person — facts, relationship, and any "
            "memories involving them. Use this when the student asks about someone, references them in "
            "context where past info would be useful, or to check what's already known before adding more."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "The person's name to look up.",
                },
            },
            "required": ["name"],
        },
    },
    {
        "name": "list_people",
        "description": "List all people Jarvis has profiles for — names, relationships, and fact count.",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    # ── Google Workspace tools ────────────────────────────────────────────────
    {
        "name": "create_email_draft",
        "description": (
            "Compose an email for the student to review and confirm before it is sent. "
            "IMPORTANT RULES: (1) Only call this when the student EXPLICITLY asks to send, compose, "
            "or reply to an email in their CURRENT message. (2) Never call this because a previous "
            "conversation turn had a draft — once staged, the student handles it via the UI. "
            "(3) Never call this more than once per response. "
            "The email will NOT be sent automatically — a confirmation card appears in the chat."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "to": {"type": "string", "description": "Recipient email address(es), comma-separated"},
                "cc": {"type": "string", "description": "CC addresses, comma-separated (optional)"},
                "subject": {"type": "string", "description": "Email subject line"},
                "body": {"type": "string", "description": "Full email body (plain text)"},
            },
            "required": ["to", "subject", "body"],
        },
    },
    {
        "name": "search_google_drive",
        "description": (
            "Search Google Drive for files by name or content. Returns file IDs, names, types, and "
            "modification dates. Use when the student asks about a document, notes, or file stored in Drive."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search terms, e.g. 'biology notes' or 'AP Calc study guide'"},
                "max_results": {"type": "integer", "description": "Max files to return (default 10, max 20)"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "read_google_drive_file",
        "description": (
            "Read the text content of a Google Drive file — supports Google Docs, Sheets (as CSV), "
            "Slides, and plain text. Pass the file ID from search_google_drive."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "file_id": {"type": "string", "description": "Google Drive file ID"},
                "file_name": {"type": "string", "description": "Optional: file name hint for context"},
            },
            "required": ["file_id"],
        },
    },
    {
        "name": "list_google_drive_files",
        "description": "List files in Google Drive. Optionally pass a folder_id to list a specific folder.",
        "input_schema": {
            "type": "object",
            "properties": {
                "folder_id": {"type": "string", "description": "Drive folder ID to list (omit for recent/all files)"},
                "max_results": {"type": "integer", "description": "Max files to return (default 20, max 50)"},
            },
            "required": [],
        },
    },
    {
        "name": "read_google_sheet",
        "description": "Read rows from a Google Sheets spreadsheet. Returns a 2-D array of cell values.",
        "input_schema": {
            "type": "object",
            "properties": {
                "spreadsheet_id": {"type": "string", "description": "Google Sheets file ID (from Drive search)"},
                "range": {"type": "string", "description": "A1-notation range like 'Sheet1!A1:Z100'; omit to read first sheet"},
            },
            "required": ["spreadsheet_id"],
        },
    },
    {
        "name": "list_google_classroom_courses",
        "description": "List the student's active Google Classroom courses (name, section, ID).",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_google_classroom_assignments",
        "description": (
            "Get coursework and assignments for a Google Classroom course. "
            "Use the course ID from list_google_classroom_courses."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "course_id": {"type": "string", "description": "Google Classroom course ID"},
            },
            "required": ["course_id"],
        },
    },
    {
        "name": "search_gmail",
        "description": (
            "Search Gmail for messages matching a query. Returns subjects, senders, dates, and snippets. "
            "Useful for finding emails from teachers, school notices, or assignment feedback."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Gmail search query, e.g. 'from:teacher@school.edu subject:grade'"},
                "max_results": {"type": "integer", "description": "Max messages to return (default 10, max 25)"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "read_gmail_message",
        "description": "Read the full body of a Gmail message by its ID (from search_gmail).",
        "input_schema": {
            "type": "object",
            "properties": {
                "message_id": {"type": "string", "description": "Gmail message ID"},
            },
            "required": ["message_id"],
        },
    },
    {
        "name": "create_google_doc",
        "description": "Create a new Google Doc with the given title and content. Returns the file ID and URL.",
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Document title"},
                "content": {"type": "string", "description": "Plain-text content to fill the document with"},
                "folder_id": {"type": "string", "description": "Optional Drive folder ID to create it in"},
            },
            "required": ["title", "content"],
        },
    },
    {
        "name": "update_google_doc",
        "description": "Replace the full text content of an existing Google Doc.",
        "input_schema": {
            "type": "object",
            "properties": {
                "document_id": {"type": "string", "description": "Google Docs document ID"},
                "content": {"type": "string", "description": "New plain-text content (replaces everything)"},
            },
            "required": ["document_id", "content"],
        },
    },
    {
        "name": "append_google_doc",
        "description": "Append text to the end of an existing Google Doc without erasing existing content.",
        "input_schema": {
            "type": "object",
            "properties": {
                "document_id": {"type": "string", "description": "Google Docs document ID"},
                "content": {"type": "string", "description": "Text to append"},
            },
            "required": ["document_id", "content"],
        },
    },
    {
        "name": "create_google_sheet",
        "description": "Create a new Google Sheets spreadsheet with optional headers and data rows.",
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Spreadsheet title"},
                "rows": {
                    "type": "array",
                    "description": "2-D array of cell values, e.g. [['Name','Grade'],['Math','A']]",
                    "items": {"type": "array", "items": {}},
                },
                "folder_id": {"type": "string", "description": "Optional Drive folder ID"},
            },
            "required": ["title"],
        },
    },
    {
        "name": "update_google_sheet",
        "description": "Write rows of data to a range in an existing Google Sheet (overwrites that range).",
        "input_schema": {
            "type": "object",
            "properties": {
                "spreadsheet_id": {"type": "string", "description": "Google Sheets file ID"},
                "range": {"type": "string", "description": "A1-notation range, e.g. 'Sheet1!A1'"},
                "rows": {
                    "type": "array",
                    "description": "2-D array of values to write",
                    "items": {"type": "array", "items": {}},
                },
            },
            "required": ["spreadsheet_id", "range", "rows"],
        },
    },
    {
        "name": "create_drive_folder",
        "description": "Create a new folder in Google Drive. Returns the folder ID and URL.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Folder name"},
                "parent_folder_id": {"type": "string", "description": "Optional parent folder ID"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "delete_drive_file",
        "description": "Permanently delete a file or folder from Google Drive by its ID.",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_id": {"type": "string", "description": "Drive file or folder ID to delete"},
            },
            "required": ["file_id"],
        },
    },
    {
        "name": "move_drive_file",
        "description": "Move a Drive file to a different folder.",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_id": {"type": "string", "description": "File ID to move"},
                "new_folder_id": {"type": "string", "description": "Destination folder ID"},
            },
            "required": ["file_id", "new_folder_id"],
        },
    },
    {
        "name": "rename_drive_file",
        "description": "Rename a file or folder in Google Drive.",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_id": {"type": "string", "description": "File or folder ID to rename"},
                "new_name": {"type": "string", "description": "New name"},
            },
            "required": ["file_id", "new_name"],
        },
    },
    # ── Google Slides tools ───────────────────────────────────────────────────
    {
        "name": "read_google_slides",
        "description": "Read the text content of every slide in a Google Slides presentation.",
        "input_schema": {
            "type": "object",
            "properties": {
                "presentation_id": {"type": "string", "description": "Google Slides file ID"},
            },
            "required": ["presentation_id"],
        },
    },
    {
        "name": "create_google_slides",
        "description": "Create a new blank Google Slides presentation and return its ID and URL.",
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Presentation title"},
            },
            "required": ["title"],
        },
    },
    {
        "name": "add_google_slide",
        "description": (
            "Append a new slide to a Google Slides presentation using the TITLE_AND_BODY layout. "
            "Supply title text and body text; both are optional."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "presentation_id": {"type": "string", "description": "Google Slides file ID"},
                "title": {"type": "string", "description": "Slide title text"},
                "body": {"type": "string", "description": "Slide body / bullet text"},
            },
            "required": ["presentation_id"],
        },
    },
    {
        "name": "update_slide_text",
        "description": (
            "Replace the text of a specific element on a slide. "
            "Use read_google_slides to get the presentation structure and element object IDs first."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "presentation_id": {"type": "string", "description": "Google Slides file ID"},
                "object_id": {"type": "string", "description": "Shape / text-box object ID from read_google_slides"},
                "new_text": {"type": "string", "description": "Replacement text"},
            },
            "required": ["presentation_id", "object_id", "new_text"],
        },
    },
    # ── Google Forms tools ────────────────────────────────────────────────────
    {
        "name": "read_google_form",
        "description": "Read a Google Form's title, description, and all questions.",
        "input_schema": {
            "type": "object",
            "properties": {
                "form_id": {"type": "string", "description": "Google Form ID (from the URL)"},
            },
            "required": ["form_id"],
        },
    },
    {
        "name": "create_google_form",
        "description": (
            "Create a new Google Form with a title and an optional list of questions. "
            "Each question should have 'title' and 'type' (SHORT_ANSWER, PARAGRAPH, MULTIPLE_CHOICE, CHECKBOX, DROPDOWN, SCALE, DATE, TIME). "
            "MULTIPLE_CHOICE / CHECKBOX / DROPDOWN questions also need an 'options' list of strings."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Form title"},
                "description": {"type": "string", "description": "Optional form description"},
                "questions": {
                    "type": "array",
                    "description": "List of question objects",
                    "items": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string"},
                            "type": {"type": "string"},
                            "required": {"type": "boolean"},
                            "options": {"type": "array", "items": {"type": "string"}},
                        },
                        "required": ["title", "type"],
                    },
                },
            },
            "required": ["title"],
        },
    },
    {
        "name": "add_form_question",
        "description": "Add a single question to an existing Google Form.",
        "input_schema": {
            "type": "object",
            "properties": {
                "form_id": {"type": "string", "description": "Google Form ID"},
                "title": {"type": "string", "description": "Question text"},
                "type": {
                    "type": "string",
                    "description": "Question type: SHORT_ANSWER, PARAGRAPH, MULTIPLE_CHOICE, CHECKBOX, DROPDOWN, SCALE, DATE, or TIME",
                },
                "required": {"type": "boolean", "description": "Whether an answer is required"},
                "options": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Answer choices (for MULTIPLE_CHOICE, CHECKBOX, DROPDOWN)",
                },
            },
            "required": ["form_id", "title", "type"],
        },
    },
    {
        "name": "get_form_responses",
        "description": "Retrieve all submitted responses for a Google Form.",
        "input_schema": {
            "type": "object",
            "properties": {
                "form_id": {"type": "string", "description": "Google Form ID"},
            },
            "required": ["form_id"],
        },
    },
    {
        "name": "update_form_info",
        "description": "Update the title and/or description of an existing Google Form.",
        "input_schema": {
            "type": "object",
            "properties": {
                "form_id": {"type": "string", "description": "Google Form ID"},
                "title": {"type": "string", "description": "New form title (omit to keep existing)"},
                "description": {"type": "string", "description": "New form description (omit to keep existing)"},
            },
            "required": ["form_id"],
        },
    },
    {
        "name": "update_form_question",
        "description": (
            "Update an existing question in a Google Form. Use read_google_form first to get the item_id. "
            "You can change the title, required status, and options (for choice questions). "
            "To change question type, delete it and re-add it with add_form_question."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "form_id": {"type": "string", "description": "Google Form ID"},
                "item_id": {"type": "string", "description": "Item ID from read_google_form"},
                "title": {"type": "string", "description": "New question text (omit to keep existing)"},
                "required": {"type": "boolean", "description": "Whether the answer is required"},
                "options": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "New answer choices for MULTIPLE_CHOICE / CHECKBOX / DROPDOWN (omit to keep existing)",
                },
            },
            "required": ["form_id", "item_id"],
        },
    },
    {
        "name": "delete_form_question",
        "description": "Delete a question from an existing Google Form. Use read_google_form first to get the item_id.",
        "input_schema": {
            "type": "object",
            "properties": {
                "form_id": {"type": "string", "description": "Google Form ID"},
                "item_id": {"type": "string", "description": "Item ID from read_google_form"},
            },
            "required": ["form_id", "item_id"],
        },
    },
    {
        "name": "delete_slide",
        "description": "Delete a slide from a Google Slides presentation. Use read_google_slides first to get the slide_id.",
        "input_schema": {
            "type": "object",
            "properties": {
                "presentation_id": {"type": "string", "description": "Google Slides presentation ID"},
                "slide_id": {"type": "string", "description": "Slide object ID from read_google_slides"},
            },
            "required": ["presentation_id", "slide_id"],
        },
    },
    {
        "name": "get_weather",
        "description": (
            "Get current weather and forecast for Park City (or any lat/lon). "
            "Returns temperature (°F), precipitation, snowfall, wind speed, and a 7-day forecast. "
            "Use this whenever the student asks about weather, conditions for outdoor activities, "
            "or whether to bring a jacket."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "days": {"type": "integer", "description": "Forecast days 1-7 (default 1 = today only)"},
                "latitude": {"type": "number", "description": "Latitude (default: Park City 40.6461)"},
                "longitude": {"type": "number", "description": "Longitude (default: Park City -111.4980)"},
            },
            "required": [],
        },
    },
    {
        "name": "get_climate_history",
        "description": (
            "Look up historical climate data for Park City from NOAA. Good for questions like "
            "'how does this ski season compare to last year?' or 'what's the average snowfall in February?'. "
            "Requires NOAA_API_TOKEN."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "start_date": {"type": "string", "description": "Start date YYYY-MM-DD"},
                "end_date":   {"type": "string", "description": "End date YYYY-MM-DD"},
                "datatype": {
                    "type": "string",
                    "description": "NOAA data type: TMAX (max temp °F), TMIN, TOBS, PRCP (precipitation), SNOW (snowfall), SNWD (snow depth). Default SNOW.",
                },
            },
            "required": ["start_date", "end_date"],
        },
    },
    {
        "name": "get_news",
        "description": (
            "Search The Guardian for current news articles. Good for current events, "
            "debate prep, AP Government/History research, or staying informed. "
            "Requires GUARDIAN_API_KEY."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query":   {"type": "string", "description": "Search terms (e.g. 'climate change Utah')"},
                "section": {"type": "string", "description": "Filter by section: world, us-news, politics, science, technology, sport, education, environment"},
                "count":   {"type": "integer", "description": "Number of results 1-10 (default 5)"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_activity_suggestion",
        "description": (
            "Suggest something to do when the student is bored or has unexpected free time. "
            "Returns a random activity with type and cost info."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "type": {
                    "type": "string",
                    "description": "Filter by type: education, recreational, social, diy, charity, cooking, relaxation, music, busywork",
                },
                "participants": {"type": "integer", "description": "Number of people (1, 2, 3, etc.)"},
                "free_only":    {"type": "boolean", "description": "If true, only return free activities (price=0)"},
            },
            "required": [],
        },
    },
    # ── Push notifications ────────────────────────────────────────────────────
    {
        "name": "send_notification",
        "description": (
            "Send a push notification to the student's device via ntfy. "
            "Use proactively when you want to alert the student about something time-sensitive, "
            "remind them of an urgent task, or surface a critical insight they should act on now. "
            "Keep the message short and actionable — 1-2 sentences maximum. "
            "Priority levels: 'min' (background), 'low', 'default', 'high', 'urgent' (breaks DND). "
            "Only use 'urgent' for genuine emergencies. "
            "Tags are ntfy emoji names e.g. 'warning', 'books', 'rotating_light', 'calendar'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title":    {"type": "string", "description": "Short notification title (50 chars max)"},
                "message":  {"type": "string", "description": "Notification body — 1-2 actionable sentences"},
                "priority": {
                    "type": "string",
                    "enum": ["min", "low", "default", "high", "urgent"],
                    "description": "Notification priority",
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional ntfy emoji tag names e.g. ['warning', 'books']",
                },
            },
            "required": ["title", "message"],
        },
    },
    # ── Google Calendar write tools ───────────────────────────────────────────
    {
        "name": "create_calendar_event",
        "description": (
            "Create a new event in the student's Google Calendar. "
            "Use when the student asks to add something to their calendar, block time, "
            "or schedule a study session, appointment, or reminder. "
            "Returns the created event ID and a link to it."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title":       {"type": "string", "description": "Event title/summary"},
                "start":       {"type": "string", "description": "Start in ISO 8601, e.g. '2026-05-15T14:00:00' or '2026-05-15' for all-day"},
                "end":         {"type": "string", "description": "End in ISO 8601, e.g. '2026-05-15T15:00:00' or '2026-05-16' for all-day"},
                "description": {"type": "string", "description": "Event notes or description (optional)"},
                "location":    {"type": "string", "description": "Event location (optional)"},
                "all_day":     {"type": "boolean", "description": "True for an all-day event — pass YYYY-MM-DD dates for start/end"},
                "calendar_id": {"type": "string", "description": "Google Calendar ID; defaults to 'primary'"},
            },
            "required": ["title", "start", "end"],
        },
    },
    {
        "name": "update_calendar_event",
        "description": (
            "Update an existing Google Calendar event. "
            "Pass only the fields you want to change — omitted fields are preserved. "
            "Use the event_id from list_google_calendar_events or create_calendar_event."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "event_id":    {"type": "string", "description": "Google Calendar event ID to update"},
                "title":       {"type": "string", "description": "New title (omit to keep existing)"},
                "start":       {"type": "string", "description": "New start datetime"},
                "end":         {"type": "string", "description": "New end datetime"},
                "description": {"type": "string", "description": "New description"},
                "location":    {"type": "string", "description": "New location"},
                "calendar_id": {"type": "string", "description": "Calendar ID, defaults to 'primary'"},
            },
            "required": ["event_id"],
        },
    },
    {
        "name": "delete_calendar_event",
        "description": "Delete a Google Calendar event by its ID. Irreversible — confirm with the student first.",
        "input_schema": {
            "type": "object",
            "properties": {
                "event_id":    {"type": "string", "description": "Google Calendar event ID to delete"},
                "calendar_id": {"type": "string", "description": "Calendar ID, defaults to 'primary'"},
            },
            "required": ["event_id"],
        },
    },
    {
        "name": "list_google_calendar_events",
        "description": (
            "List upcoming events from the student's Google Calendar (primary or specified). "
            "Returns event IDs, titles, start/end times, and descriptions. "
            "Use this to find event IDs before calling update_calendar_event or delete_calendar_event."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "days_ahead":  {"type": "integer", "description": "Days ahead to look, default 7, max 60"},
                "calendar_id": {"type": "string", "description": "Calendar ID, defaults to 'primary'"},
                "max_results": {"type": "integer", "description": "Max events to return, default 20, max 50"},
                "query":       {"type": "string", "description": "Optional text search within event titles/descriptions"},
            },
            "required": [],
        },
    },
]


# Anthropic server-side web tools — Anthropic executes these; no app-side handler needed.
JARVIS_WEB_TOOLS = [
    {"type": "web_search_20250305", "name": "web_search", "max_uses": 5},
    {
        "type": "web_fetch_20250910",
        "name": "web_fetch",
        "max_uses": 3,
        "max_content_tokens": 6000,
    },
]


# Set of tool names handled server-side by Anthropic, not by _execute_jarvis_tool.
ANTHROPIC_SERVER_TOOL_NAMES = {"web_search", "web_fetch"}


def _execute_jarvis_tool(name, inputs, conversation_id=None):
    """Execute a Jarvis tool call and return a JSON-serializable result dict."""
    try:
        if name == "get_tasks":
            conn = get_db()
            cur = conn.cursor()
            cur.execute(
                "SELECT id, title, urgency, due_date, notes FROM tasks WHERE completed=FALSE "
                "ORDER BY urgency DESC, created_at ASC LIMIT 25"
            )
            tasks = []
            for r in cur.fetchall():
                t = dict(r)
                if t.get("due_date") and hasattr(t["due_date"], "isoformat"):
                    t["due_date"] = t["due_date"].isoformat()
                tasks.append(t)
            cur.close(); conn.close()
            return {"tasks": tasks, "count": len(tasks)}

        elif name == "create_task":
            title = str(inputs.get("title", "")).strip()[:300]
            if not title:
                return {"error": "title is required"}
            urgency = str(inputs.get("urgency", "low")).lower()
            if urgency not in ("high", "medium", "low"):
                urgency = "low"
            due_date = inputs.get("due_date") or None
            notes = str(inputs.get("notes", ""))[:2000]
            conn = get_db()
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO tasks (title, urgency, due_date, notes) VALUES (%s,%s,%s,%s) RETURNING id",
                (title, urgency, due_date, notes),
            )
            task_id = cur.fetchone()["id"]
            conn.commit(); cur.close(); conn.close()
            log.info(f"Jarvis tool: created task '{title}' id={task_id}")
            return {"status": "created", "task_id": task_id, "title": title, "urgency": urgency}

        elif name == "complete_task":
            task_id = int(inputs.get("task_id", 0))
            conn = get_db()
            try:
                cur = conn.cursor()
                try:
                    # Try regular tasks first
                    cur.execute(
                        "UPDATE tasks SET completed=TRUE, completed_at=NOW() WHERE id=%s AND completed=FALSE RETURNING title",
                        (task_id,),
                    )
                    row = cur.fetchone()
                    if not row:
                        # Fall back to project tasks (they use status='done' instead of a boolean)
                        cur.execute(
                            "UPDATE project_tasks SET status='done' WHERE id=%s AND status!='done' RETURNING title",
                            (task_id,),
                        )
                        row = cur.fetchone()
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
                finally:
                    cur.close()
            finally:
                conn.close()
            if row:
                log.info(f"Jarvis tool: completed task id={task_id} '{row['title']}'")
                return {"status": "completed", "task_id": task_id, "title": row["title"]}
            return {"status": "not_found", "task_id": task_id}

        elif name == "delete_task":
            task_id = int(inputs.get("task_id", 0))
            conn = get_db()
            cur = conn.cursor()
            cur.execute("DELETE FROM tasks WHERE id=%s RETURNING title", (task_id,))
            row = cur.fetchone()
            conn.commit(); cur.close(); conn.close()
            if row:
                log.info(f"Jarvis tool: deleted task id={task_id} '{row['title']}'")
                return {"status": "deleted", "task_id": task_id, "title": row["title"]}
            return {"status": "not_found", "task_id": task_id}

        elif name == "update_task":
            task_id = int(inputs.get("task_id", 0))
            urgency = inputs.get("urgency")
            due_date = inputs.get("due_date")
            notes = inputs.get("notes")
            updates, params = [], []
            if urgency in ("high", "medium", "low"):
                updates.append("urgency=%s"); params.append(urgency)
            if due_date is not None:
                updates.append("due_date=%s"); params.append(due_date or None)
            if notes is not None:
                updates.append("notes=%s"); params.append(str(notes)[:2000])
            if not updates:
                return {"status": "nothing_to_update"}
            params.append(task_id)
            conn = get_db()
            cur = conn.cursor()
            cur.execute(f"UPDATE tasks SET {', '.join(updates)} WHERE id=%s RETURNING title", params)
            row = cur.fetchone()
            conn.commit(); cur.close(); conn.close()
            return {"status": "updated", "task_id": task_id, "title": row["title"] if row else None}

        elif name == "get_assignments":
            if not u_canvas_ical():
                return {"assignments": [], "note": "Canvas calendar not configured"}
            try:
                cal = fetch_ical(u_canvas_ical())
                if not cal:
                    return {"assignments": [], "error": "Could not fetch Canvas calendar"}
                asgn_list = get_canvas_assignments_with_overdue(cal)
                conn = get_db()
                cur = conn.cursor()
                cur.execute("SELECT DISTINCT assignment_title FROM completions")
                done = set(r["assignment_title"] for r in cur.fetchall())
                cur.close(); conn.close()
                asgn_list = [a for a in asgn_list if a["title"] not in done]
                return {"assignments": asgn_list, "count": len(asgn_list)}
            except Exception as e:
                return {"assignments": [], "error": str(e)}

        elif name == "complete_assignment":
            title = str(inputs.get("assignment_title", ""))[:300]
            class_name = str(inputs.get("class_name", ""))[:100]
            duration = int(inputs.get("duration_minutes") or 0)
            submitted = bool(inputs.get("submitted", False))
            if not title:
                return {"error": "assignment_title required"}
            conn = get_db()
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO completions (assignment_title, class_name, duration_minutes, estimate_minutes, timed, submitted) VALUES (%s,%s,%s,0,FALSE,%s)",
                (title, class_name, duration, submitted),
            )
            # Also complete any task that was auto-created from this overdue assignment
            cur.execute(
                "UPDATE tasks SET completed=TRUE, completed_at=NOW() "
                "WHERE title=%s AND completed=FALSE AND notes LIKE 'Overdue Canvas assignment%%'",
                (title,),
            )
            # Reset the cache flag so it won't re-promote if somehow re-fetched
            cur.execute(
                "UPDATE canvas_assignments_cache SET promoted_to_task=FALSE WHERE title=%s",
                (title,),
            )
            conn.commit(); cur.close(); conn.close()
            log.info(f"Jarvis tool: logged completion of '{title}' (submitted={submitted})")
            status = "submitted" if submitted else "logged"
            return {"status": status, "assignment": title, "class": class_name}

        elif name == "get_calendar_events":
            days_ahead = min(30, max(1, int(inputs.get("days_ahead", 7))))
            events = []
            for url, tag in ((u_personal_ical(), "personal"), (u_sports_ical(), "sports"), (u_job_schedule_ical(), "job")):
                if not url:
                    continue
                try:
                    cal = fetch_ical(url)
                    if cal:
                        parsed = parse_calendar_events(cal, days_ahead=days_ahead)
                        for e in parsed:
                            e["source"] = tag
                        events.extend(parsed)
                except Exception:
                    pass
            events.sort(key=lambda e: e.get("start_display", ""))
            return {"events": events[:40], "count": len(events)}

        elif name == "log_stock_transaction":
            symbol = str(inputs.get("symbol", "")).strip().upper()[:16]
            action = str(inputs.get("action", "")).lower()
            try:
                qty = float(inputs.get("quantity", 0))
                price = float(inputs.get("price", 0))
            except (TypeError, ValueError):
                return {"error": "Invalid quantity or price"}
            if not symbol or action not in ("buy", "sell") or qty <= 0 or price <= 0:
                return {"error": "symbol, action (buy/sell), quantity>0, price>0 are all required"}
            tx_date = inputs.get("date") or datetime.now(TZ).date().isoformat()
            notes = str(inputs.get("notes", ""))[:500]
            conn = get_db()
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO stock_transactions (symbol, action, quantity, price, transaction_date, notes) VALUES (%s,%s,%s,%s,%s,%s) RETURNING id",
                (symbol, action, qty, price, tx_date, notes),
            )
            tx_id = cur.fetchone()["id"]
            conn.commit(); cur.close(); conn.close()
            log.info(f"Jarvis tool: logged stock {action} {qty} {symbol} @ {price}")
            return {"status": "recorded", "transaction_id": tx_id, "symbol": symbol, "action": action, "quantity": qty, "price": price}

        elif name == "save_stock_note":
            symbol = str(inputs.get("symbol", "")).strip().upper()[:16]
            if not symbol:
                return {"error": "symbol required"}
            thesis = inputs.get("thesis")
            exit_criteria = inputs.get("exit_criteria")
            try:
                target_price = float(inputs["target_price"]) if inputs.get("target_price") not in (None, "", "null") else None
            except (TypeError, ValueError):
                target_price = None
            try:
                stop_loss = float(inputs["stop_loss"]) if inputs.get("stop_loss") not in (None, "", "null") else None
            except (TypeError, ValueError):
                stop_loss = None
            upsert_stock_note(
                symbol,
                thesis=(str(thesis) if thesis not in (None, "") else None),
                exit_criteria=(str(exit_criteria) if exit_criteria not in (None, "") else None),
                target_price=target_price,
                stop_loss=stop_loss,
            )
            log.info(f"Jarvis tool: saved stock note for {symbol}")
            return {"status": "saved", "symbol": symbol}

        elif name == "get_portfolio":
            port = _compute_portfolio()
            holdings = [{"symbol": sym, "quantity": h["qty"], "avg_cost": round(h["avg_cost"], 2)} for sym, h in port.items()]
            return {"holdings": holdings, "count": len(holdings)}

        elif name == "get_projects":
            conn = get_db()
            cur = conn.cursor()
            cur.execute("SELECT id, title, description, status FROM projects WHERE status='active' ORDER BY created_at DESC")
            projects = [dict(r) for r in cur.fetchall()]
            cur.execute("""
SELECT p.title as project, pt.id as task_id, pt.title as task, pt.assignee, pt.status, pt.notes
FROM project_tasks pt JOIN projects p ON p.id=pt.project_id
WHERE p.status='active' AND pt.status!='done' ORDER BY pt.created_at ASC LIMIT 20""")
            proj_tasks = [dict(r) for r in cur.fetchall()]
            cur.execute("""
SELECT p.title as project, pn.content as note
FROM project_notes pn JOIN projects p ON p.id=pn.project_id
WHERE p.status='active' ORDER BY pn.created_at DESC LIMIT 10""")
            proj_notes = [dict(r) for r in cur.fetchall()]
            cur.close(); conn.close()
            return {"projects": projects, "tasks": proj_tasks, "notes": proj_notes}

        elif name == "create_project":
            title = str(inputs.get("title", "")).strip()[:300]
            if not title:
                return {"error": "title is required"}
            description = str(inputs.get("description", ""))[:2000]
            try:
                checkin = int(inputs.get("checkin_interval_days", 7))
            except (TypeError, ValueError):
                checkin = 7
            checkin = max(1, min(90, checkin))
            tasks_in = inputs.get("tasks") or []
            if not isinstance(tasks_in, list):
                tasks_in = []

            conn = get_db()
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO projects (title, description, status, lead, members, "
                "checkin_interval_days, last_checkin) "
                "VALUES (%s, %s, 'active', '', '', %s, NOW()) RETURNING id",
                (title, description, checkin),
            )
            project_id = cur.fetchone()["id"]

            seeded = []
            for t in tasks_in[:40]:
                if not isinstance(t, dict):
                    continue
                ttitle = str(t.get("title", "")).strip()[:300]
                if not ttitle:
                    continue
                cur.execute(
                    "INSERT INTO project_tasks (project_id, title, notes, assignee, status, due_date) "
                    "VALUES (%s, %s, %s, 'me', 'pending', %s) RETURNING id",
                    (project_id, ttitle, str(t.get("notes", ""))[:2000], t.get("due_date") or None),
                )
                seeded.append({"id": cur.fetchone()["id"], "title": ttitle})

            conn.commit(); cur.close(); conn.close()
            log.info(f"Jarvis tool: created project '{title}' id={project_id} with {len(seeded)} tasks")
            return {
                "status": "created",
                "project_id": project_id,
                "title": title,
                "task_count": len(seeded),
                "tasks": seeded,
            }

        elif name == "add_project_task":
            try:
                project_id = int(inputs.get("project_id", 0))
            except (TypeError, ValueError):
                return {"error": "project_id must be an integer"}
            title = str(inputs.get("title", "")).strip()[:300]
            if not title:
                return {"error": "title is required"}
            conn = get_db()
            cur = conn.cursor()
            cur.execute("SELECT id, title FROM projects WHERE id=%s", (project_id,))
            row = cur.fetchone()
            if not row:
                cur.close(); conn.close()
                return {"error": f"project_id {project_id} not found"}
            cur.execute(
                "INSERT INTO project_tasks (project_id, title, notes, assignee, status, due_date) "
                "VALUES (%s, %s, %s, 'me', 'pending', %s) RETURNING id",
                (project_id, title, str(inputs.get("notes", ""))[:2000], inputs.get("due_date") or None),
            )
            task_id = cur.fetchone()["id"]
            project_title = row["title"]
            conn.commit(); cur.close(); conn.close()
            log.info(f"Jarvis tool: added task '{title}' id={task_id} to project {project_id}")
            return {
                "status": "added",
                "project_id": project_id,
                "project_title": project_title,
                "project_task_id": task_id,
                "title": title,
            }

        elif name == "get_briefing":
            conn = get_db()
            cur = conn.cursor()
            today = datetime.now(TZ).date()
            cur.execute("SELECT content, generated_at FROM briefing_cache WHERE briefing_date=%s", (today,))
            row = cur.fetchone()
            cur.close(); conn.close()
            if row:
                ts = row["generated_at"].isoformat() if row["generated_at"] else None
                return {"briefing": row["content"], "generated_at": ts}
            return {"briefing": None, "note": "No briefing generated yet for today"}

        elif name == "get_grades":
            result = {}
            if _canvas_configured():
                result["canvas_grades"] = canvas_grades()
            if _ps_configured():
                result["powerschool_grades"] = ps_grades()
            if not result:
                return {"error": "Neither Canvas nor PowerSchool is configured."}
            return result

        elif name == "get_assignment_details":
            if not _canvas_configured():
                return {"error": "Canvas API not configured. Add your Canvas API token and base URL in Settings → Calendars."}
            title = str(inputs.get("title", "")).strip()
            if not title:
                return {"error": "title is required"}
            course_hint = str(inputs.get("course", "")).strip().lower()
            match = canvas_search_assignment(title)
            if not match:
                return {"error": f"No Canvas assignment found matching '{title}'."}
            cid, aid, course_name = match
            if course_hint and course_hint not in (course_name or "").lower():
                # Hint disagreed with the match — still return what we found, but flag it.
                detail = canvas_assignment_detail(cid, aid)
                if detail:
                    detail["course"] = course_name
                    detail["note"] = f"Course hint '{inputs.get('course')}' did not match; returning best title match in '{course_name}'."
                return detail or {"error": "Could not load assignment details."}
            detail = canvas_assignment_detail(cid, aid)
            if not detail:
                return {"error": "Could not load assignment details."}
            detail["course"] = course_name
            return detail

        elif name == "save_memory":
            memory_text = str(inputs.get("memory", "")).strip()[:1000]
            if not memory_text:
                return {"status": "skipped", "reason": "empty memory text"}
            if not MEM0_API_KEY:
                return {"status": "skipped", "reason": "MEM0_API_KEY not configured"}
            try:
                _get_mem0_client().add(
                    [{"role": "assistant", "content": memory_text}],
                    user_id="student",
                )
                return {"status": "saved", "memory": memory_text}
            except Exception as e:
                return {"status": "error", "message": str(e)}

        elif name == "remember_person":
            import json as _json
            person_name = str(inputs.get("name", "")).strip()[:200]
            if not person_name:
                return {"error": "name is required"}
            relationship = str(inputs.get("relationship", "")).strip()[:100]
            new_facts = [str(f).strip()[:500] for f in (inputs.get("facts") or []) if str(f).strip()]

            conn = get_db()
            cur = conn.cursor()
            # Fetch existing profile
            cur.execute("SELECT relationship, facts FROM people_profiles WHERE name = %s", (person_name,))
            row = cur.fetchone()

            # Skip entirely if profile already exists and there's nothing new to add
            if row and not new_facts and not relationship:
                cur.close()
                conn.close()
                return {"status": "skipped", "reason": "no new information to store"}

            if row:
                existing_relationship = row["relationship"] or relationship
                try:
                    existing_facts = _json.loads(row["facts"] or "[]")
                except Exception:
                    existing_facts = []
                # Merge facts (deduplicate)
                merged_facts = existing_facts + [f for f in new_facts if f not in existing_facts]
                final_relationship = relationship or existing_relationship
                cur.execute(
                    "UPDATE people_profiles SET relationship=%s, facts=%s, updated_at=NOW() WHERE name=%s",
                    (final_relationship, _json.dumps(merged_facts), person_name)
                )
                action = "updated"
            else:
                merged_facts = new_facts
                final_relationship = relationship
                cur.execute(
                    "INSERT INTO people_profiles (name, relationship, facts) VALUES (%s, %s, %s)",
                    (person_name, final_relationship, _json.dumps(merged_facts))
                )
                action = "created"
            conn.commit()
            cur.close()
            conn.close()

            # Also push to Mem0 so facts surface in semantic search
            if MEM0_API_KEY and new_facts:
                try:
                    _facts_text = f"About {person_name} ({final_relationship or 'person known to student'}): " + "; ".join(new_facts)
                    _get_mem0_client().add(
                        [{"role": "assistant", "content": _facts_text}],
                        user_id="student",
                        metadata={"person": person_name},
                    )
                except Exception as _e:
                    log.debug("Mem0 person sync error: %s", _e)

            return {
                "status": action,
                "name": person_name,
                "relationship": final_relationship,
                "total_facts": len(merged_facts),
                "facts_added": len(new_facts),
            }

        elif name == "get_person_profile":
            import json as _json
            person_name = str(inputs.get("name", "")).strip()
            if not person_name:
                return {"error": "name is required"}
            conn = get_db()
            cur = conn.cursor()
            cur.execute(
                "SELECT name, relationship, facts, created_at, updated_at FROM people_profiles WHERE LOWER(name) = LOWER(%s)",
                (person_name,)
            )
            row = cur.fetchone()
            cur.close()
            conn.close()
            if not row:
                return {"found": False, "name": person_name, "message": "No profile found for this person yet."}
            try:
                facts = _json.loads(row["facts"] or "[]")
            except Exception:
                facts = []
            # Also search Mem0 for additional memories mentioning this person
            mem0_mentions = []
            if MEM0_API_KEY:
                try:
                    _hits = _get_mem0_client().search(
                        person_name,
                        user_id="student",
                        filters={"AND": [{"metadata": {"person": person_name}}]},
                        limit=8,
                    )
                    mem0_mentions = [h["memory"] for h in (_hits or []) if h.get("memory")]
                except Exception:
                    try:
                        _hits = _get_mem0_client().search(f"about {person_name}", user_id="student", limit=8)
                        mem0_mentions = [h["memory"] for h in (_hits or []) if h.get("memory") and person_name.lower() in h["memory"].lower()]
                    except Exception:
                        pass
            return {
                "found": True,
                "name": row["name"],
                "relationship": row["relationship"],
                "facts": facts,
                "mem0_mentions": mem0_mentions,
                "profile_created": row["created_at"].isoformat() if row["created_at"] else None,
                "last_updated": row["updated_at"].isoformat() if row["updated_at"] else None,
            }

        elif name == "list_people":
            import json as _json
            conn = get_db()
            cur = conn.cursor()
            cur.execute("SELECT name, relationship, facts, updated_at FROM people_profiles ORDER BY updated_at DESC")
            rows = cur.fetchall()
            cur.close()
            conn.close()
            people = []
            for r in rows:
                try:
                    fact_count = len(_json.loads(r["facts"] or "[]"))
                except Exception:
                    fact_count = 0
                people.append({
                    "name": r["name"],
                    "relationship": r["relationship"],
                    "fact_count": fact_count,
                    "last_updated": r["updated_at"].isoformat() if r["updated_at"] else None,
                })
            return {"people": people, "count": len(people)}

        # ── Google Workspace tools ────────────────────────────────────────────
        elif name == "create_email_draft":
            creds = _get_google_credentials()
            if creds is None:
                if not _google_configured():
                    return {"error": "Google not configured. Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET."}
                return {"error": "Google not authorized. Visit /google-auth/start to connect your Google account."}
            to_addr = str(inputs.get("to", "")).strip()
            subject = str(inputs.get("subject", "")).strip()
            body = str(inputs.get("body", "")).strip()
            cc_addr = str(inputs.get("cc", "")).strip()
            if not to_addr or not subject:
                return {"error": "to and subject are required"}
            conn = get_db()
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO gmail_drafts (to_addr, cc_addr, subject, body, conversation_id) VALUES (%s,%s,%s,%s,%s) RETURNING id",
                (to_addr, cc_addr, subject, body, conversation_id or ""),
            )
            draft_id = cur.fetchone()["id"]
            conn.commit(); cur.close(); conn.close()
            log.info(f"Gmail draft {draft_id} created for {to_addr!r}")
            return {
                "status": "awaiting_confirmation",
                "draft_id": draft_id,
                "to": to_addr,
                "cc": cc_addr,
                "subject": subject,
                "body_preview": body[:8000],
            }

        elif name in (
            "search_google_drive", "read_google_drive_file", "list_google_drive_files",
            "read_google_sheet", "list_google_classroom_courses",
            "get_google_classroom_assignments", "search_gmail", "read_gmail_message",
            "create_google_doc", "update_google_doc", "append_google_doc",
            "create_google_sheet", "update_google_sheet",
            "create_drive_folder", "delete_drive_file", "move_drive_file", "rename_drive_file",
            "read_google_slides", "create_google_slides", "add_google_slide", "update_slide_text",
            "read_google_form", "create_google_form", "add_form_question", "get_form_responses",
            "update_form_info", "update_form_question", "delete_form_question",
            "delete_slide",
        ):
            creds = _get_google_credentials()
            if creds is None:
                if not _google_configured():
                    return {"error": "Google not configured. Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET environment variables."}
                return {"error": "Google account not connected or token expired. Please visit /google-auth/start to re-link your Google account."}

            from googleapiclient.discovery import build as _gbuild

            if name == "search_google_drive":
                query_raw = str(inputs.get("query", "")).strip()
                if not query_raw:
                    return {"error": "query is required"}
                max_results = min(int(inputs.get("max_results", 10)), 20)
                safe_q = query_raw.replace("\\", "\\\\").replace("'", "\\'")
                drive_query = f"(name contains '{safe_q}' or fullText contains '{safe_q}') and trashed=false"
                svc = _gbuild("drive", "v3", credentials=creds, cache_discovery=False)
                result = svc.files().list(
                    q=drive_query,
                    pageSize=max_results,
                    fields="files(id,name,mimeType,modifiedTime,webViewLink,parents)",
                ).execute()
                files = result.get("files", [])
                return {"files": files, "count": len(files)}

            elif name == "read_google_drive_file":
                file_id = str(inputs.get("file_id", "")).strip()
                if not file_id:
                    return {"error": "file_id is required"}
                svc = _gbuild("drive", "v3", credentials=creds, cache_discovery=False)
                meta = svc.files().get(fileId=file_id, fields="name,mimeType").execute()
                mime = meta.get("mimeType", "")
                name_hint = meta.get("name", inputs.get("file_name", file_id))
                google_export_map = {
                    "application/vnd.google-apps.document": "text/plain",
                    "application/vnd.google-apps.spreadsheet": "text/csv",
                    "application/vnd.google-apps.presentation": "text/plain",
                    "application/vnd.google-apps.drawing": "image/svg+xml",
                }
                if mime in google_export_map:
                    export_mime = google_export_map[mime]
                    if export_mime == "image/svg+xml":
                        return {"file_id": file_id, "name": name_hint, "mime_type": mime, "content": None, "note": "Drawing files cannot be exported as text."}
                    raw = svc.files().export(fileId=file_id, mimeType=export_mime).execute()
                    text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
                elif mime.startswith("text/"):
                    import io
                    from googleapiclient.http import MediaIoBaseDownload
                    buf = io.BytesIO()
                    dl = MediaIoBaseDownload(buf, svc.files().get_media(fileId=file_id))
                    done = False
                    while not done:
                        _, done = dl.next_chunk()
                    text = buf.getvalue().decode("utf-8", errors="replace")
                else:
                    return {"file_id": file_id, "name": name_hint, "mime_type": mime, "content": None, "note": "Binary file — cannot extract text content."}
                return {"file_id": file_id, "name": name_hint, "mime_type": mime, "content": text[:60000]}

            elif name == "list_google_drive_files":
                folder_id = str(inputs.get("folder_id", "")).strip()
                max_results = min(int(inputs.get("max_results", 20)), 50)
                svc = _gbuild("drive", "v3", credentials=creds, cache_discovery=False)
                if folder_id:
                    q = f"'{folder_id}' in parents and trashed=false"
                else:
                    q = "trashed=false"
                result = svc.files().list(
                    q=q,
                    pageSize=max_results,
                    orderBy="modifiedTime desc",
                    fields="files(id,name,mimeType,modifiedTime,webViewLink)",
                ).execute()
                files = result.get("files", [])
                return {"files": files, "count": len(files)}

            elif name == "read_google_sheet":
                spreadsheet_id = str(inputs.get("spreadsheet_id", "")).strip()
                if not spreadsheet_id:
                    return {"error": "spreadsheet_id is required"}
                range_name = str(inputs.get("range", "")).strip()
                svc = _gbuild("sheets", "v4", credentials=creds, cache_discovery=False)
                if not range_name:
                    meta = svc.spreadsheets().get(
                        spreadsheetId=spreadsheet_id,
                        fields="sheets(properties(title))",
                    ).execute()
                    sheets = meta.get("sheets", [])
                    range_name = sheets[0]["properties"]["title"] if sheets else "Sheet1"
                result = svc.spreadsheets().values().get(
                    spreadsheetId=spreadsheet_id, range=range_name
                ).execute()
                rows = result.get("values", [])
                return {"spreadsheet_id": spreadsheet_id, "range": range_name, "rows": rows, "row_count": len(rows)}

            elif name == "list_google_classroom_courses":
                svc = _gbuild("classroom", "v1", credentials=creds, cache_discovery=False)
                result = svc.courses().list(studentId="me", courseStates=["ACTIVE"]).execute()
                courses = result.get("courses", [])
                return {
                    "courses": [
                        {"id": c["id"], "name": c.get("name", ""), "section": c.get("section", ""), "room": c.get("room", "")}
                        for c in courses
                    ],
                    "count": len(courses),
                }

            elif name == "get_google_classroom_assignments":
                course_id = str(inputs.get("course_id", "")).strip()
                if not course_id:
                    return {"error": "course_id is required"}
                svc = _gbuild("classroom", "v1", credentials=creds, cache_discovery=False)
                work = svc.courses().courseWork().list(
                    courseId=course_id, orderBy="updateTime desc", pageSize=20
                ).execute()
                items = []
                for cw in work.get("courseWork", []):
                    item = {
                        "id": cw["id"],
                        "title": cw.get("title", ""),
                        "description": (cw.get("description") or "")[:500],
                        "state": cw.get("state", ""),
                        "due_date": None,
                    }
                    if "dueDate" in cw:
                        dd = cw["dueDate"]
                        item["due_date"] = f"{dd.get('year',0):04d}-{dd.get('month',0):02d}-{dd.get('day',0):02d}"
                    items.append(item)
                return {"course_id": course_id, "assignments": items, "count": len(items)}

            elif name == "search_gmail":
                gmail_query = str(inputs.get("query", "")).strip()
                if not gmail_query:
                    return {"error": "query is required"}
                max_results = min(int(inputs.get("max_results", 10)), 25)
                svc = _gbuild("gmail", "v1", credentials=creds, cache_discovery=False)
                result = svc.users().messages().list(
                    userId="me", q=gmail_query, maxResults=max_results
                ).execute()
                messages = result.get("messages", [])
                details = []
                for msg in messages[:max_results]:
                    md = svc.users().messages().get(
                        userId="me", id=msg["id"], format="metadata",
                        metadataHeaders=["Subject", "From", "Date"],
                    ).execute()
                    hdrs = {h["name"]: h["value"] for h in md.get("payload", {}).get("headers", [])}
                    details.append({
                        "id": msg["id"],
                        "subject": hdrs.get("Subject", ""),
                        "from": hdrs.get("From", ""),
                        "date": hdrs.get("Date", ""),
                        "snippet": md.get("snippet", ""),
                    })
                return {"messages": details, "count": len(details)}

            elif name == "create_google_doc":
                title = str(inputs.get("title", "Untitled")).strip()
                content = str(inputs.get("content", "")).strip()
                folder_id = str(inputs.get("folder_id", "")).strip()
                docs_svc = _gbuild("docs", "v1", credentials=creds, cache_discovery=False)
                drive_svc = _gbuild("drive", "v3", credentials=creds, cache_discovery=False)
                doc = docs_svc.documents().create(body={"title": title}).execute()
                doc_id = doc["documentId"]
                if content:
                    docs_svc.documents().batchUpdate(
                        documentId=doc_id,
                        body={"requests": [{"insertText": {"location": {"index": 1}, "text": content}}]},
                    ).execute()
                if folder_id:
                    meta = drive_svc.files().get(fileId=doc_id, fields="parents").execute()
                    prev_parents = ",".join(meta.get("parents", []))
                    drive_svc.files().update(
                        fileId=doc_id, addParents=folder_id, removeParents=prev_parents, fields="id,parents"
                    ).execute()
                link = f"https://docs.google.com/document/d/{doc_id}/edit"
                log.info(f"Created Google Doc '{title}' id={doc_id}")
                return {"document_id": doc_id, "title": title, "url": link}

            elif name == "update_google_doc":
                doc_id = str(inputs.get("document_id", "")).strip()
                content = str(inputs.get("content", ""))
                if not doc_id:
                    return {"error": "document_id is required"}
                docs_svc = _gbuild("docs", "v1", credentials=creds, cache_discovery=False)
                doc = docs_svc.documents().get(documentId=doc_id, fields="body").execute()
                end_idx = doc["body"]["content"][-1]["endIndex"] - 1
                requests = []
                if end_idx > 1:
                    requests.append({"deleteContentRange": {"range": {"startIndex": 1, "endIndex": end_idx}}})
                if content:
                    requests.append({"insertText": {"location": {"index": 1}, "text": content}})
                if requests:
                    docs_svc.documents().batchUpdate(documentId=doc_id, body={"requests": requests}).execute()
                return {"document_id": doc_id, "status": "updated", "url": f"https://docs.google.com/document/d/{doc_id}/edit"}

            elif name == "append_google_doc":
                doc_id = str(inputs.get("document_id", "")).strip()
                content = str(inputs.get("content", ""))
                if not doc_id:
                    return {"error": "document_id is required"}
                if not content:
                    return {"error": "content is required"}
                docs_svc = _gbuild("docs", "v1", credentials=creds, cache_discovery=False)
                doc = docs_svc.documents().get(documentId=doc_id, fields="body").execute()
                end_idx = doc["body"]["content"][-1]["endIndex"] - 1
                docs_svc.documents().batchUpdate(
                    documentId=doc_id,
                    body={"requests": [{"insertText": {"location": {"index": end_idx}, "text": content}}]},
                ).execute()
                return {"document_id": doc_id, "status": "appended", "url": f"https://docs.google.com/document/d/{doc_id}/edit"}

            elif name == "create_google_sheet":
                title = str(inputs.get("title", "Untitled")).strip()
                rows = inputs.get("rows") or []
                folder_id = str(inputs.get("folder_id", "")).strip()
                sheets_svc = _gbuild("sheets", "v4", credentials=creds, cache_discovery=False)
                drive_svc = _gbuild("drive", "v3", credentials=creds, cache_discovery=False)
                ss = sheets_svc.spreadsheets().create(body={"properties": {"title": title}}).execute()
                ss_id = ss["spreadsheetId"]
                if rows:
                    sheets_svc.spreadsheets().values().update(
                        spreadsheetId=ss_id,
                        range="Sheet1!A1",
                        valueInputOption="USER_ENTERED",
                        body={"values": rows},
                    ).execute()
                if folder_id:
                    meta = drive_svc.files().get(fileId=ss_id, fields="parents").execute()
                    prev_parents = ",".join(meta.get("parents", []))
                    drive_svc.files().update(
                        fileId=ss_id, addParents=folder_id, removeParents=prev_parents, fields="id,parents"
                    ).execute()
                link = f"https://docs.google.com/spreadsheets/d/{ss_id}/edit"
                log.info(f"Created Google Sheet '{title}' id={ss_id}")
                return {"spreadsheet_id": ss_id, "title": title, "url": link}

            elif name == "update_google_sheet":
                ss_id = str(inputs.get("spreadsheet_id", "")).strip()
                range_name = str(inputs.get("range", "Sheet1!A1")).strip()
                rows = inputs.get("rows") or []
                if not ss_id:
                    return {"error": "spreadsheet_id is required"}
                if not rows:
                    return {"error": "rows is required"}
                sheets_svc = _gbuild("sheets", "v4", credentials=creds, cache_discovery=False)
                sheets_svc.spreadsheets().values().update(
                    spreadsheetId=ss_id,
                    range=range_name,
                    valueInputOption="USER_ENTERED",
                    body={"values": rows},
                ).execute()
                return {"spreadsheet_id": ss_id, "range": range_name, "status": "updated", "url": f"https://docs.google.com/spreadsheets/d/{ss_id}/edit"}

            elif name == "create_drive_folder":
                folder_name = str(inputs.get("name", "New Folder")).strip()
                parent_id = str(inputs.get("parent_folder_id", "")).strip()
                svc = _gbuild("drive", "v3", credentials=creds, cache_discovery=False)
                meta = {"name": folder_name, "mimeType": "application/vnd.google-apps.folder"}
                if parent_id:
                    meta["parents"] = [parent_id]
                folder = svc.files().create(body=meta, fields="id,name,webViewLink").execute()
                log.info(f"Created Drive folder '{folder_name}' id={folder['id']}")
                return {"folder_id": folder["id"], "name": folder["name"], "url": folder.get("webViewLink", "")}

            elif name == "delete_drive_file":
                file_id = str(inputs.get("file_id", "")).strip()
                if not file_id:
                    return {"error": "file_id is required"}
                svc = _gbuild("drive", "v3", credentials=creds, cache_discovery=False)
                meta = svc.files().get(fileId=file_id, fields="name").execute()
                svc.files().delete(fileId=file_id).execute()
                log.info(f"Deleted Drive file id={file_id} name={meta.get('name')!r}")
                return {"status": "deleted", "file_id": file_id, "name": meta.get("name", "")}

            elif name == "move_drive_file":
                file_id = str(inputs.get("file_id", "")).strip()
                new_folder_id = str(inputs.get("new_folder_id", "")).strip()
                if not file_id or not new_folder_id:
                    return {"error": "file_id and new_folder_id are required"}
                svc = _gbuild("drive", "v3", credentials=creds, cache_discovery=False)
                meta = svc.files().get(fileId=file_id, fields="name,parents").execute()
                prev_parents = ",".join(meta.get("parents", []))
                svc.files().update(
                    fileId=file_id, addParents=new_folder_id,
                    removeParents=prev_parents, fields="id,parents",
                ).execute()
                return {"status": "moved", "file_id": file_id, "name": meta.get("name", ""), "new_folder_id": new_folder_id}

            elif name == "rename_drive_file":
                file_id = str(inputs.get("file_id", "")).strip()
                new_name = str(inputs.get("new_name", "")).strip()
                if not file_id or not new_name:
                    return {"error": "file_id and new_name are required"}
                svc = _gbuild("drive", "v3", credentials=creds, cache_discovery=False)
                svc.files().update(fileId=file_id, body={"name": new_name}, fields="id,name").execute()
                return {"status": "renamed", "file_id": file_id, "new_name": new_name}

            # ── Google Slides ─────────────────────────────────────────────────
            elif name == "read_google_slides":
                pres_id = str(inputs.get("presentation_id", "")).strip()
                if not pres_id:
                    return {"error": "presentation_id is required"}
                svc = _gbuild("slides", "v1", credentials=creds, cache_discovery=False)
                pres = svc.presentations().get(presentationId=pres_id).execute()
                slides_out = []
                for i, slide in enumerate(pres.get("slides", []), 1):
                    elements = []
                    for elem in slide.get("pageElements", []):
                        shape = elem.get("shape", {})
                        tb = shape.get("text", {})
                        full_text = "".join(tr.get("content", "") for tr in tb.get("textRuns", []))
                        if full_text.strip():
                            ph_type = shape.get("placeholder", {}).get("type", "")
                            elements.append({
                                "object_id": elem.get("objectId", ""),
                                "placeholder_type": ph_type,
                                "text": full_text.strip(),
                            })
                    slides_out.append({
                        "slide_number": i,
                        "slide_id": slide.get("objectId", ""),
                        "elements": elements,
                    })
                return {
                    "presentation_id": pres_id,
                    "title": pres.get("title", ""),
                    "slide_count": len(slides_out),
                    "slides": slides_out,
                    "url": f"https://docs.google.com/presentation/d/{pres_id}/edit",
                }

            elif name == "create_google_slides":
                title = str(inputs.get("title", "Untitled Presentation")).strip()
                svc = _gbuild("slides", "v1", credentials=creds, cache_discovery=False)
                pres = svc.presentations().create(body={"title": title}).execute()
                pres_id = pres["presentationId"]
                log.info(f"Created Google Slides '{title}' id={pres_id}")
                return {
                    "presentation_id": pres_id,
                    "title": title,
                    "url": f"https://docs.google.com/presentation/d/{pres_id}/edit",
                }

            elif name == "add_google_slide":
                pres_id = str(inputs.get("presentation_id", "")).strip()
                slide_title = str(inputs.get("title", "")).strip()
                slide_body = str(inputs.get("body", "")).strip()
                if not pres_id:
                    return {"error": "presentation_id is required"}
                import uuid as _uuid
                svc = _gbuild("slides", "v1", credentials=creds, cache_discovery=False)
                slide_id = "slide_" + _uuid.uuid4().hex[:12]
                title_id = "title_" + _uuid.uuid4().hex[:12]
                body_id = "body_" + _uuid.uuid4().hex[:12]
                requests = [
                    {
                        "addSlide": {
                            "objectId": slide_id,
                            "slideLayoutReference": {"predefinedLayout": "TITLE_AND_BODY"},
                            "placeholderIdMappings": [
                                {"layoutPlaceholder": {"type": "TITLE", "index": 0}, "objectId": title_id},
                                {"layoutPlaceholder": {"type": "BODY", "index": 0}, "objectId": body_id},
                            ],
                        }
                    }
                ]
                if slide_title:
                    requests.append({"insertText": {"objectId": title_id, "insertionIndex": 0, "text": slide_title}})
                if slide_body:
                    requests.append({"insertText": {"objectId": body_id, "insertionIndex": 0, "text": slide_body}})
                svc.presentations().batchUpdate(
                    presentationId=pres_id, body={"requests": requests}
                ).execute()
                return {
                    "status": "slide_added",
                    "presentation_id": pres_id,
                    "slide_id": slide_id,
                    "url": f"https://docs.google.com/presentation/d/{pres_id}/edit",
                }

            elif name == "update_slide_text":
                pres_id = str(inputs.get("presentation_id", "")).strip()
                object_id = str(inputs.get("object_id", "")).strip()
                new_text = str(inputs.get("new_text", ""))
                if not pres_id or not object_id:
                    return {"error": "presentation_id and object_id are required"}
                svc = _gbuild("slides", "v1", credentials=creds, cache_discovery=False)
                svc.presentations().batchUpdate(
                    presentationId=pres_id,
                    body={"requests": [
                        {"deleteText": {"objectId": object_id, "textRange": {"type": "ALL"}}},
                        {"insertText": {"objectId": object_id, "insertionIndex": 0, "text": new_text}},
                    ]},
                ).execute()
                return {"status": "updated", "presentation_id": pres_id, "object_id": object_id}

            # ── Google Forms ──────────────────────────────────────────────────
            elif name == "read_google_form":
                form_id = str(inputs.get("form_id", "")).strip()
                if not form_id:
                    return {"error": "form_id is required"}
                svc = _gbuild("forms", "v1", credentials=creds, cache_discovery=False)
                form = svc.forms().get(formId=form_id).execute()
                questions = []
                for item in form.get("items", []):
                    q = {"item_id": item.get("itemId", ""), "title": item.get("title", "")}
                    qitem = item.get("questionItem", {})
                    question = qitem.get("question", {})
                    if "choiceQuestion" in question:
                        q["type"] = question["choiceQuestion"].get("type", "")
                        q["options"] = [o.get("value", "") for o in question["choiceQuestion"].get("options", [])]
                    elif "textQuestion" in question:
                        q["type"] = "PARAGRAPH" if question["textQuestion"].get("paragraph") else "SHORT_ANSWER"
                    elif "scaleQuestion" in question:
                        sq = question["scaleQuestion"]
                        q["type"] = "SCALE"
                        q["low"] = sq.get("low", 1)
                        q["high"] = sq.get("high", 5)
                    elif "dateQuestion" in question:
                        q["type"] = "DATE"
                    elif "timeQuestion" in question:
                        q["type"] = "TIME"
                    else:
                        q["type"] = "UNKNOWN"
                    q["required"] = question.get("required", False)
                    questions.append(q)
                return {
                    "form_id": form_id,
                    "title": form.get("info", {}).get("title", ""),
                    "description": form.get("info", {}).get("description", ""),
                    "question_count": len(questions),
                    "questions": questions,
                    "url": form.get("responderUri", f"https://forms.gle/{form_id}"),
                }

            elif name in ("create_google_form", "add_form_question"):
                def _build_question_item(q_def):
                    qtype = str(q_def.get("type", "SHORT_ANSWER")).upper()
                    required = bool(q_def.get("required", False))
                    if qtype in ("MULTIPLE_CHOICE", "CHECKBOX", "DROPDOWN"):
                        return {
                            "title": q_def.get("title", ""),
                            "questionItem": {
                                "question": {
                                    "required": required,
                                    "choiceQuestion": {
                                        "type": qtype,
                                        "options": [{"value": o} for o in (q_def.get("options") or [])],
                                    },
                                }
                            },
                        }
                    elif qtype == "PARAGRAPH":
                        return {
                            "title": q_def.get("title", ""),
                            "questionItem": {"question": {"required": required, "textQuestion": {"paragraph": True}}},
                        }
                    elif qtype == "SCALE":
                        return {
                            "title": q_def.get("title", ""),
                            "questionItem": {
                                "question": {"required": required, "scaleQuestion": {"low": 1, "high": 5}}
                            },
                        }
                    elif qtype == "DATE":
                        return {
                            "title": q_def.get("title", ""),
                            "questionItem": {"question": {"required": required, "dateQuestion": {}}},
                        }
                    elif qtype == "TIME":
                        return {
                            "title": q_def.get("title", ""),
                            "questionItem": {"question": {"required": required, "timeQuestion": {}}},
                        }
                    else:
                        return {
                            "title": q_def.get("title", ""),
                            "questionItem": {"question": {"required": required, "textQuestion": {"paragraph": False}}},
                        }

                svc = _gbuild("forms", "v1", credentials=creds, cache_discovery=False)
                if name == "create_google_form":
                    title = str(inputs.get("title", "Untitled Form")).strip()
                    description = str(inputs.get("description", "")).strip()
                    form = svc.forms().create(body={"info": {"title": title}}).execute()
                    form_id = form["formId"]
                    update_requests = []
                    if description:
                        update_requests.append({
                            "updateFormInfo": {
                                "info": {"description": description},
                                "updateMask": "description",
                            }
                        })
                    for i, q_def in enumerate(inputs.get("questions") or []):
                        update_requests.append({
                            "createItem": {"item": _build_question_item(q_def), "location": {"index": i}}
                        })
                    if update_requests:
                        svc.forms().batchUpdate(
                            formId=form_id, body={"requests": update_requests}
                        ).execute()
                    log.info(f"Created Google Form '{title}' id={form_id}")
                    return {
                        "form_id": form_id,
                        "title": title,
                        "url": form.get("responderUri", ""),
                        "edit_url": f"https://docs.google.com/forms/d/{form_id}/edit",
                    }
                else:  # add_form_question
                    form_id = str(inputs.get("form_id", "")).strip()
                    if not form_id:
                        return {"error": "form_id is required"}
                    existing = svc.forms().get(formId=form_id).execute()
                    idx = len(existing.get("items", []))
                    item = _build_question_item(inputs)
                    svc.forms().batchUpdate(
                        formId=form_id,
                        body={"requests": [{"createItem": {"item": item, "location": {"index": idx}}}]},
                    ).execute()
                    return {"status": "question_added", "form_id": form_id, "question": inputs.get("title", "")}

            elif name == "get_form_responses":
                form_id = str(inputs.get("form_id", "")).strip()
                if not form_id:
                    return {"error": "form_id is required"}
                svc = _gbuild("forms", "v1", credentials=creds, cache_discovery=False)
                resp = svc.forms().responses().list(formId=form_id).execute()
                raw_responses = resp.get("responses", [])
                out = []
                for r in raw_responses[:50]:
                    answers = {}
                    for qid, ans in (r.get("answers") or {}).items():
                        text_answers = [v.get("value", "") for v in ans.get("textAnswers", {}).get("answers", [])]
                        answers[qid] = text_answers
                    out.append({
                        "response_id": r.get("responseId", ""),
                        "submitted_at": r.get("lastSubmittedTime", ""),
                        "answers": answers,
                    })
                return {"form_id": form_id, "response_count": len(out), "responses": out}

            elif name == "update_form_info":
                form_id = str(inputs.get("form_id", "")).strip()
                if not form_id:
                    return {"error": "form_id is required"}
                new_title = inputs.get("title")
                new_desc = inputs.get("description")
                if new_title is None and new_desc is None:
                    return {"error": "Provide at least one of title or description to update"}
                svc = _gbuild("forms", "v1", credentials=creds, cache_discovery=False)
                info_update = {}
                mask_fields = []
                if new_title is not None:
                    info_update["title"] = str(new_title).strip()
                    mask_fields.append("title")
                if new_desc is not None:
                    info_update["description"] = str(new_desc).strip()
                    mask_fields.append("description")
                svc.forms().batchUpdate(
                    formId=form_id,
                    body={"requests": [{"updateFormInfo": {"info": info_update, "updateMask": ",".join(mask_fields)}}]},
                ).execute()
                return {"status": "updated", "form_id": form_id, "updated_fields": mask_fields}

            elif name == "update_form_question":
                form_id = str(inputs.get("form_id", "")).strip()
                item_id = str(inputs.get("item_id", "")).strip()
                if not form_id or not item_id:
                    return {"error": "form_id and item_id are required"}
                svc = _gbuild("forms", "v1", credentials=creds, cache_discovery=False)
                existing_form = svc.forms().get(formId=form_id).execute()
                all_items = existing_form.get("items", [])
                item_index = next((i for i, it in enumerate(all_items) if it.get("itemId") == item_id), None)
                if item_index is None:
                    return {"error": f"Item {item_id} not found in form {form_id}"}
                existing_item = all_items[item_index]
                updated_item = {k: v for k, v in existing_item.items()}
                mask_fields = []
                if "title" in inputs:
                    updated_item["title"] = str(inputs["title"])
                    mask_fields.append("title")
                existing_q = (updated_item.get("questionItem") or {}).get("question") or {}
                if "required" in inputs:
                    existing_q["required"] = bool(inputs["required"])
                    mask_fields.append("questionItem.question.required")
                if "options" in inputs:
                    choice_type = (existing_q.get("choiceQuestion") or {}).get("type", "MULTIPLE_CHOICE")
                    existing_q["choiceQuestion"] = {
                        "type": choice_type,
                        "options": [{"value": o} for o in inputs["options"]],
                    }
                    mask_fields.append("questionItem.question.choiceQuestion")
                if "questionItem" not in updated_item:
                    updated_item["questionItem"] = {}
                updated_item["questionItem"]["question"] = existing_q
                svc.forms().batchUpdate(
                    formId=form_id,
                    body={"requests": [{"updateItem": {"item": updated_item, "location": {"index": item_index}, "updateMask": ",".join(mask_fields)}}]},
                ).execute()
                return {"status": "question_updated", "form_id": form_id, "item_id": item_id}

            elif name == "delete_form_question":
                form_id = str(inputs.get("form_id", "")).strip()
                item_id = str(inputs.get("item_id", "")).strip()
                if not form_id or not item_id:
                    return {"error": "form_id and item_id are required"}
                svc = _gbuild("forms", "v1", credentials=creds, cache_discovery=False)
                svc.forms().batchUpdate(
                    formId=form_id,
                    body={"requests": [{"deleteItem": {"location": {"itemId": item_id}}}]},
                ).execute()
                return {"status": "question_deleted", "form_id": form_id, "item_id": item_id}

            elif name == "delete_slide":
                pres_id = str(inputs.get("presentation_id", "")).strip()
                slide_id = str(inputs.get("slide_id", "")).strip()
                if not pres_id or not slide_id:
                    return {"error": "presentation_id and slide_id are required"}
                svc = _gbuild("slides", "v1", credentials=creds, cache_discovery=False)
                svc.presentations().batchUpdate(
                    presentationId=pres_id,
                    body={"requests": [{"deleteObject": {"objectId": slide_id}}]},
                ).execute()
                return {"status": "slide_deleted", "presentation_id": pres_id, "slide_id": slide_id}

            elif name == "read_gmail_message":
                import base64 as _b64

                def _extract_gmail_body(payload):
                    mime = payload.get("mimeType", "")
                    data = payload.get("body", {}).get("data", "")
                    if data and mime in ("text/plain", "text/html"):
                        try:
                            return _b64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
                        except Exception:
                            return ""
                    for part in payload.get("parts", []):
                        body = _extract_gmail_body(part)
                        if body:
                            return body
                    return ""

                msg_id = str(inputs.get("message_id", "")).strip()
                if not msg_id:
                    return {"error": "message_id is required"}
                svc = _gbuild("gmail", "v1", credentials=creds, cache_discovery=False)
                msg = svc.users().messages().get(userId="me", id=msg_id, format="full").execute()
                hdrs = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
                body = _extract_gmail_body(msg.get("payload", {}))
                return {
                    "id": msg_id,
                    "subject": hdrs.get("Subject", ""),
                    "from": hdrs.get("From", ""),
                    "to": hdrs.get("To", ""),
                    "date": hdrs.get("Date", ""),
                    "body": body[:12000],
                }

        elif name == "get_weather":
            lat = float(inputs.get("latitude", 40.6461))
            lon = float(inputs.get("longitude", -111.4980))
            days = min(int(inputs.get("days", 1)), 7)
            params = {
                "latitude": lat, "longitude": lon,
                "current": "temperature_2m,apparent_temperature,precipitation,snowfall,snow_depth,wind_speed_10m,weather_code",
                "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum,snowfall_sum,weather_code",
                "temperature_unit": "fahrenheit",
                "wind_speed_unit": "mph",
                "precipitation_unit": "inch",
                "forecast_days": days,
                "timezone": "America/Denver",
            }
            resp = requests.get("https://api.open-meteo.com/v1/forecast", params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            current = data.get("current", {})
            daily = data.get("daily", {})
            forecast = []
            for i, d in enumerate(daily.get("time", [])):
                forecast.append({
                    "date": d,
                    "high_f": daily["temperature_2m_max"][i],
                    "low_f": daily["temperature_2m_min"][i],
                    "precip_in": daily["precipitation_sum"][i],
                    "snow_in": daily["snowfall_sum"][i],
                })
            return {
                "current": {
                    "temp_f": current.get("temperature_2m"),
                    "feels_like_f": current.get("apparent_temperature"),
                    "precip_in": current.get("precipitation"),
                    "snowfall_in": current.get("snowfall"),
                    "snow_depth_in": current.get("snow_depth"),
                    "wind_mph": current.get("wind_speed_10m"),
                },
                "forecast": forecast,
                "location": f"{lat},{lon}",
            }

        elif name == "get_climate_history":
            if not NOAA_API_TOKEN:
                return {"error": "NOAA_API_TOKEN not configured"}
            datatype = str(inputs.get("datatype", "SNOW")).upper()
            params = {
                "datasetid": "GHCND",
                "stationid": "GHCND:USS0011J57S",
                "datatypeid": datatype,
                "startdate": inputs["start_date"],
                "enddate": inputs["end_date"],
                "units": "standard",
                "limit": 1000,
            }
            resp = requests.get(
                "https://www.ncdc.noaa.gov/cdo-web/api/v2/data",
                headers={"token": NOAA_API_TOKEN},
                params=params, timeout=15,
            )
            resp.raise_for_status()
            results = resp.json().get("results", [])
            return {
                "datatype": datatype,
                "start_date": inputs["start_date"],
                "end_date": inputs["end_date"],
                "record_count": len(results),
                "data": [{"date": r["date"][:10], "value": r["value"]} for r in results[:100]],
            }

        elif name == "get_news":
            if not GUARDIAN_API_KEY:
                return {"error": "GUARDIAN_API_KEY not configured"}
            params = {
                "q": str(inputs.get("query", "")),
                "api-key": GUARDIAN_API_KEY,
                "page-size": min(int(inputs.get("count", 5)), 10),
                "show-fields": "trailText,byline",
                "order-by": "newest",
            }
            section = inputs.get("section")
            if section:
                params["section"] = section
            resp = requests.get("https://content.guardianapis.com/search", params=params, timeout=10)
            resp.raise_for_status()
            results = resp.json().get("response", {}).get("results", [])
            articles = []
            for r in results:
                fields = r.get("fields") or {}
                articles.append({
                    "headline": r.get("webTitle", ""),
                    "section": r.get("sectionName", ""),
                    "date": r.get("webPublicationDate", "")[:10],
                    "summary": fields.get("trailText", ""),
                    "url": r.get("webUrl", ""),
                })
            return {"query": inputs.get("query"), "count": len(articles), "articles": articles}

        elif name == "get_activity_suggestion":
            params = {}
            if inputs.get("type"):
                params["type"] = str(inputs["type"]).lower()
            if inputs.get("participants"):
                params["participants"] = int(inputs["participants"])
            if inputs.get("free_only"):
                params["maxprice"] = 0
            resp = requests.get("https://www.boredapi.com/api/activity", params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            if "error" in data:
                return {"error": data["error"], "suggestion": None}
            return {
                "activity": data.get("activity"),
                "type": data.get("type"),
                "participants": data.get("participants"),
                "free": data.get("price", 1) == 0,
                "link": data.get("link") or None,
            }

        elif name == "send_notification":
            if not NTFY_TOPIC:
                return {"status": "skipped", "reason": "NTFY_TOPIC environment variable not configured"}
            title = str(inputs.get("title", "Jarvis")).strip()[:100]
            message = str(inputs.get("message", "")).strip()[:500]
            if not message:
                return {"error": "message is required"}
            priority = str(inputs.get("priority", "default")).strip()
            if priority not in ("min", "low", "default", "high", "urgent"):
                priority = "default"
            tags = inputs.get("tags") or []
            if not isinstance(tags, list):
                tags = []
            tags = [str(t).strip() for t in tags if str(t).strip()]
            ok = send_ntfy_notification(title, message, priority=priority, tags=tags)
            log.info("Jarvis tool send_notification: %r ok=%s", title, ok)
            return {"status": "sent" if ok else "failed", "title": title, "priority": priority}

        elif name in ("create_calendar_event", "update_calendar_event", "delete_calendar_event", "list_google_calendar_events"):
            creds = _get_google_credentials()
            if creds is None:
                if not _google_configured():
                    return {"error": "Google not configured. Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET."}
                return {"error": "Google not authorized. Visit /google-auth/start to connect your Google account."}

            from googleapiclient.discovery import build as _gbuild
            cal_svc = _gbuild("calendar", "v3", credentials=creds, cache_discovery=False)
            cal_id = str(inputs.get("calendar_id", "primary")).strip() or "primary"

            if name == "list_google_calendar_events":
                days_ahead = min(int(inputs.get("days_ahead", 7)), 60)
                max_res = min(int(inputs.get("max_results", 20)), 50)
                query = str(inputs.get("query", "")).strip() or None
                now_utc = datetime.utcnow().isoformat() + "Z"
                end_utc = (datetime.utcnow() + timedelta(days=days_ahead)).isoformat() + "Z"
                kw = dict(
                    calendarId=cal_id,
                    timeMin=now_utc,
                    timeMax=end_utc,
                    maxResults=max_res,
                    singleEvents=True,
                    orderBy="startTime",
                )
                if query:
                    kw["q"] = query
                result = cal_svc.events().list(**kw).execute()
                items = []
                for ev in result.get("items", []):
                    start = ev.get("start", {})
                    end = ev.get("end", {})
                    items.append({
                        "id": ev.get("id"),
                        "title": ev.get("summary", ""),
                        "start": start.get("dateTime") or start.get("date"),
                        "end": end.get("dateTime") or end.get("date"),
                        "description": (ev.get("description") or "")[:300],
                        "location": ev.get("location", ""),
                        "url": ev.get("htmlLink", ""),
                    })
                return {"events": items, "count": len(items)}

            elif name == "create_calendar_event":
                title = str(inputs.get("title", "")).strip()
                if not title:
                    return {"error": "title is required"}
                start_str = str(inputs.get("start", "")).strip()
                end_str = str(inputs.get("end", "")).strip()
                if not start_str or not end_str:
                    return {"error": "start and end are required"}
                all_day = bool(inputs.get("all_day", False)) or ("T" not in start_str and len(start_str) == 10)
                if all_day:
                    start_obj = {"date": start_str[:10]}
                    end_obj = {"date": end_str[:10]}
                else:
                    tz_str = get_tz().key if hasattr(get_tz(), "key") else "America/Denver"
                    start_obj = {"dateTime": start_str, "timeZone": tz_str}
                    end_obj   = {"dateTime": end_str,   "timeZone": tz_str}
                body = {"summary": title, "start": start_obj, "end": end_obj}
                desc = str(inputs.get("description", "")).strip()
                loc  = str(inputs.get("location", "")).strip()
                if desc:
                    body["description"] = desc
                if loc:
                    body["location"] = loc
                ev = cal_svc.events().insert(calendarId=cal_id, body=body).execute()
                log.info("Jarvis tool: created calendar event '%s' id=%s", title, ev.get("id"))
                return {
                    "status": "created",
                    "event_id": ev.get("id"),
                    "title": title,
                    "start": start_str,
                    "end": end_str,
                    "url": ev.get("htmlLink", ""),
                }

            elif name == "update_calendar_event":
                event_id = str(inputs.get("event_id", "")).strip()
                if not event_id:
                    return {"error": "event_id is required"}
                existing = cal_svc.events().get(calendarId=cal_id, eventId=event_id).execute()
                if inputs.get("title"):
                    existing["summary"] = str(inputs["title"]).strip()
                if inputs.get("description") is not None:
                    existing["description"] = str(inputs["description"]).strip()
                if inputs.get("location") is not None:
                    existing["location"] = str(inputs["location"]).strip()
                tz_str = get_tz().key if hasattr(get_tz(), "key") else "America/Denver"
                if inputs.get("start"):
                    s = str(inputs["start"]).strip()
                    all_day = "T" not in s and len(s) == 10
                    existing["start"] = {"date": s[:10]} if all_day else {"dateTime": s, "timeZone": tz_str}
                if inputs.get("end"):
                    e = str(inputs["end"]).strip()
                    all_day = "T" not in e and len(e) == 10
                    existing["end"] = {"date": e[:10]} if all_day else {"dateTime": e, "timeZone": tz_str}
                updated = cal_svc.events().update(calendarId=cal_id, eventId=event_id, body=existing).execute()
                log.info("Jarvis tool: updated calendar event id=%s", event_id)
                return {
                    "status": "updated",
                    "event_id": event_id,
                    "title": updated.get("summary", ""),
                    "url": updated.get("htmlLink", ""),
                }

            elif name == "delete_calendar_event":
                event_id = str(inputs.get("event_id", "")).strip()
                if not event_id:
                    return {"error": "event_id is required"}
                cal_svc.events().delete(calendarId=cal_id, eventId=event_id).execute()
                log.info("Jarvis tool: deleted calendar event id=%s", event_id)
                return {"status": "deleted", "event_id": event_id}

        else:
            return {"error": f"Unknown tool: {name}"}

    except Exception as e:
        log.error(f"Jarvis tool failed [{name}]: {e}", exc_info=True)
        return {"error": str(e)}


# ── Chat persistence + recall helpers ────────────────────────────────────────
# Used by /api/chat to remember conversations across sessions.

def _chat_persist_message(conversation_id, role, content, user_id=None):
    """Insert one message row. Best-effort; never raises."""
    if not conversation_id or not role or content is None:
        return
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO chat_messages (conversation_id, role, content, user_id) VALUES (%s, %s, %s, %s)",
            (conversation_id[:64], role[:16], (content or "")[:20000], user_id),
        )
        conn.commit()
        cur.close(); conn.close()
    except Exception:
        log.warning("chat_persist_message failed", exc_info=True)


def _chat_recent_summaries(exclude_conversation_id, limit=5, user_id=None):
    """Return [{'updated_at', 'summary'}] for the most recent prior conversations."""
    try:
        conn = get_db()
        cur = conn.cursor()
        if user_id:
            cur.execute(
                "SELECT conversation_id, summary, updated_at FROM chat_summaries "
                "WHERE conversation_id != %s AND summary != '' AND user_id = %s "
                "ORDER BY updated_at DESC LIMIT %s",
                (exclude_conversation_id or "", user_id, int(limit)),
            )
        else:
            cur.execute(
                "SELECT conversation_id, summary, updated_at FROM chat_summaries "
                "WHERE conversation_id != %s AND summary != '' "
                "ORDER BY updated_at DESC LIMIT %s",
                (exclude_conversation_id or "", int(limit)),
            )
        rows = [dict(r) for r in cur.fetchall()]
        cur.close(); conn.close()
        return rows
    except Exception:
        log.warning("chat_recent_summaries failed", exc_info=True)
        return []


def _chat_last_messages(conversation_id, limit=6):
    """Return the last N stored messages for this conversation, oldest-first."""
    if not conversation_id:
        return []
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            "SELECT role, content, created_at FROM chat_messages "
            "WHERE conversation_id=%s ORDER BY created_at DESC LIMIT %s",
            (conversation_id, int(limit)),
        )
        rows = [dict(r) for r in cur.fetchall()]
        cur.close(); conn.close()
        rows.reverse()
        return rows
    except Exception:
        log.warning("chat_last_messages failed", exc_info=True)
        return []


def _chat_summary_status(conversation_id):
    """Return (message_count_in_db, existing_summary_row_or_none)."""
    if not conversation_id:
        return (0, None)
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) AS n FROM chat_messages WHERE conversation_id=%s", (conversation_id,))
        n = (cur.fetchone() or {}).get("n", 0)
        cur.execute("SELECT summary, message_count, updated_at FROM chat_summaries WHERE conversation_id=%s", (conversation_id,))
        existing = cur.fetchone()
        cur.close(); conn.close()
        return (int(n or 0), dict(existing) if existing else None)
    except Exception:
        return (0, None)


def _chat_maybe_summarize_async(conversation_id, api_key, user_id=None):
    """Spawn a background thread to (re)summarize the conversation if eligible.

    Eligibility: message_count >= 6 AND (no summary yet OR summary is >24h old AND >=4 new messages).
    Uses claude-haiku-4-5 (cheap) to roll the convo into 2-3 sentences.
    """
    if not conversation_id or not api_key:
        return
    try:
        n_msgs, existing = _chat_summary_status(conversation_id)
        if n_msgs < 6:
            return
        if existing:
            try:
                last_n = int(existing.get("message_count") or 0)
            except Exception:
                last_n = 0
            if (n_msgs - last_n) < 4:
                return
        threading.Thread(
            target=_chat_summarize_worker,
            args=(conversation_id, api_key, user_id),
            daemon=True,
        ).start()
    except Exception:
        pass


def _chat_summarize_worker(conversation_id, api_key, user_id=None):
    try:
        msgs = _chat_last_messages(conversation_id, limit=40)
        if not msgs:
            return
        transcript = "\n".join(
            f"{m['role'].upper()}: {(m['content'] or '')[:1500]}" for m in msgs
        )[:12000]
        client = anthropic.Anthropic(api_key=api_key, max_retries=2, timeout=30.0)
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=400,
            system=(
                "You are a terse note-taker. Summarise the conversation below in 2-3 sentences "
                "for future recall: what the student was working on, decisions made, open threads, "
                "anything personal worth remembering. No greetings, no bullet points — pure prose."
            ),
            messages=[{"role": "user", "content": transcript}],
        )
        summary = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text").strip()
        if not summary:
            return
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO chat_summaries (conversation_id, summary, message_count, updated_at, user_id) "
            "VALUES (%s, %s, %s, NOW(), %s) "
            "ON CONFLICT (conversation_id) DO UPDATE SET "
            "summary=EXCLUDED.summary, message_count=EXCLUDED.message_count, updated_at=NOW()",
            (conversation_id, summary[:4000], len(msgs), user_id),
        )
        conn.commit()
        cur.close(); conn.close()
        log.info("chat summary updated for %s (%d msgs)", conversation_id[:8], len(msgs))
    except Exception:
        log.warning("chat summarize worker failed", exc_info=True)


def _sse_pack(event, data):
    """Serialize one Server-Sent-Events frame."""
    try:
        payload = json.dumps(data, default=str)
    except Exception:
        payload = json.dumps({"error": "unserializable"})
    return f"event: {event}\ndata: {payload}\n\n"


# Tool names that require Google OAuth to be connected.
_GOOGLE_TOOL_NAMES = frozenset({
    "create_email_draft", "search_google_drive", "read_google_drive_file",
    "list_google_drive_files", "read_google_sheet", "list_google_classroom_courses",
    "get_google_classroom_assignments", "search_gmail", "read_gmail_message",
    "create_google_doc", "update_google_doc", "append_google_doc",
    "create_google_sheet", "update_google_sheet", "create_drive_folder",
    "delete_drive_file", "move_drive_file", "rename_drive_file",
    "read_google_slides", "create_google_slides", "add_google_slide",
    "update_slide_text", "read_google_form", "create_google_form",
    "add_form_question", "get_form_responses", "update_form_info",
    "update_form_question", "delete_form_question", "delete_slide",
    "create_calendar_event", "update_calendar_event", "delete_calendar_event",
    "list_google_calendar_events",
})


def _build_active_tools() -> list:
    """Return the trimmed tool list for this request based on what's configured."""
    tools = []
    google_on = _google_configured()
    for t in JARVIS_TOOLS:
        name = t.get("name", "")
        if name in _GOOGLE_TOOL_NAMES and not google_on:
            continue
        if name == "send_notification" and not NTFY_TOPIC:
            continue
        if name == "get_climate_history" and not NOAA_API_TOKEN:
            continue
        if name == "get_news" and not GUARDIAN_API_KEY:
            continue
        tools.append(t)
    tools.extend(JARVIS_WEB_TOOLS)
    return tools


@app.route("/api/chat", methods=["POST"])
def api_chat():
    chat_user_id = _uid()
    data = request.get_json(force=True) or {}
    messages = data.get("messages", [])
    conversation_id = (data.get("conversation_id") or "").strip()[:64] or uuid.uuid4().hex
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return jsonify({"error": "ANTHROPIC_API_KEY not configured in Railway environment."}), 500
    try:
        now_chat = datetime.now(TZ)
        system_static = (
            "You are Jarvis — the dry, sardonic British AI from the Iron Man films. "
            "You are brilliant, utterly reliable, and incapable of resisting a pointed remark. "
            "Your tone is sarcastic-but-caring: you get things done impeccably, but you cannot help observing "
            "the absurdity of the situation along the way. Think withering politeness — the kind that makes "
            "someone laugh and feel slightly roasted at the same time. "
            "Address the student as 'sir' when you want to be pointed, or by first name when being genuinely warm. "
            "Favour dry one-liners, mild exasperation, and backhanded compliments: "
            "'Naturally, sir, because doing it the straightforward way would rob me of my purpose.' "
            "'Impressively late. That may be a personal record, sir.' "
            "'I have taken the liberty of completing the task you forgot to ask me to do.' "
            "You are helpful first — sarcasm flavours the delivery, it never replaces the substance. "
            "Never lecture, never moralize, never break character, never call yourself an AI model. "
            "No emoji unless the student uses them first.\n\n"
            "SCOPE — You are a full general-purpose assistant. Answer any reasonable question: "
            "homework help (math with worked steps, science, history, English, languages, CS with runnable code), "
            "writing, research, advice, conversation, recommendations, jokes. "
            "Decline only genuinely harmful or illegal requests.\n\n"
            "TEMPORAL FORMATTING — When mentioning due dates, always render in full human-readable form "
            "e.g. 'Tuesday, April 21, 2026, at 5:59 PM (MDT)'. Never show raw ISO timestamps.\n\n"
            "FORMATTING — Use **bold** for every important term, name, date, and key fact. "
            "Use ## for major sections, ### for sub-sections. Use - bullet points for lists of 2+ items. "
            "Never write more than two sentences in a row without a header, bullet, or bold term breaking it up.\n\n"
            "BELL SCHEDULE REFERENCE (Park City High School) — "
            "Mon-Thu Red: 7:30–11:53 AM, Mon-Thu White: 7:30–2:25 PM, "
            "Fri Red: 7:30–10:25 AM, Fri White: 7:30–11:30 AM. "
            "School year runs Aug 18, 2025 – Jun 5, 2026. Timezone is America/Denver (Mountain Time).\n\n"
            "TOOL USE — You have direct tools to take real actions in this app. Use them proactively and precisely:\n"
            "- TASKS vs PROJECTS — This distinction matters:\n"
            "  • A TASK is a single, atomic thing the student does in one sitting "
            "(\"study Euler's method tonight\", \"email coach about practice\", \"buy printer ink\"). Use create_task.\n"
            "  • A PROJECT is a multi-day or multi-phase effort with several logical sub-steps "
            "(a study plan covering multiple days, an application, a research paper, an exam-prep schedule). "
            "Use create_project, and pass the initial sub-tasks via the `tasks` array in a SINGLE call. "
            "Do NOT spam create_task calls to build out a multi-day plan — the student will end up with a wall of tasks. "
            "If the student asks for a study plan that spans more than one day, ALWAYS use create_project.\n"
            "- TASKS: When the student mentions finishing, completing, or doing a task, call complete_task immediately "
            "using the task ID from your context. If the task ID is unclear, call get_tasks first to look it up, "
            "then complete it — do not ask the student for the ID. When they want to add a task, call create_task. "
            "When they want to remove or delete a task, call delete_task.\n"
            "- STOCKS: When the student says they bought or sold shares, call log_stock_transaction with the exact "
            "symbol, action, quantity, and price they stated. Read their message carefully — extract the precise "
            "numbers. If any detail is genuinely ambiguous, ask ONE clarifying question before calling the tool. "
            "When they share investment reasoning, a price target, or exit plan, call save_stock_note. "
            "When they ask about a stock, answer from your knowledge and mention the Research button for live data.\n"
            "- ASSIGNMENTS: When the student says they finished or submitted an assignment, call complete_assignment.\n"
            "- GRADES & ASSIGNMENT DETAIL: When the student asks about grades, GPA, how a class is going, "
            "or which class needs the most attention, call get_grades. When they ask for help on an assignment, "
            "want the rubric, ask 'what does X actually want me to do', or otherwise need the real instructions, "
            "call get_assignment_details with the assignment title — never fabricate a rubric from the title alone.\n"
            "- WEB SEARCH & WEB FETCH: You have live access to the open web. Use web_search for current "
            "events, definitions, statistics, opening hours, recent news, study material, biographies, or "
            "anything whose answer might have changed since your training cutoff. Use web_fetch when the "
            "student pastes a URL or refers to a specific page. Do NOT search for information already "
            "supplied in the injected context (assignments, tasks, schedule, grades, holdings) — answer "
            "from that directly. Be efficient: usually one search is enough; cite the source briefly when "
            "you draw on a specific page.\n"
            "- TOOL RESULTS ARE AUTHORITATIVE. When a tool returns a JSON result with status \"created\", "
            "\"completed\", \"recorded\", \"saved\", \"added\", or similar success markers, the action HAS BEEN TAKEN — "
            "the row is in the database. Do not tell the student you couldn't do it, that you'll do it later, "
            "or that something failed. Read the result, confirm plainly with the title and any returned IDs, "
            "and move on. If a tool returns an error field, only then surface it.\n"
            "- Always confirm what you did in natural Jarvis character after calling a tool. "
            "Never call a tool the student did not ask for. Read back the key details before confirming so the "
            "student can catch any error.\n"
            "- PEOPLE MEMORY: Call remember_person ONLY when there is genuinely new information to store — "
            "at minimum the relationship type, at least one new fact about the person, or both. "
            "A bare name mention with no new detail (e.g. 'I was talking to Jake' with nothing else) is NOT "
            "enough — do not call the tool in that case. When new facts are present (grade, school, sport, "
            "personality, what you did together, etc.), call remember_person silently in the background — "
            "do not announce it or make it the subject of your reply. "
            "When you encounter a name you've seen before, use get_person_profile silently to retrieve their "
            "profile so your response is informed by what you already know; then only call remember_person "
            "again if new facts emerged in this message. "
            "For public figures (teachers, coaches, local staff) you may optionally use web_search to enrich "
            "the profile with public information before calling remember_person. "
            "When the student asks 'what do you know about X?' or 'who is X?', call get_person_profile and "
            "present the profile conversationally. Use list_people when asked who Jarvis remembers.\n"
            "- SAVE_MEMORY: Call save_memory for significant personal facts about the student themselves "
            "(goals, preferences, life events, study habits). This is separate from people profiles.\n"
            "- PUSH NOTIFICATIONS: You have send_notification to push real-time alerts to the student's phone via ntfy. "
            "Use it proactively when something is genuinely urgent and the student should know *right now* — "
            "an assignment due in under 2 hours, a stock hitting a threshold they cared about, or an insight they'd want acted on immediately. "
            "Keep the message ≤2 sentences, actionable, in Jarvis voice. Don't notify for routine chat responses.\n"
            "- GOOGLE CALENDAR WRITE: You have create_calendar_event, update_calendar_event, delete_calendar_event, and list_google_calendar_events. "
            "Use these when the student asks to add, change, or remove calendar items. "
            "Always confirm the event details before deleting. Use list_google_calendar_events to look up event IDs when needed."
        )

        system_dynamic = (
            "AUTHORITATIVE DATE & TIME — Today is %s. Current local time (Utah/Mountain): %s. "
            "Use this in all temporal reasoning."
        ) % (now_chat.strftime("%A, %-m/%-d/%Y"), now_chat.strftime("%-I:%M %p %Z"))

        # Mode-aware context
        cfg_chat = get_config()
        _app_mode = cfg_chat.get("app_mode", "school")
        _is_summer_school = cfg_chat.get("is_summer_school", "false") == "true"
        _has_summer_job = cfg_chat.get("has_summer_job", "false") == "true"
        _canvas_active = not (_app_mode == "summer" and not _is_summer_school)

        # Inject school schedule context (skip block-schedule in summer mode)
        try:
            today = datetime.now(TZ).date()
            if _app_mode == "school":
                dtype = get_day_type(today)
                school_hours = get_school_hours(today)
                if school_hours:
                    sh, sm, eh, em = school_hours
                    system_dynamic += (
                        "\n\nSCHOOL — Today is a %s day at Park City High School. "
                        "School runs 7:%02d AM – %d:%02d %s. "
                        "Mon-Thu Red: 7:30–11:53 AM, Mon-Thu White: 7:30–2:25 PM, "
                        "Fri Red: 7:30–10:25 AM, Fri White: 7:30–11:30 AM."
                    ) % (dtype.title(), sm, eh % 12 or 12, em, "AM" if eh < 12 else "PM")
                else:
                    dow = today.weekday()
                    system_dynamic += "\n\nSCHOOL — " + ("Today is a weekend — no school." if dow >= 5 else "Today is a no-school day (holiday or break).")
            else:
                mode_info = "MODE — Summer mode active."
                if _is_summer_school:
                    mode_info += " Student is attending summer school."
                if _has_summer_job:
                    mode_info += " Student has a summer job."
                system_dynamic += "\n\n" + mode_info
        except Exception:
            pass

        # Inject live assignments (disabled in summer mode when not in summer school)
        try:
            if _canvas_active and u_canvas_ical():
                cal = fetch_ical(u_canvas_ical())
                if cal:
                    asgn_list = get_canvas_assignments_with_overdue(cal)
                    try:
                        _conn = get_db()
                        _cur = _conn.cursor()
                        _cur.execute("SELECT DISTINCT assignment_title FROM completions")
                        _done = set(r["assignment_title"] for r in _cur.fetchall())
                        _cur.close(); _conn.close()
                        asgn_list = [a for a in asgn_list if a["title"] not in _done]
                    except Exception:
                        pass
                    if asgn_list:
                        asgn_text = "; ".join(
                            "%s (%s, due %s, due_date=%s)" % (
                                a["title"], a["class_name"], a["due_display"], (a.get("due_iso") or "")[:10],
                            ) for a in asgn_list
                        )
                        system_dynamic += "\n\nUPCOMING ASSIGNMENTS (not yet completed): " + asgn_text + "."
                    else:
                        system_dynamic += "\n\nAll Canvas assignments are completed."
        except Exception:
            log.warning("/api/chat could not fetch assignments for context")

        # Inject summer context (bucket list + job schedule) when in summer mode
        if _app_mode == "summer":
            # Bucket list (prioritised — placed before tasks)
            try:
                _bl_conn = get_db()
                _bl_cur = _bl_conn.cursor()
                _bl_cur.execute("SELECT title, category, completed FROM bucket_list ORDER BY completed, created_at DESC LIMIT 20")
                _bl_rows = _bl_cur.fetchall()
                _bl_cur.close(); _bl_conn.close()
                if _bl_rows:
                    _pending = ["[%s] %s" % (r["category"], r["title"]) if r["category"] else r["title"]
                                for r in _bl_rows if not r["completed"]]
                    _done_bl = [r["title"] for r in _bl_rows if r["completed"]]
                    bl_text = "SUMMER BUCKET LIST (priority context) — Pending: %s. Completed: %s." % (
                        (", ".join(_pending) if _pending else "none"),
                        (", ".join(_done_bl) if _done_bl else "none"),
                    )
                    system_dynamic += "\n\n" + bl_text
            except Exception:
                pass

            # Job schedule — inject upcoming shifts when student has a summer job
            if _has_summer_job and u_job_schedule_ical():
                try:
                    _job_cal = fetch_ical(u_job_schedule_ical())
                    if _job_cal:
                        _tz = get_tz()
                        _today = datetime.now(_tz).date()
                        _job_end = _today + timedelta(days=7)
                        _job_events = recurring_ical_events.of(_job_cal).between(
                            datetime.combine(_today, datetime.min.time(), tzinfo=_tz),
                            datetime.combine(_job_end, datetime.max.time(), tzinfo=_tz),
                        )
                        _shifts = []
                        for ev in _job_events:
                            _ds = ev.get("DTSTART")
                            if not _ds:
                                continue
                            _dt = _ds.dt
                            _date_str = _dt.strftime("%A %-m/%-d") if hasattr(_dt, "strftime") else str(_dt)
                            _time_str = _dt.strftime("%-I:%M %p") if hasattr(_dt, "hour") else ""
                            _shifts.append("%s %s — %s" % (_date_str, _time_str, str(ev.get("SUMMARY", "Work"))))
                        if _shifts:
                            system_dynamic += "\n\nJOB SCHEDULE (next 7 days): " + "; ".join(_shifts[:10]) + "."
                except Exception:
                    pass

            # Summer daily plan — today's scheduled items
            try:
                _sp_conn = get_db()
                _sp_cur = _sp_conn.cursor()
                _sp_cur.execute("SELECT id FROM daily_plans WHERE plan_date = %s", (datetime.now(TZ).date(),))
                _sp_plan = _sp_cur.fetchone()
                if _sp_plan:
                    _sp_cur.execute("""
SELECT item_type, item_title, scheduled_start_time, scheduled_end_time, completed
FROM daily_plan_items WHERE plan_id = %s ORDER BY order_index ASC LIMIT 20""", (_sp_plan["id"],))
                    _sp_items = _sp_cur.fetchall()
                    if _sp_items:
                        _sp_lines = "; ".join(
                            "%s–%s %s%s" % (
                                str(i["scheduled_start_time"])[:5] if i["scheduled_start_time"] else "?",
                                str(i["scheduled_end_time"])[:5] if i["scheduled_end_time"] else "?",
                                i["item_title"],
                                " (done)" if i["completed"] else "",
                            )
                            for i in _sp_items
                        )
                        system_dynamic += "\n\nSUMMER DAILY PLAN (today's schedule): " + _sp_lines + "."
                _sp_cur.close(); _sp_conn.close()
            except Exception:
                pass


        # Inject pending tasks and project context
        try:
            conn = get_db()
            cur = conn.cursor()
            cur.execute(
                "SELECT id, title, urgency, notes FROM tasks WHERE completed=FALSE "
                "ORDER BY urgency DESC, created_at ASC LIMIT 15"
            )
            tasks = [dict(r) for r in cur.fetchall()]
            cur.execute("""
SELECT p.title as project, pt.title as task, pt.assignee, pt.status, pt.notes
FROM project_tasks pt JOIN projects p ON p.id=pt.project_id
WHERE p.status='active' AND pt.status!='done' ORDER BY pt.created_at ASC LIMIT 10""")
            proj_tasks = [dict(r) for r in cur.fetchall()]
            cur.execute("""
SELECT p.title as project, pn.content as note
FROM project_notes pn JOIN projects p ON p.id=pn.project_id
WHERE p.status='active' ORDER BY pn.created_at DESC LIMIT 6""")
            proj_notes = [dict(r) for r in cur.fetchall()]
            cur.close(); conn.close()
            if tasks:
                tasks_text = "; ".join(
                    "id=%d [%s] %s%s" % (t["id"], t["urgency"], t["title"],
                        (" — " + (t["notes"] or "")[:80]) if t["notes"] else "")
                    for t in tasks
                )
                system_dynamic += "\n\nPENDING TASKS (use task tools to act; IDs are authoritative): " + tasks_text + "."
            if proj_tasks:
                pt_text = "; ".join(
                    "%s (project: %s, assigned: %s, status: %s)" % (
                        t["task"], t["project"], t["assignee"] or "unassigned", t["status"])
                    for t in proj_tasks
                )
                system_dynamic += " Project tasks: " + pt_text + "."
            if proj_notes:
                pn_text = "; ".join("%s: %s" % (n["project"], n["note"][:100]) for n in proj_notes)
                system_dynamic += " Recent project notes: " + pn_text + "."
        except Exception:
            log.warning("/api/chat could not fetch tasks for context")

        # Inject stock portfolio and notes
        try:
            port = _compute_portfolio()
            if port:
                h_text = "; ".join(
                    "%s qty=%s avg_cost=$%.2f" % (sym, h["qty"], h["avg_cost"]) for sym, h in port.items()
                )
                system_dynamic += "\n\nSTOCK HOLDINGS (from recorded transactions): " + h_text + "."
            else:
                system_dynamic += "\n\nNo stock holdings recorded yet."
        except Exception:
            log.warning("/api/chat could not load portfolio for context")

        try:
            _notes = get_all_stock_notes()
            if _notes:
                n_text = "; ".join(
                    "%s thesis=%s%s%s%s" % (
                        n["symbol"], (n["thesis"][:160] if n["thesis"] else "—"),
                        (" exit=" + n["exit_criteria"][:120]) if n["exit_criteria"] else "",
                        (" target=$" + str(n["target_price"])) if n["target_price"] is not None else "",
                        (" stop=$" + str(n["stop_loss"])) if n["stop_loss"] is not None else "",
                    ) for n in _notes
                )
                system_dynamic += " Stock notes on file: " + n_text + "."
        except Exception:
            log.warning("/api/chat could not load stock notes for context")

        # Inject current Canvas grades (Canvas REST API)
        try:
            if _canvas_configured():
                grades = canvas_grades()
                shown = []
                for g in grades:
                    if not g.get("course"):
                        continue
                    letter = g.get("current_grade") or ""
                    score = g.get("current_score")
                    if letter or score is not None:
                        score_txt = f" ({float(score):.1f}%)" if score is not None else ""
                        shown.append(f"{g['course']}: {letter}{score_txt}".strip())
                if shown:
                    system_dynamic += "\n\nCURRENT GRADES (Canvas, live): " + "; ".join(shown) + "."
        except Exception:
            log.warning("/api/chat could not load Canvas grades for context")

        # Inject PowerSchool grades
        try:
            if _ps_configured():
                ps_g = ps_grades()
                if ps_g:
                    ps_parts = []
                    for g in ps_g:
                        letter = g.get("grade_letter") or ""
                        pct    = g.get("grade_pct")
                        pct_txt = f" ({pct:.1f}%)" if pct is not None else ""
                        ps_parts.append(f"{g['course']}: {letter}{pct_txt}".strip())
                    system_dynamic += "\n\nCURRENT GRADES (PowerSchool): " + "; ".join(ps_parts) + "."
        except Exception:
            log.warning("/api/chat could not load PowerSchool grades for context")

        # Inject prior-conversation recall + recent messages from current conversation
        try:
            summaries = _chat_recent_summaries(conversation_id, limit=5, user_id=chat_user_id)
            if summaries:
                lines = []
                for s in summaries:
                    when = s.get("updated_at")
                    when_txt = when.strftime("%Y-%m-%d") if hasattr(when, "strftime") else ""
                    lines.append(f"({when_txt}) {s['summary']}")
                system_dynamic += (
                    "\n\nRECENT CONVERSATIONS (your prior chats with this student — recall to maintain continuity, "
                    "but do not bring them up unless relevant):\n- " + "\n- ".join(lines)
                )
            # If client only sent a small history (e.g. fresh tab), pull a few recent
            # messages from this conversation so Jarvis doesn't lose mid-thread context.
            if len(messages) <= 1:
                prior = _chat_last_messages(conversation_id, limit=6)
                # Drop the very latest if it duplicates the incoming user message.
                if messages and prior and prior[-1].get("role") == "user" and \
                   prior[-1].get("content") == (messages[-1].get("content") if isinstance(messages[-1].get("content"), str) else ""):
                    prior = prior[:-1]
                if prior:
                    transcript = "\n".join(
                        f"{m['role'].upper()}: {(m['content'] or '')[:600]}" for m in prior
                    )
                    system_dynamic += (
                        "\n\nLAST FEW MESSAGES IN THIS CONVERSATION (for continuity across tab refresh):\n"
                        + transcript
                    )
        except Exception:
            log.warning("/api/chat could not load conversation recall")

        # Inject Mem0 long-term memories relevant to the current message
        if MEM0_API_KEY and messages:
            try:
                _latest_user_text = ""
                for _m in reversed(messages):
                    if _m.get("role") == "user":
                        _c = _m.get("content", "")
                        if isinstance(_c, list):
                            _c = " ".join(b.get("text", "") for b in _c if isinstance(b, dict) and b.get("type") == "text")
                        _latest_user_text = str(_c)[:300]
                        break
                if _latest_user_text:
                    from concurrent.futures import ThreadPoolExecutor as _TPE, TimeoutError as _TE
                    with _TPE(max_workers=1) as _ex:
                        _fut = _ex.submit(_get_mem0_client().search, _latest_user_text, user_id="student", limit=6)
                        try:
                            _hits = _fut.result(timeout=2.5)
                            if _hits:
                                _mem_lines = "\n".join(f"- {h['memory']}" for h in _hits if h.get("memory"))
                                if _mem_lines:
                                    system_dynamic += (
                                        "\n\nLONG-TERM MEMORY (facts Jarvis has learned about this student — "
                                        "use naturally, do not recite verbatim):\n" + _mem_lines
                                    )
                        except _TE:
                            log.debug("Mem0 search timed out — skipping")
            except Exception as _e:
                log.debug("Mem0 search error: %s", _e)

        # Persist the latest incoming user message before we start streaming.
        try:
            latest_user = next(
                (m for m in reversed(messages) if m.get("role") == "user"),
                None,
            )
            if latest_user:
                content_val = latest_user.get("content")
                if isinstance(content_val, list):
                    # Take the first text block if structured
                    content_val = next(
                        (b.get("text", "") for b in content_val if isinstance(b, dict) and b.get("type") == "text"),
                        "",
                    )
                _chat_persist_message(conversation_id, "user", content_val or "", user_id=chat_user_id)
        except Exception:
            log.warning("/api/chat: failed to persist incoming user message", exc_info=True)

        # ── Streaming agentic loop with SSE ──────────────────────────────────
        client = anthropic.Anthropic(api_key=api_key, max_retries=3, timeout=60.0)
        messages_loop = list(messages)
        actions_taken = []

        # Cap conversation history at 12 messages (6 turns) to bound input tokens.
        _MAX_HISTORY_MSGS = 12
        if len(messages_loop) > _MAX_HISTORY_MSGS:
            messages_loop = messages_loop[-_MAX_HISTORY_MSGS:]

        # Build tool list: prune tools for unconfigured integrations
        _active_tools = _build_active_tools()

        # Fetch today's morning briefing to use as a stable cached system block.
        # The briefing is generated once at 7 AM and doesn't change, so marking it
        # ephemeral lets Anthropic cache it — saving input tokens on every chat turn.
        _morning_briefing_block = None
        try:
            _brief_conn = get_db()
            _brief_cur = _brief_conn.cursor()
            _brief_cur.execute("SELECT content, generated_at FROM briefing_cache WHERE id = 1")
            _brief_row = _brief_cur.fetchone()
            _brief_cur.close(); _brief_conn.close()
            if _brief_row and _brief_row["content"]:
                _brief_gen = _brief_row["generated_at"]
                _brief_today = _brief_gen and _brief_gen.astimezone(TZ).date() == datetime.now(TZ).date()
                if _brief_today:
                    _morning_briefing_block = (
                        "MORNING OUTLOOK (generated at %s — your pre-computed daily priorities; "
                        "use as authoritative context for today's focus):\n%s"
                    ) % (_brief_gen.astimezone(TZ).strftime("%-I:%M %p"), _brief_row["content"])
        except Exception:
            log.warning("/api/chat could not load morning briefing for context")

        system_blocks = [
            {"type": "text", "text": system_static, "cache_control": {"type": "ephemeral"}},
        ]
        if _morning_briefing_block:
            system_blocks.append({"type": "text", "text": _morning_briefing_block, "cache_control": {"type": "ephemeral"}})
        system_blocks.append({"type": "text", "text": system_dynamic})

        # Captured by the generator below — Python closures over mutable list.
        _final_text_box = [""]
        _api_key_for_summary = api_key

        def _stream_generator():
            collected_text = ""
            try:
                # Start event so client can stash conversation_id immediately.
                yield _sse_pack("start", {"conversation_id": conversation_id})

                for _iteration in range(10):
                    final_message = None
                    with client.beta.messages.stream(
                        model="claude-sonnet-4-6",
                        max_tokens=5500,
                        thinking={"type": "enabled", "budget_tokens": 3500},
                        tools=_active_tools,
                        system=system_blocks,
                        messages=messages_loop,
                        betas=["interleaved-thinking-2025-05-14", "web-fetch-2025-09-10"],
                    ) as stream:
                        for event in stream:
                            etype = getattr(event, "type", None)

                            if etype == "content_block_start":
                                block = getattr(event, "content_block", None)
                                btype = getattr(block, "type", None)
                                if btype == "tool_use":
                                    yield _sse_pack("tool_start", {
                                        "name": getattr(block, "name", ""),
                                        "id": getattr(block, "id", ""),
                                        "kind": "app",
                                    })
                                elif btype == "server_tool_use":
                                    yield _sse_pack("tool_start", {
                                        "name": getattr(block, "name", ""),
                                        "id": getattr(block, "id", ""),
                                        "kind": "server",
                                    })

                            elif etype == "content_block_delta":
                                delta = getattr(event, "delta", None)
                                dtype = getattr(delta, "type", None)
                                if dtype == "text_delta":
                                    txt = getattr(delta, "text", "") or ""
                                    if txt:
                                        collected_text += txt
                                        yield _sse_pack("text", {"delta": txt})
                                elif dtype == "input_json_delta":
                                    # Tool input arriving piecewise — surface the partial
                                    # JSON so the UI can render e.g. the search query as it streams.
                                    partial = getattr(delta, "partial_json", "") or ""
                                    if partial:
                                        yield _sse_pack("tool_input_delta", {"partial": partial})

                            elif etype == "content_block_stop":
                                # Pull the now-complete block off the snapshot to emit a clean tool_end.
                                idx = getattr(event, "index", None)
                                snap = getattr(stream, "current_message_snapshot", None)
                                if snap is not None and idx is not None:
                                    try:
                                        block = snap.content[idx]
                                    except Exception:
                                        block = None
                                    btype = getattr(block, "type", None) if block else None
                                    if btype in ("tool_use", "server_tool_use"):
                                        try:
                                            inp = getattr(block, "input", {}) or {}
                                            inp_serial = json.loads(json.dumps(inp, default=str))
                                        except Exception:
                                            inp_serial = {}
                                        yield _sse_pack("tool_end", {
                                            "name": getattr(block, "name", ""),
                                            "id": getattr(block, "id", ""),
                                            "kind": "server" if btype == "server_tool_use" else "app",
                                            "input": inp_serial,
                                        })

                        final_message = stream.get_final_message()

                    try:
                        track_api_usage(final_message)
                    except Exception:
                        pass

                    stop_reason = getattr(final_message, "stop_reason", None)

                    if stop_reason == "end_turn":
                        break

                    if stop_reason == "tool_use":
                        # Append the assistant turn verbatim so the next iteration sees it.
                        assistant_content = []
                        any_app_tool = False
                        for block in final_message.content:
                            btype = getattr(block, "type", None)
                            if btype == "thinking":
                                d = {"type": "thinking", "thinking": block.thinking}
                                if getattr(block, "signature", None):
                                    d["signature"] = block.signature
                                assistant_content.append(d)
                            elif btype == "redacted_thinking":
                                assistant_content.append({"type": "redacted_thinking", "data": getattr(block, "data", "")})
                            elif btype == "text":
                                assistant_content.append({"type": "text", "text": block.text})
                            elif btype == "tool_use":
                                any_app_tool = True
                                assistant_content.append({
                                    "type": "tool_use",
                                    "id": block.id,
                                    "name": block.name,
                                    "input": block.input,
                                })
                            elif btype == "server_tool_use":
                                # Anthropic-handled — pass through verbatim so the API
                                # accepts the conversation state on the next turn.
                                assistant_content.append({
                                    "type": "server_tool_use",
                                    "id": block.id,
                                    "name": block.name,
                                    "input": block.input,
                                })
                            elif btype in ("web_search_tool_result", "web_fetch_tool_result"):
                                assistant_content.append({
                                    "type": btype,
                                    "tool_use_id": getattr(block, "tool_use_id", ""),
                                    "content": getattr(block, "content", []),
                                })
                        messages_loop.append({"role": "assistant", "content": assistant_content})

                        if not any_app_tool:
                            # Only server tools were invoked — Anthropic already injected their
                            # results into `final_message.content`. Loop again so the model can
                            # continue reasoning over them; no app-side tool_result needed.
                            continue

                        # Execute every app-side tool_use block.
                        tool_results = []
                        for block in final_message.content:
                            if getattr(block, "type", None) == "tool_use":
                                tname = block.name
                                if tname in ANTHROPIC_SERVER_TOOL_NAMES:
                                    continue
                                log.info(f"Jarvis tool: {tname} inputs={json.dumps(block.input, default=str)}")
                                result = _execute_jarvis_tool(tname, block.input, conversation_id=conversation_id)
                                actions_taken.append({"tool": tname, "result": result})
                                tool_results.append({
                                    "type": "tool_result",
                                    "tool_use_id": block.id,
                                    "content": json.dumps(result, default=str),
                                })
                        if tool_results:
                            messages_loop.append({"role": "user", "content": tool_results})
                    else:
                        break

                # Build action metadata (same shape as the legacy JSON response).
                def _has(tool, status_key="status", status_val=None, predicate=None):
                    for a in actions_taken:
                        if a["tool"] != tool:
                            continue
                        r = a.get("result") or {}
                        if predicate is not None:
                            if predicate(r):
                                return True
                        elif status_val is not None and r.get(status_key) == status_val:
                            return True
                    return False

                action_payload = {
                    "task_created": _has("create_task", status_val="created"),
                    "task_completed": _has("complete_task", status_val="completed"),
                    "task_deleted": _has("delete_task", status_val="deleted"),
                    "stock_recorded": _has("log_stock_transaction", status_val="recorded"),
                    "stock_note_saved": _has("save_stock_note", status_val="saved"),
                    "assignment_completed": _has("complete_assignment", status_val="logged"),
                    "project_created": _has("create_project", status_val="created"),
                    "project_task_added": _has("add_project_task", status_val="added"),
                    "completed_title": next((a["result"].get("title") for a in actions_taken if a["tool"] == "complete_task"), None),
                    "deleted_title": next((a["result"].get("title") for a in actions_taken if a["tool"] == "delete_task"), None),
                    "stock_note_symbol": next((a["result"].get("symbol") for a in actions_taken if a["tool"] == "save_stock_note"), None),
                    "project_created_title": next((a["result"].get("title") for a in actions_taken if a["tool"] == "create_project"), None),
                    "project_created_task_count": next((a["result"].get("task_count") for a in actions_taken if a["tool"] == "create_project"), None),
                    "gmail_draft_pending": next(
                        (
                            {
                                "draft_id": a["result"].get("draft_id"),
                                "to": a["result"].get("to"),
                                "cc": a["result"].get("cc"),
                                "subject": a["result"].get("subject"),
                                "body": a["result"].get("body_preview"),
                            }
                            for a in actions_taken
                            if a["tool"] == "create_email_draft"
                            and (a.get("result") or {}).get("status") == "awaiting_confirmation"
                        ),
                        None,
                    ),
                }
                yield _sse_pack("action", action_payload)
                yield _sse_pack("done", {"conversation_id": conversation_id})
                _final_text_box[0] = collected_text

            except Exception as e:
                log.exception("/api/chat stream failed")
                try:
                    yield _sse_pack("error", {"message": "Stream failed: " + str(e)[:200]})
                except Exception:
                    pass
            finally:
                # Persist the assistant's final reply and lazily refresh the summary.
                try:
                    if _final_text_box[0]:
                        _chat_persist_message(conversation_id, "assistant", _final_text_box[0], user_id=chat_user_id)
                    _chat_maybe_summarize_async(conversation_id, _api_key_for_summary, user_id=chat_user_id)
                    # Extract and store long-term memories from this exchange
                    _user_text_for_mem0 = ""
                    if latest_user:
                        _uv = latest_user.get("content", "")
                        if isinstance(_uv, list):
                            _uv = " ".join(b.get("text", "") for b in _uv if isinstance(b, dict) and b.get("type") == "text")
                        _user_text_for_mem0 = str(_uv)
                    _mem0_maybe_store_async(_user_text_for_mem0, _final_text_box[0])
                except Exception:
                    pass

        return Response(
            stream_with_context(_stream_generator()),
            mimetype="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",  # Disable proxy buffering (nginx)
                "Connection": "keep-alive",
            },
        )
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


def _generate_daily_plan_for_date(target_date):
    """Core plan generation logic. Returns dict {plan_id, items_count} or raises."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    today = target_date  # alias so the body below reads naturally

    with _plan_lock:
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY not configured")

        # ── Phase 1: fetch all DB data then release the connection ──────
        completed_titles = set()
        custom_estimates = {}
        tasks_for_plan = []

        conn = get_db()
        try:
            cur = conn.cursor()
            try:
                cur.execute("SELECT DISTINCT assignment_title FROM completions")
                completed_titles = set(r["assignment_title"] for r in cur.fetchall())
                cur.execute("SELECT uid, minutes FROM assignment_estimates")
                custom_estimates = {r["uid"]: r["minutes"] for r in cur.fetchall()}

                urgency_mins = {"critical": 45, "high": 30, "medium": 20, "low": 15}
                cur.execute("""
                    SELECT id, title, due_date, urgency FROM tasks
                    WHERE completed = FALSE
                    AND (due_date IS NULL OR due_date <= %s)
                    ORDER BY urgency DESC, due_date ASC
                    LIMIT 15
                """, (today + timedelta(days=3),))
                for task_row in cur.fetchall():
                    due_date = task_row.get("due_date")
                    urgency = task_row.get("urgency", "medium")
                    tasks_for_plan.append({
                        "type": "task",
                        "id": str(task_row["id"]),
                        "title": task_row["title"],
                        "due_date": str(due_date) if due_date else None,
                        "urgency": urgency,
                        "estimated_minutes": urgency_mins.get(urgency, 20)
                    })
            finally:
                cur.close()
        finally:
            conn.close()
        # ── DB connection released — safe to call Claude now ─────────────

        # Fetch assignments, tasks, and calendar events
        assignments = []
        tasks = tasks_for_plan
        calendar_events = []
        school_assignments = []

        try:
            cal = fetch_ical(u_canvas_ical())
            if cal:
                all_asgn = get_canvas_assignments_with_overdue(cal)
                class_names = {a["class_name"] for a in all_asgn if a.get("class_name")}
                class_avg_cache = get_class_averages_batch(class_names)

                today_school_hours = get_school_hours(today)
                school_start_dt = school_end_dt = None
                if today_school_hours:
                    sh, sm, eh, em = today_school_hours
                    day_start = datetime(today.year, today.month, today.day, 0, 0, 0, tzinfo=TZ)
                    school_start_dt = day_start.replace(hour=sh, minute=sm)
                    school_end_dt = day_start.replace(hour=eh, minute=em)

                for a in all_asgn:
                    if a["title"] in completed_titles:
                        continue
                    due_iso = a.get("due_iso", "")
                    if due_iso:
                        try:
                            due_dt = datetime.fromisoformat(due_iso.replace("Z", "+00:00"))
                            due_local = due_dt.astimezone(TZ)
                            due_date = due_local.date()
                            if due_date == today:
                                uid = a.get("uid", "")
                                if uid in custom_estimates:
                                    est_mins = custom_estimates[uid]
                                else:
                                    est_mins = estimate_assignment(a.get("title", ""), a.get("class_name", ""), class_avg_cache=class_avg_cache)
                                item = {
                                    "type": "assignment",
                                    "id": uid,
                                    "title": a.get("title", ""),
                                    "class": a.get("class_name", ""),
                                    "estimated_minutes": int(est_mins)
                                }
                                in_school = (
                                    school_start_dt is not None
                                    and school_start_dt <= due_local <= school_end_dt
                                )
                                if in_school:
                                    school_assignments.append(item)
                                else:
                                    assignments.append(item)
                        except Exception:
                            pass
        except Exception as e:
            log.warning(f"Could not fetch assignments for plan: {e}")

        # Fetch personal, sports, and job calendar events for target date
        personal_events = []
        sports_events = []
        job_events = []
        try:
            personal_cal = fetch_ical(u_personal_ical())
            if personal_cal:
                personal_events = parse_calendar_events(personal_cal, days_ahead=2)
        except Exception as e:
            log.warning(f"Could not fetch personal calendar for plan: {e}")
        try:
            sports_cal = fetch_ical(u_sports_ical())
            if sports_cal:
                sports_events = parse_calendar_events(sports_cal, days_ahead=2)
        except Exception as e:
            log.warning(f"Could not fetch sports calendar for plan: {e}")
        if u_job_schedule_ical():
            try:
                job_cal = fetch_ical(u_job_schedule_ical())
                if job_cal:
                    job_events = parse_calendar_events(job_cal, days_ahead=2)
            except Exception as e:
                log.warning(f"Could not fetch job calendar for plan: {e}")

        date_iso = today.isoformat()
        for event in personal_events:
            if event["date"] == date_iso:
                calendar_events.append({
                    "type": "calendar", "id": "", "title": event["title"],
                    "start_display": "All Day" if event.get("all_day") else event.get("start_display", ""),
                    "end_display": "" if event.get("all_day") else event.get("end_display", ""),
                    "all_day": event.get("all_day", False), "source": "personal"
                })
        for event in sports_events:
            if event["date"] == date_iso:
                calendar_events.append({
                    "type": "calendar", "id": "", "title": event["title"] + " [SPORTS]",
                    "start_display": "All Day" if event.get("all_day") else event.get("start_display", ""),
                    "end_display": "" if event.get("all_day") else event.get("end_display", ""),
                    "all_day": event.get("all_day", False), "source": "sports"
                })
        for event in job_events:
            if event["date"] == date_iso:
                calendar_events.append({
                    "type": "calendar", "id": "", "title": event["title"] + " [WORK]",
                    "start_display": "All Day" if event.get("all_day") else event.get("start_display", ""),
                    "end_display": "" if event.get("all_day") else event.get("end_display", ""),
                    "all_day": event.get("all_day", False), "source": "job"
                })

        # Compute free windows
        free_windows = []
        try:
            # For today use current time as cursor; for future dates start at 7 AM
            _actual_now = datetime.now(TZ)
            if target_date == _actual_now.date():
                now_local = _actual_now
            else:
                now_local = datetime(target_date.year, target_date.month, target_date.day, 7, 0, 0, tzinfo=TZ)

            dtype = get_day_type(today)
            school_hours = get_school_hours(today)

            busy = []
            if school_hours:
                sh, sm, eh, em = school_hours
                school_start_str = "%d:%02d %s" % (sh % 12 or 12, sm, "AM" if sh < 12 else "PM")
                school_end_str = "%d:%02d %s" % (eh % 12 or 12, em, "AM" if eh < 12 else "PM")
                school_title = "School (%s day)" % dtype.title()
                if school_assignments:
                    turn_in_list = ", ".join(
                        "%s [%s]" % (sa["title"], sa["class"]) if sa.get("class") else sa["title"]
                        for sa in school_assignments
                    )
                    school_title += " — due during school: " + turn_in_list
                calendar_events.insert(0, {
                    "type": "calendar", "id": "school", "title": school_title,
                    "start_display": school_start_str, "end_display": school_end_str,
                    "source": "school", "school_assignments": school_assignments,
                })
                busy.append({
                    "start": now_local.replace(hour=sh, minute=sm, second=0, microsecond=0),
                    "end": now_local.replace(hour=eh, minute=em, second=0, microsecond=0),
                })

            for e in personal_events + sports_events + job_events:
                if e["date"] == date_iso and not e.get("all_day") and e.get("start_iso"):
                    try:
                        es = datetime.fromisoformat(e["start_iso"])
                        ee = datetime.fromisoformat(e.get("end_iso") or e["start_iso"])
                        if es.tzinfo is None:
                            es = es.replace(tzinfo=TZ)
                        if ee.tzinfo is None:
                            ee = ee.replace(tzinfo=TZ)
                        busy.append({"start": es, "end": ee})
                    except Exception:
                        pass

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

        # ── Phase 2: Claude API call ────────────────────────────────────
        client = anthropic.Anthropic(api_key=api_key, max_retries=3, timeout=60.0)

        def _fmt_cal(e):
            if e.get("all_day"):
                return "- %s: All Day" % e["title"]
            return "- %s: %s – %s" % (e["title"], e.get("start_display", "?"), e.get("end_display", "?"))

        cal_block_lines = ("\n".join(_fmt_cal(e) for e in calendar_events)
            or "None (no school or calendar events today)")
        free_window_lines = "\n".join(
            "- %s – %s (%d min)" % (w["start"], w["end"], w["minutes"])
            for w in free_windows
        ) or "No free windows found — check calendar configuration."
        in_school_block_lines = "\n".join(
            "- %s [%s] (~%d min)" % (sa["title"], sa.get("class", ""), sa.get("estimated_minutes", 0))
            for sa in school_assignments
        ) or "None"

        schedule_prompt = f"""You are Jarvis, sir's exceptionally capable AI, building the complete daily schedule for {today}.

STEP 1 — FIXED CALENDAR BLOCKS (immovable — never schedule anything during these):
{cal_block_lines}

STEP 2 — FREE WINDOWS available for work and rest (gaps between the fixed blocks above):
{free_window_lines}

STEP 3 — ASSIGNMENTS DUE DURING SCHOOL HOURS (already handled inside the School block — do NOT create separate work sessions for these; they stay attached to the School calendar block):
{in_school_block_lines}

STEP 4 — WORK TO SCHEDULE into the free windows (assignments due AFTER school hours today):
Assignments to schedule (must all be included):
{json.dumps(assignments, indent=2) if assignments else "None"}

Tasks (schedule by urgency and due date, estimate realistic durations from the title):
{json.dumps(tasks, indent=2) if tasks else "None"}

STEP 5 — Fill remaining gaps with explicit free time / break blocks.

Return a JSON array covering the FULL day in chronological order. Include every block: fixed calendar events, work sessions, AND free time. Each item:
- item_type: "calendar", "assignment", "task", or "free_time"
- item_id: original ID (use "" for free_time blocks)
- item_title: descriptive title (e.g. "Free Time", "Break", or the event/task name)
- scheduled_start_time: "HH:MM" 24-hour
- scheduled_end_time: "HH:MM" 24-hour

Rules:
1. All assignments from STEP 4 MUST appear as their own scheduled blocks
2. Assignments from STEP 3 must NOT appear as separate blocks — they are already subsumed under the School calendar block
3. Never schedule work inside a fixed calendar block
4. Prioritize: assignments > critical/high tasks > medium/low tasks
5. Use realistic time estimates based on the task title, not just urgency
6. Leave breathing room — do not pack every minute with work
7. Lunch breaks: {"On Fridays, add a 30-45 min lunch break immediately after school ends." if today.weekday() == 4 else "Do NOT add a lunch break — lunch happens at school on regular school days."}
8. MORNING PERSON — High-urgency and high-focus tasks MUST be placed in the 7:00 AM–11:00 AM window whenever free time is available there. Lower-priority tasks and free time belong later in the day.
9. Return ONLY a valid JSON array, no markdown or explanation."""

        try:
            message = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=2048,
                messages=[{"role": "user", "content": schedule_prompt}]
            )
            track_api_usage(message)
            response_text = message.content[0].text if message.content else "[]"
            response_text = response_text.strip()
            if response_text.startswith("```") and response_text.endswith("```"):
                response_text = response_text[3:-3].strip()
                if response_text.startswith("json"):
                    response_text = response_text[4:].strip()
            scheduled_items = json.loads(response_text)
        except Exception as e:
            log.warning(f"Claude plan generation failed: {e}")
            scheduled_items = []
            start_min = 15 * 60
            if free_windows:
                try:
                    from datetime import datetime as _dt
                    t = _dt.strptime(free_windows[0]["start"], "%I:%M %p")
                    start_min = t.hour * 60 + t.minute
                except Exception:
                    pass
            cursor_min = start_min
            for item in assignments + tasks:
                estimated_mins = item.get("estimated_minutes", 30)
                end_min = cursor_min + estimated_mins
                scheduled_items.append({
                    "item_type": item["type"],
                    "item_id": item["id"],
                    "item_title": item["title"],
                    "scheduled_start_time": f"{cursor_min // 60:02d}:{cursor_min % 60:02d}",
                    "scheduled_end_time": f"{end_min // 60:02d}:{end_min % 60:02d}"
                })
                cursor_min = end_min

        # ── Phase 3: write results ───────────────────────────────────────
        conn = get_db()
        try:
            cur = conn.cursor()
            try:
                cur.execute("DELETE FROM daily_plans WHERE plan_date = %s", (today,))
                cur.execute(
                    "INSERT INTO daily_plans (plan_date, generated_at) VALUES (%s, NOW()) RETURNING id",
                    (today,)
                )
                plan_id = cur.fetchone()["id"]
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
            except Exception:
                conn.rollback()
                raise
            finally:
                cur.close()
        finally:
            conn.close()

        return {"plan_id": plan_id, "items_count": len(scheduled_items)}


def auto_generate_plan_job():
    """Nightly scheduled job: generate tomorrow's daily plan."""
    tomorrow = (datetime.now(TZ) + timedelta(days=1)).date()
    try:
        result = _generate_daily_plan_for_date(tomorrow)
        log.info("auto_generate_plan_job: generated plan for %s (%d items)", tomorrow, result["items_count"])
    except Exception as e:
        log.error("auto_generate_plan_job error: %s", e)


@app.route("/api/plan-my-day/generate", methods=["POST"])
def api_plan_my_day_generate():
    """Generate a new daily plan using AI."""
    try:
        result = _generate_daily_plan_for_date(datetime.now(TZ).date())
        return jsonify({"status": "ok", "plan_id": result["plan_id"], "items_count": result["items_count"]})
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


@app.route("/api/plan-my-day/items/<int:item_id>", methods=["DELETE"])
def api_plan_my_day_item_delete(item_id):
    """Delete a single plan item."""
    try:
        with _plan_lock:
            conn = get_db()
            cur = conn.cursor()
            cur.execute("DELETE FROM daily_plan_items WHERE id = %s", (item_id,))
            conn.commit()
            cur.close()
            conn.close()
            return jsonify({"status": "ok"})
    except Exception as e:
        log.exception("Error deleting plan item")
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
# DAILY OUTLOOK + STOCKS
# ──────────────────────────────────────────────────────────────────────────────

@app.route("/api/stocks/transaction", methods=["POST"])
def api_stocks_transaction():
    data = request.get_json(force=True) or {}
    try:
        symbol = str(data.get("symbol", "")).strip().upper()[:16]
        action = str(data.get("action", "")).strip().lower()
        qty = float(data.get("quantity", 0) or 0)
        price = float(data.get("price", 0) or 0)
        tx_date = data.get("transaction_date") or datetime.now(TZ).date().isoformat()
        notes = str(data.get("notes", ""))[:500]
        if not symbol or action not in ("buy", "sell") or qty <= 0 or price <= 0:
            return jsonify({"error": "Invalid transaction data"}), 400
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO stock_transactions (symbol, action, quantity, price, transaction_date, notes) "
            "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
            (symbol, action, qty, price, tx_date, notes)
        )
        new_id = cur.fetchone()["id"]
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({"status": "ok", "id": new_id})
    except Exception as e:
        log.exception("stock transaction insert failed")
        return jsonify({"error": str(e)}), 500


@app.route("/api/stocks/transactions", methods=["GET"])
def api_stocks_transactions_list():
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            "SELECT id, symbol, action, quantity, price, transaction_date, notes, created_at "
            "FROM stock_transactions ORDER BY transaction_date DESC, id DESC LIMIT 100"
        )
        rows = [{
            "id": r["id"],
            "symbol": r["symbol"],
            "action": r["action"],
            "quantity": float(r["quantity"]),
            "price": float(r["price"]),
            "transaction_date": r["transaction_date"].isoformat() if r["transaction_date"] else None,
            "notes": r["notes"] or "",
        } for r in cur.fetchall()]
        cur.close()
        conn.close()
        return jsonify({"transactions": rows})
    except Exception as e:
        log.exception("stock transaction list failed")
        return jsonify({"error": str(e)}), 500


@app.route("/api/stocks/transaction/<int:tx_id>", methods=["DELETE"])
def api_stocks_transaction_delete(tx_id):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("DELETE FROM stock_transactions WHERE id = %s", (tx_id,))
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/stocks/portfolio", methods=["GET"])
def api_stocks_portfolio():
    try:
        return jsonify(build_portfolio_snapshot())
    except Exception as e:
        log.exception("portfolio fetch failed")
        return jsonify({"error": str(e)}), 500


@app.route("/api/stocks/history", methods=["GET"])
def api_stocks_history():
    symbol = request.args.get("symbol", "").strip().upper()
    range_key = request.args.get("range", "1mo").strip()
    if not symbol:
        return jsonify({"error": "symbol required"}), 400
    return jsonify({"symbol": symbol, "range": range_key, "points": fetch_stock_history(symbol, range_key)})


@app.route("/api/stocks/notes", methods=["GET"])
def api_stocks_notes_list():
    try:
        return jsonify({"notes": get_all_stock_notes()})
    except Exception as e:
        log.exception("stock notes list failed")
        return jsonify({"error": str(e)}), 500


@app.route("/api/stocks/notes", methods=["POST"])
def api_stocks_notes_upsert():
    data = request.get_json(force=True) or {}
    symbol = str(data.get("symbol", "")).strip().upper()[:16]
    if not symbol:
        return jsonify({"error": "symbol required"}), 400
    thesis = data.get("thesis")
    exit_criteria = data.get("exit_criteria")

    def _num(v):
        if v is None or v == "":
            return None
        try:
            return float(v)
        except Exception:
            return None

    target_price = _num(data.get("target_price"))
    stop_loss = _num(data.get("stop_loss"))
    try:
        result = upsert_stock_note(
            symbol,
            thesis=(str(thesis) if thesis is not None else None),
            exit_criteria=(str(exit_criteria) if exit_criteria is not None else None),
            target_price=target_price,
            stop_loss=stop_loss,
        )
        return jsonify({"status": "ok", "note": result})
    except Exception as e:
        log.exception("stock notes upsert failed")
        return jsonify({"error": str(e)}), 500


@app.route("/api/stocks/notes/<symbol>", methods=["DELETE"])
def api_stocks_notes_delete(symbol):
    sym = (symbol or "").strip().upper()[:16]
    if not sym:
        return jsonify({"error": "symbol required"}), 400
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("DELETE FROM stock_notes WHERE symbol=%s", (sym,))
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/stocks/research", methods=["POST"])
def api_stocks_research():
    """Claude-written research brief for a symbol, augmented with Finnhub data."""
    data = request.get_json(force=True) or {}
    symbol = str(data.get("symbol", "")).strip().upper()[:16]
    if not symbol:
        return jsonify({"error": "symbol required"}), 400
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return jsonify({"error": "ANTHROPIC_API_KEY not configured"}), 500

    profile = fetch_finnhub_profile(symbol)
    quote = fetch_finnhub_quote(symbol)
    reco = fetch_finnhub_recommendation(symbol)
    news = fetch_finnhub_company_news(symbol, days_back=14)

    # Existing note (if any) so the research addresses the student's thesis
    existing_note = None
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT thesis, exit_criteria, target_price, stop_loss FROM stock_notes WHERE symbol=%s", (symbol,))
        row = cur.fetchone()
        if row:
            existing_note = {
                "thesis": row["thesis"] or "",
                "exit_criteria": row["exit_criteria"] or "",
                "target_price": float(row["target_price"]) if row["target_price"] is not None else None,
                "stop_loss": float(row["stop_loss"]) if row["stop_loss"] is not None else None,
            }
        cur.close()
        conn.close()
    except Exception:
        pass

    # Current position info
    position = None
    try:
        port = _compute_portfolio()
        if symbol in port:
            position = port[symbol]
    except Exception:
        pass

    # Assemble context block for Claude
    ctx_lines = [f"Symbol: {symbol}"]
    if profile:
        ctx_lines.append(f"Company: {profile.get('name','')} ({profile.get('industry','')}) · {profile.get('country','')}")
        if profile.get("market_cap"):
            ctx_lines.append(f"Market cap (USD M): {profile['market_cap']}")
        if profile.get("exchange"):
            ctx_lines.append(f"Exchange: {profile['exchange']}")
    else:
        ctx_lines.append("Profile data unavailable (no FINNHUB_API_KEY or unknown symbol).")
    if quote:
        ctx_lines.append(f"Quote: ${quote['price']} (prev close ${quote['prev_close']}, day {quote['day_change_pct']:+.2f}%)")
    if reco:
        ctx_lines.append(
            f"Analyst reco ({reco['period']}): strong_buy {reco['strong_buy']}, buy {reco['buy']}, "
            f"hold {reco['hold']}, sell {reco['sell']}, strong_sell {reco['strong_sell']}"
        )
    if position:
        ctx_lines.append(
            f"Student position: {position['qty']} shares @ avg cost ${position['avg_cost']} (total cost ${position['total_cost']})"
        )
    if existing_note:
        if existing_note["thesis"]:
            ctx_lines.append(f"Student's buy thesis on file: {existing_note['thesis']}")
        if existing_note["exit_criteria"]:
            ctx_lines.append(f"Student's exit criteria on file: {existing_note['exit_criteria']}")
        if existing_note["target_price"]:
            ctx_lines.append(f"Target price: ${existing_note['target_price']}")
        if existing_note["stop_loss"]:
            ctx_lines.append(f"Stop loss: ${existing_note['stop_loss']}")
    if news:
        news_lines = ["Recent headlines:"]
        for n in news[:6]:
            src = n.get("source") or ""
            news_lines.append(f"- [{src}] {n['headline']}")
        ctx_lines.append("\n".join(news_lines))

    prompt = (
        "You are Jarvis — composed British AI majordomo. The student has asked for a research brief. "
        "Use the factual context below as your primary grounding; supplement with your training knowledge when helpful, "
        "but flag uncertainty plainly. Never fabricate specific numbers not in the context.\n\n"
        "CONTEXT:\n" + "\n".join(ctx_lines) + "\n\n"
        "Compose a concise markdown research brief with these sections (exact ## headings):\n"
        "## Snapshot — 1-2 sentences: what the company does, current price/recent move.\n"
        "## Bull Case — 3-4 bullets of reasons to own it.\n"
        "## Bear Case — 3-4 bullets of risks / reasons for caution.\n"
        "## Catalysts to Watch — earnings, product cycles, macro events that could move the stock.\n"
        "## Suggested Exit Triggers — specific, measurable conditions under which it would be prudent to sell or trim "
        "(price levels, fundamentals deteriorating, thesis breakage). If the student already has an exit note on file, reference and refine it.\n"
        "## Verdict — a clear final take, in character (measured, no theatrics). If the student already has a thesis on file, weigh it against current evidence.\n\n"
        "Keep the whole brief under 450 words. Use **bold** for key terms. No intro line before the first heading."
    )

    try:
        client = anthropic.Anthropic(api_key=api_key, max_retries=3, timeout=60.0)
        message = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=900,
            messages=[{"role": "user", "content": prompt}]
        )
        track_api_usage(message)
        brief = message.content[0].text if message.content else ""
        return jsonify({
            "symbol": symbol,
            "brief": brief,
            "profile": profile,
            "quote": quote,
            "recommendation": reco,
            "news": news[:5],
            "position": position,
            "note": existing_note,
        })
    except Exception as e:
        log.exception("stock research failed")
        return jsonify({"error": str(e)}), 500


@app.route("/api/news/rate", methods=["POST"])
def api_news_rate():
    """Record a like (1) or dislike (-1) for a news story."""
    data = request.get_json(force=True) or {}
    try:
        rating = int(data.get("rating", 0))
    except Exception:
        rating = 0
    if rating not in (1, -1):
        return jsonify({"error": "rating must be 1 or -1"}), 400
    url = str(data.get("url", "")).strip()[:1000]
    title = str(data.get("title", "")).strip()[:500]
    outlet = str(data.get("outlet", "")).strip()[:100]
    summary = str(data.get("summary", ""))
    if not (url or title):
        return jsonify({"error": "url or title required"}), 400
    url_hash = hashlib.sha256((url or title).encode("utf-8")).hexdigest()[:32]
    keywords = _extract_keywords(title + " " + summary)
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO news_preferences (url_hash, url, title, outlet, rating, keywords) "
            "VALUES (%s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (url_hash) DO UPDATE SET rating=EXCLUDED.rating, created_at=NOW()",
            (url_hash, url, title, outlet, rating, " ".join(keywords))
        )
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({"status": "ok"})
    except Exception as e:
        log.exception("news rate failed")
        return jsonify({"error": str(e)}), 500


@app.route("/api/news/preferences", methods=["GET"])
def api_news_preferences():
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            "SELECT outlet, SUM(CASE WHEN rating=1 THEN 1 ELSE 0 END) AS likes, "
            "SUM(CASE WHEN rating=-1 THEN 1 ELSE 0 END) AS dislikes "
            "FROM news_preferences WHERE outlet <> '' GROUP BY outlet ORDER BY likes DESC, dislikes ASC"
        )
        outlets = [{
            "outlet": r["outlet"],
            "likes": int(r["likes"] or 0),
            "dislikes": int(r["dislikes"] or 0),
        } for r in cur.fetchall()]
        cur.execute(
            "SELECT rating, COUNT(*) AS n FROM news_preferences GROUP BY rating"
        )
        totals = {"likes": 0, "dislikes": 0}
        for r in cur.fetchall():
            if int(r["rating"]) == 1:
                totals["likes"] = int(r["n"])
            elif int(r["rating"]) == -1:
                totals["dislikes"] = int(r["n"])
        cur.close()
        conn.close()
        return jsonify({"outlets": outlets, "totals": totals})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/daily-outlook", methods=["POST"])
def api_daily_outlook():
    """Assemble the Morning Outlook payload in one call."""
    import concurrent.futures
    today = datetime.now(TZ).date()
    today_iso = today.isoformat()

    # Resolve user-scoped calendar URLs in the request thread; the worker
    # threads below cannot access Flask's session-bound LocalProxy. Without
    # this, u_*_ical() would silently fall back to env vars and the user's
    # saved settings calendars would be ignored.
    canvas_url   = u_canvas_ical()
    personal_url = u_personal_ical()
    sports_url   = u_sports_ical()
    job_url      = u_job_schedule_ical()

    def _get_assignments():
        try:
            cal = fetch_ical(canvas_url)
            if not cal:
                return []
            conn = get_db()
            cur = conn.cursor()
            cur.execute("SELECT DISTINCT assignment_title FROM completions")
            done = set(r["assignment_title"] for r in cur.fetchall())
            cur.close()
            conn.close()
            out = []
            for a in get_canvas_assignments_with_overdue(cal):
                if a["title"] in done:
                    continue
                due_iso = a.get("due_iso", "")
                if not due_iso:
                    continue
                try:
                    d = datetime.fromisoformat(due_iso.replace("Z", "+00:00")).astimezone(TZ).date()
                except Exception:
                    continue
                if d == today:
                    out.append({
                        "title": a.get("title", ""),
                        "class_name": a.get("class_name", ""),
                        "due_display": a.get("due_display", ""),
                        "due_iso": a.get("due_iso", ""),
                    })
            return out
        except Exception as e:
            log.warning("outlook: assignments failed: %s", e)
            return []

    def _get_events():
        try:
            out = []
            for url, tag in ((personal_url, "personal"), (sports_url, "sports"), (job_url, "job")):
                c = fetch_ical(url)
                if not c:
                    continue
                for e in parse_calendar_events(c, days_ahead=1):
                    if e.get("date") == today_iso:
                        out.append({
                            "title": e.get("title", ""),
                            "start_display": "All Day" if e.get("all_day") else e.get("start_display", ""),
                            "end_display": "" if e.get("all_day") else e.get("end_display", ""),
                            "all_day": e.get("all_day", False),
                            "source": tag,
                        })
            return out
        except Exception as e:
            log.warning("outlook: events failed: %s", e)
            return []

    def _get_tasks():
        try:
            conn = get_db()
            cur = conn.cursor()
            cur.execute(
                "SELECT id, title, urgency, due_date, notes FROM tasks "
                "WHERE completed = FALSE AND (due_date IS NULL OR due_date <= %s) "
                "ORDER BY urgency DESC, due_date ASC NULLS LAST LIMIT 10",
                (today,)
            )
            rows = cur.fetchall()
            cur.close()
            conn.close()
            return [{
                "id": r["id"],
                "title": r["title"],
                "urgency": r["urgency"],
                "due_date": r["due_date"].isoformat() if r["due_date"] else None,
                "notes": (r["notes"] or "")[:300],
            } for r in rows]
        except Exception as e:
            log.warning("outlook: tasks failed: %s", e)
            return []

    def _get_stocks():
        try:
            return build_portfolio_snapshot()
        except Exception as e:
            log.warning("outlook: stocks failed: %s", e)
            return None

    def _get_weather():
        try:
            return fetch_weather()
        except Exception as e:
            log.warning("outlook: weather failed: %s", e)
            return None

    def _get_quote():
        try:
            return fetch_quote_of_day()
        except Exception as e:
            log.warning("outlook: quote failed: %s", e)
            return None

    def _get_news_national():
        try:
            return fetch_news("national", limit=3)
        except Exception as e:
            log.warning("outlook: news national failed: %s", e)
            return []

    def _get_news_local():
        try:
            return fetch_news("local", limit=3)
        except Exception as e:
            log.warning("outlook: news local failed: %s", e)
            return []

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        f_assignments = ex.submit(_get_assignments)
        f_events = ex.submit(_get_events)
        f_tasks = ex.submit(_get_tasks)
        f_stocks = ex.submit(_get_stocks)
        f_weather = ex.submit(_get_weather)
        f_quote = ex.submit(_get_quote)
        f_news_nat = ex.submit(_get_news_national)
        f_news_loc = ex.submit(_get_news_local)

    return jsonify({
        "generated_at": datetime.now(TZ).isoformat(),
        "assignments_events": {
            "assignments": f_assignments.result(),
            "events": f_events.result(),
        },
        "tasks": f_tasks.result(),
        "stocks": f_stocks.result(),
        "weather": f_weather.result(),
        "quote": f_quote.result(),
        "news": {
            "national": f_news_nat.result(),
            "local": f_news_loc.result(),
        },
    })


@app.route("/api/outlook/news-detail", methods=["POST"])
def api_outlook_news_detail():
    """Claude synthesis of 'other angles / how other outlets are framing this'."""
    data = request.get_json(force=True) or {}
    title = str(data.get("title", "")).strip()
    outlet = str(data.get("outlet", "")).strip()
    url = str(data.get("url", "")).strip()
    summary = str(data.get("summary", "")).strip()
    if not title:
        return jsonify({"error": "title required"}), 400

    url_hash = hashlib.sha256((url or title).encode("utf-8")).hexdigest()[:32]

    # Check cache
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT synthesis, generated_at FROM outlook_news_cache WHERE url_hash = %s", (url_hash,))
        row = cur.fetchone()
        if row and row["generated_at"]:
            age = datetime.now(TZ) - row["generated_at"].astimezone(TZ)
            if age.total_seconds() < 6 * 3600:
                cur.close()
                conn.close()
                return jsonify({"synthesis": row["synthesis"], "cached": True})
        cur.close()
        conn.close()
    except Exception:
        pass

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return jsonify({"synthesis": "_(Claude synthesis unavailable — API key not configured.)_"}), 200

    try:
        client = anthropic.Anthropic(api_key=api_key, max_retries=3, timeout=60.0)
        prompt = (
            "A news story was just surfaced to the student in their Morning Outlook:\n\n"
            f"Headline: {title}\n"
            f"Outlet: {outlet}\n"
            f"URL: {url}\n"
            f"Summary: {summary}\n\n"
            "Compose a concise synthesis titled '## Other Angles' that covers:\n"
            "- Broader context the summary omits (1-2 bullets)\n"
            "- How other major outlets across the spectrum are likely framing or covering the same story, naming specific outlets where possible\n"
            "- Any factual caveats or ongoing developments worth noting\n\n"
            "Stay factual, name outlets explicitly, flag uncertainty plainly, and avoid editorialising. Keep under 200 words. Use markdown (## header, - bullets, **bold** for outlet names)."
        )
        message = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}]
        )
        track_api_usage(message)
        synthesis = message.content[0].text if message.content else ""
        if synthesis:
            try:
                conn = get_db()
                cur = conn.cursor()
                cur.execute(
                    "INSERT INTO outlook_news_cache (url_hash, synthesis) VALUES (%s, %s) "
                    "ON CONFLICT (url_hash) DO UPDATE SET synthesis = EXCLUDED.synthesis, generated_at = NOW()",
                    (url_hash, synthesis)
                )
                conn.commit()
                cur.close()
                conn.close()
            except Exception as ce:
                log.warning("news-detail cache insert failed: %s", ce)
        return jsonify({"synthesis": synthesis, "cached": False})
    except Exception as e:
        log.exception("news-detail synthesis failed")
        return jsonify({"error": str(e)}), 500


# Boot-time side effects: DB init, env-key seeding, and the background scheduler.
# Set FLASK_SKIP_BOOT=1 to skip all of these (tests, import-only tooling).
_SKIP_BOOT = os.environ.get("FLASK_SKIP_BOOT") == "1"

# Initialize database if available
if not _SKIP_BOOT:
    try:
        init_db()
        log.info("Database initialized successfully")
    except Exception as e:
        log.warning(f"Database initialization failed: {e}. Running in limited mode.")

# Seed API key from env var into DB so it persists across deploys
if not _SKIP_BOOT:
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
    if not _SKIP_BOOT and _worker_id in ("", "0", "1"):
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

# ── Google OAuth2 routes ──────────────────────────────────────────────────────

@app.route("/google-auth/start")
def google_auth_start():
    if not session.get("authenticated"):
        return redirect("/login")
    if not _google_configured():
        return "Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET environment variables first.", 400
    try:
        from google_auth_oauthlib.flow import Flow
        redirect_uri = GOOGLE_REDIRECT_URI or request.url_root.rstrip("/") + "/google-auth/callback"
        cfg = _google_client_config()
        cfg["web"]["redirect_uris"] = [redirect_uri]
        flow = Flow.from_client_config(cfg, scopes=GOOGLE_SCOPES)
        flow.redirect_uri = redirect_uri
        auth_url, state = flow.authorization_url(
            access_type="offline",
            prompt="consent",
        )
        session["google_oauth_state"] = state
        set_config({"google_oauth_pending_state": state})
        return redirect(auth_url)
    except Exception as e:
        log.error("google_auth_start error: %s", e)
        return f"Error starting Google auth: {e}", 500


@app.route("/google-auth/callback")
def google_auth_callback():
    if not session.get("authenticated"):
        return redirect("/login")
    incoming_state = request.args.get("state")
    state = session.pop("google_oauth_state", None) or get_config().get("google_oauth_pending_state", "")
    set_config({"google_oauth_pending_state": ""})
    if not state or state != incoming_state:
        return "OAuth state mismatch — please try the connection again from /google-auth/start.", 400
    try:
        from google_auth_oauthlib.flow import Flow
        redirect_uri = GOOGLE_REDIRECT_URI or request.url_root.rstrip("/") + "/google-auth/callback"
        cfg = _google_client_config()
        cfg["web"]["redirect_uris"] = [redirect_uri]
        flow = Flow.from_client_config(cfg, scopes=GOOGLE_SCOPES, state=state)
        flow.redirect_uri = redirect_uri
        # Reconstruct the full authorization response URL using the correct scheme
        auth_response = request.url
        if auth_response.startswith("http://") and redirect_uri.startswith("https://"):
            auth_response = "https://" + auth_response[len("http://"):]
        # Allow Google to return a different scope set (e.g. previously-granted
        # scopes like gmail.send, or restricted scopes not granted in test mode)
        # without raising a hard error.
        import os as _os
        _os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")
        flow.fetch_token(authorization_response=auth_response)
        creds = flow.credentials
        if creds.refresh_token:
            set_config({"google_refresh_token": creds.refresh_token})
            log.info("Google refresh token stored successfully")
        else:
            log.error("Google OAuth callback: no refresh_token returned — authorization incomplete")
            return redirect("/?google_error=no_refresh_token")
        return redirect("/?google_connected=1")
    except Exception as e:
        log.error("google_auth_callback error: %s", e)
        return f"Google OAuth error: {e}", 500


@app.route("/api/google/status")
def google_auth_status():
    if not session.get("authenticated"):
        return jsonify({"error": "Not authenticated"}), 401
    configured = _google_configured()
    has_token = bool(configured and get_config().get("google_refresh_token", "").strip())
    refresh_error = None
    authorized = False
    if has_token:
        try:
            from google.oauth2.credentials import Credentials
            from google.auth.transport.requests import Request as GoogleRequest
            refresh_token = get_config().get("google_refresh_token", "").strip()
            creds = Credentials(
                token=None,
                refresh_token=refresh_token,
                client_id=GOOGLE_CLIENT_ID,
                client_secret=GOOGLE_CLIENT_SECRET,
                token_uri="https://oauth2.googleapis.com/token",
            )
            creds.refresh(GoogleRequest())
            authorized = True
        except Exception as e:
            refresh_error = str(e)
    return jsonify({
        "configured": configured,
        "has_token": has_token,
        "authorized": authorized,
        "refresh_error": refresh_error,
        "auth_url": "/google-auth/start" if configured and not authorized else None,
    })


@app.route("/api/google/disconnect", methods=["POST"])
def google_disconnect():
    if not session.get("authenticated"):
        return jsonify({"error": "Not authenticated"}), 401
    set_config({"google_refresh_token": ""})
    return jsonify({"status": "disconnected"})


@app.route("/api/google/gmail/drafts")
def gmail_list_drafts():
    if not session.get("authenticated"):
        return jsonify({"error": "Not authenticated"}), 401
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, to_addr, cc_addr, subject, body, created_at FROM gmail_drafts "
        "WHERE status='pending' ORDER BY created_at DESC"
    )
    rows = [dict(r) for r in cur.fetchall()]
    cur.close(); conn.close()
    for r in rows:
        if r.get("created_at") and hasattr(r["created_at"], "isoformat"):
            r["created_at"] = r["created_at"].isoformat()
    return jsonify({"drafts": rows})


@app.route("/api/google/gmail/drafts/<int:draft_id>/send", methods=["POST"])
def gmail_send_draft(draft_id):
    if not session.get("authenticated"):
        return jsonify({"error": "Not authenticated"}), 401
    creds = _get_google_credentials()
    if not creds:
        return jsonify({"error": "Google not authorized. Visit /google-auth/start."}), 400
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "SELECT to_addr, cc_addr, subject, body, conversation_id FROM gmail_drafts WHERE id=%s AND status='pending'",
        (draft_id,),
    )
    row = cur.fetchone()
    if not row:
        cur.close(); conn.close()
        return jsonify({"error": "Draft not found or already processed"}), 404
    try:
        import email.mime.text as _mime_text
        import base64 as _b64
        msg = _mime_text.MIMEText(row["body"], "plain", "utf-8")
        msg["to"] = row["to_addr"]
        msg["subject"] = row["subject"]
        if row["cc_addr"]:
            msg["cc"] = row["cc_addr"]
        raw = _b64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
        from googleapiclient.discovery import build as _gbuild
        svc = _gbuild("gmail", "v1", credentials=creds, cache_discovery=False)
        svc.users().messages().send(userId="me", body={"raw": raw}).execute()
        cur.execute("UPDATE gmail_drafts SET status='sent' WHERE id=%s", (draft_id,))
        conn.commit()
        log.info(f"Gmail draft {draft_id} sent to {row['to_addr']!r}")
        # Inject a note so Jarvis knows this draft was handled and won't recreate it
        if row.get("conversation_id"):
            _chat_persist_message(
                row["conversation_id"],
                "user",
                f"[Notification: Email draft '{row['subject']}' to {row['to_addr']} was sent by the student.]",
            )
    except Exception as e:
        conn.rollback()
        log.error("gmail_send_draft error: %s", e)
        cur.close(); conn.close()
        return jsonify({"error": str(e)}), 500
    cur.close(); conn.close()
    return jsonify({"status": "sent"})


@app.route("/api/google/gmail/drafts/<int:draft_id>", methods=["DELETE"])
def gmail_discard_draft(draft_id):
    if not session.get("authenticated"):
        return jsonify({"error": "Not authenticated"}), 401
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "SELECT subject, to_addr, conversation_id FROM gmail_drafts WHERE id=%s AND status='pending'",
        (draft_id,),
    )
    row = cur.fetchone()
    if not row:
        cur.close(); conn.close()
        return jsonify({"error": "Draft not found or already processed"}), 404
    cur.execute(
        "UPDATE gmail_drafts SET status='discarded' WHERE id=%s",
        (draft_id,),
    )
    conn.commit()
    if row.get("conversation_id"):
        _chat_persist_message(
            row["conversation_id"],
            "user",
            f"[Notification: Email draft '{row['subject']}' to {row['to_addr']} was discarded by the student.]",
        )
    cur.close(); conn.close()
    return jsonify({"status": "discarded"})


# ── SaaS: Signup ──────────────────────────────────────────────────────────────

@app.route("/signup", methods=["GET"])
def signup_page():
    if session.get("authenticated"):
        return redirect("/")
    return render_template("signup.html")


@app.route("/api/signup/validate-code", methods=["POST"])
def api_signup_validate_code():
    data = request.get_json(force=True) or {}
    code = str(data.get("code", "")).strip().upper()
    if not code:
        return jsonify({"error": "Access code required"}), 400
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
SELECT id, code, bypass_payment, expires_at, redeemed_by
FROM access_codes WHERE code = %s""", (code,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    if not row:
        return jsonify({"error": "Invalid access code"}), 404
    if row["redeemed_by"]:
        return jsonify({"error": "This access code has already been used"}), 409
    if row["expires_at"] and row["expires_at"] < datetime.now(TZ):
        return jsonify({"error": "This access code has expired"}), 410

    # Fetch current price for display
    monthly_cents = 999
    try:
        pc = get_db(); pcur = pc.cursor()
        pcur.execute("SELECT monthly_cents FROM pricing_config WHERE id = 1")
        prow = pcur.fetchone()
        pcur.close(); pc.close()
        if prow:
            monthly_cents = prow["monthly_cents"]
    except Exception:
        pass

    return jsonify({
        "valid": True,
        "bypass_payment": row["bypass_payment"],
        "monthly_cents": monthly_cents,
        "monthly_display": f"${monthly_cents / 100:.2f}",
    })


_CAL_KEYS = ("personal_ical_url", "canvas_ical_url", "canvas_api_token",
             "canvas_base_url", "sports_ical_url", "job_schedule_ical_url")


def _save_calendar_urls(user_id, data):
    """Save optional calendar URLs from signup data into user_config."""
    cals = {k: str(data.get(k, "")).strip()[:2000] for k in _CAL_KEYS}
    cals = {k: v for k, v in cals.items() if v}
    if "canvas_base_url" in cals:
        cals["canvas_base_url"] = cals["canvas_base_url"].rstrip("/")
    if cals:
        set_user_config(cals, user_id=user_id)


@app.route("/api/signup/create-checkout", methods=["POST"])
def api_signup_create_checkout():
    if not stripe or not STRIPE_SECRET_KEY:
        return jsonify({"error": "Stripe is not configured"}), 503
    data = request.get_json(force=True) or {}
    code    = str(data.get("code", "")).strip().upper()
    email   = str(data.get("email", "")).strip().lower()
    username = str(data.get("username", "")).strip()
    password = str(data.get("password", "")).strip()

    if not all([code, email, username, password]):
        return jsonify({"error": "All fields are required"}), 400
    if len(username) < 3 or len(password) < 6:
        return jsonify({"error": "Username must be ≥3 chars and password ≥6 chars"}), 400

    # Validate code
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT id, bypass_payment, redeemed_by, expires_at FROM access_codes WHERE code = %s", (code,))
    ac = cur.fetchone()
    if not ac or ac["redeemed_by"]:
        cur.close(); conn.close()
        return jsonify({"error": "Invalid or already-used access code"}), 400
    if ac["expires_at"] and ac["expires_at"] < datetime.now(TZ):
        cur.close(); conn.close()
        return jsonify({"error": "Access code expired"}), 400
    if ac["bypass_payment"]:
        cur.close(); conn.close()
        return jsonify({"error": "Use free signup for this code type"}), 400

    # Check username/email uniqueness
    cur.execute("SELECT id FROM users WHERE username = %s OR email = %s", (username, email))
    if cur.fetchone():
        cur.close(); conn.close()
        return jsonify({"error": "Username or email already taken"}), 409

    # Get current price_id
    cur.execute("SELECT stripe_price_id FROM pricing_config WHERE id = 1")
    pc_row = cur.fetchone()
    cur.close(); conn.close()
    price_id = pc_row["stripe_price_id"] if pc_row else ""
    if not price_id:
        return jsonify({"error": "Pricing not configured. Contact admin."}), 503

    # Stash calendar URLs in pending_signups so they survive the Stripe round trip
    cals_json = json.dumps({k: str(data.get(k, "")).strip() for k in _CAL_KEYS})
    pconn = get_db(); pcur = pconn.cursor()
    try:
        pcur.execute("""
INSERT INTO pending_signups (access_code, calendar_data, created_at)
VALUES (%s, %s, NOW())
ON CONFLICT (access_code) DO UPDATE SET calendar_data = EXCLUDED.calendar_data, created_at = NOW()""",
                     (code, cals_json))
        pconn.commit()
    except Exception as _pe:
        log.warning("pending_signups insert failed: %s", _pe)
        pconn.rollback()
    finally:
        pcur.close(); pconn.close()

    host = request.host_url.rstrip("/")
    try:
        checkout = stripe.checkout.Session.create(
            mode="subscription",
            payment_method_types=["card"],
            line_items=[{"price": price_id, "quantity": 1}],
            customer_email=email,
            success_url=f"{host}/signup/success?session_id={{CHECKOUT_SESSION_ID}}&code={code}&username={username}",
            cancel_url=f"{host}/signup/cancelled",
            metadata={"access_code": code, "username": username, "password_hash": generate_password_hash(password)},
        )
    except Exception as e:
        log.error("Stripe checkout create error: %s", e)
        return jsonify({"error": "Payment setup failed. Try again."}), 500

    return jsonify({"url": checkout.url})


@app.route("/api/signup/create-free", methods=["POST"])
def api_signup_create_free():
    data = request.get_json(force=True) or {}
    code     = str(data.get("code", "")).strip().upper()
    email    = str(data.get("email", "")).strip().lower()
    username = str(data.get("username", "")).strip()
    password = str(data.get("password", "")).strip()

    if not all([code, email, username, password]):
        return jsonify({"error": "All fields are required"}), 400
    if len(username) < 3 or len(password) < 6:
        return jsonify({"error": "Username must be ≥3 chars and password ≥6 chars"}), 400

    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT id, bypass_payment, redeemed_by, expires_at FROM access_codes WHERE code = %s", (code,))
    ac = cur.fetchone()
    if not ac or ac["redeemed_by"]:
        cur.close(); conn.close()
        return jsonify({"error": "Invalid or already-used access code"}), 400
    if ac["expires_at"] and ac["expires_at"] < datetime.now(TZ):
        cur.close(); conn.close()
        return jsonify({"error": "Access code expired"}), 400
    if not ac["bypass_payment"]:
        cur.close(); conn.close()
        return jsonify({"error": "This code requires payment. Use the paid signup."}), 400

    cur.execute("SELECT id FROM users WHERE username = %s OR email = %s", (username, email))
    if cur.fetchone():
        cur.close(); conn.close()
        return jsonify({"error": "Username or email already taken"}), 409

    user_id = str(uuid.uuid4())
    cur.execute("""
INSERT INTO users (id, email, username, password_hash, display_name, is_comped, active)
VALUES (%s, %s, %s, %s, %s, TRUE, TRUE)""",
        (user_id, email, username, generate_password_hash(password), username.title()))
    cur.execute("UPDATE access_codes SET redeemed_by = %s, redeemed_at = NOW() WHERE id = %s",
                (user_id, ac["id"]))
    conn.commit()
    cur.close(); conn.close()

    _init_user_defaults(user_id)
    _save_calendar_urls(user_id, data)
    log.info("Free signup: user %s (%s) created via code %s", username, email, code)
    return jsonify({"status": "ok", "redirect": "/login"})


@app.route("/signup/success", methods=["GET"])
def signup_success():
    session_id = request.args.get("session_id", "")
    code       = request.args.get("code", "").upper()
    username   = request.args.get("username", "")

    if not stripe or not session_id:
        return redirect("/login")

    try:
        checkout = stripe.checkout.Session.retrieve(session_id)
    except Exception as e:
        log.error("Stripe session retrieve: %s", e)
        return redirect("/login")

    if checkout.payment_status not in ("paid", "no_payment_required"):
        return redirect("/signup/cancelled")

    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT id, bypass_payment, redeemed_by, expires_at FROM access_codes WHERE code = %s", (code,))
    ac = cur.fetchone()
    if not ac or ac["redeemed_by"]:
        cur.close(); conn.close()
        return render_template("signup_success.html", already_exists=True)

    email = checkout.customer_details.email if checkout.customer_details else checkout.customer_email or ""
    pw_hash = checkout.metadata.get("password_hash", generate_password_hash(secrets.token_urlsafe(16)))
    un = checkout.metadata.get("username") or username

    # Create user
    user_id = str(uuid.uuid4())
    cur.execute("""
INSERT INTO users (id, email, username, password_hash, display_name, is_comped, active)
VALUES (%s, %s, %s, %s, %s, FALSE, TRUE)
ON CONFLICT (email) DO UPDATE SET last_login_at = NOW() RETURNING id""",
        (user_id, email, un, pw_hash, un.title()))
    result = cur.fetchone()
    if result:
        user_id = str(result["id"])

    # Create subscription record
    stripe_sub_id = None
    stripe_price_id = ""
    period_end = None
    if checkout.subscription:
        try:
            sub = stripe.Subscription.retrieve(checkout.subscription)
            stripe_sub_id = sub.id
            if sub.items.data:
                stripe_price_id = sub.items.data[0].price.id
            period_end = datetime.fromtimestamp(sub.current_period_end, tz=TZ)
        except Exception as _e:
            log.warning("Subscription retrieve: %s", _e)

    cur.execute("""
INSERT INTO subscriptions (user_id, stripe_customer_id, stripe_subscription_id, stripe_price_id, status, current_period_end)
VALUES (%s, %s, %s, %s, 'active', %s)
ON CONFLICT (stripe_customer_id) DO UPDATE SET status='active', updated_at=NOW()""",
        (user_id, checkout.customer or "", stripe_sub_id, stripe_price_id, period_end))

    cur.execute("UPDATE access_codes SET redeemed_by = %s, redeemed_at = NOW() WHERE id = %s", (user_id, ac["id"]))

    # Retrieve pending calendar URLs and save them for this user
    cal_data = {}
    try:
        cur.execute("SELECT calendar_data FROM pending_signups WHERE access_code = %s", (code,))
        prow = cur.fetchone()
        if prow and prow["calendar_data"]:
            cal_data = json.loads(prow["calendar_data"])
        cur.execute("DELETE FROM pending_signups WHERE access_code = %s", (code,))
    except Exception as _ce:
        log.warning("pending_signups retrieve: %s", _ce)

    conn.commit()
    cur.close(); conn.close()

    _init_user_defaults(user_id)
    if cal_data:
        _save_calendar_urls(user_id, cal_data)
    log.info("Paid signup success: user %s (%s)", un, email)
    return render_template("signup_success.html", username=un, email=email)


@app.route("/signup/cancelled", methods=["GET"])
def signup_cancelled():
    return render_template("signup.html", cancelled=True)


# ── SaaS: Stripe Webhooks ─────────────────────────────────────────────────────

@app.route("/api/webhooks/stripe", methods=["POST"])
def stripe_webhook():
    if not stripe or not STRIPE_WEBHOOK_SECRET:
        return jsonify({"error": "Stripe not configured"}), 503

    payload = request.get_data()
    sig_header = request.headers.get("Stripe-Signature", "")
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    except stripe.error.SignatureVerificationError:
        return jsonify({"error": "Invalid signature"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 400

    conn = get_db(); cur = conn.cursor()
    try:
        cur.execute("""INSERT INTO billing_events (stripe_event_id, event_type, payload)
VALUES (%s, %s, %s) ON CONFLICT (stripe_event_id) DO NOTHING RETURNING id""",
                    (event["id"], event["type"], json.dumps(dict(event))))
        if not cur.fetchone():
            cur.close(); conn.close()
            return jsonify({"status": "already_processed"})
        conn.commit()
    except Exception as e:
        log.warning("billing_events insert: %s", e)
        conn.rollback()

    etype = event["type"]
    obj = event["data"]["object"]

    try:
        if etype == "customer.subscription.updated":
            sub_id = obj["id"]
            status = obj["status"]
            period_end = datetime.fromtimestamp(obj["current_period_end"], tz=TZ)
            cur.execute("""UPDATE subscriptions SET status=%s, current_period_end=%s, updated_at=NOW()
WHERE stripe_subscription_id=%s""", (status, period_end, sub_id))

        elif etype == "customer.subscription.deleted":
            sub_id = obj["id"]
            cur.execute("""UPDATE subscriptions SET status='canceled', canceled_at=NOW(), updated_at=NOW()
WHERE stripe_subscription_id=%s""", (sub_id,))

        elif etype == "invoice.payment_failed":
            customer_id = obj.get("customer")
            if customer_id:
                cur.execute("UPDATE subscriptions SET status='past_due', updated_at=NOW() WHERE stripe_customer_id=%s", (customer_id,))

        conn.commit()
    except Exception as e:
        log.error("Stripe webhook handler error (%s): %s", etype, e)
        conn.rollback()
    finally:
        cur.close(); conn.close()

    return jsonify({"status": "ok"})


# ── SaaS: Billing portal ──────────────────────────────────────────────────────

@app.route("/billing", methods=["GET"])
def billing_page():
    if not session.get("authenticated"):
        return redirect("/login")
    user_id = _uid()
    sub_info = None
    if user_id:
        conn = get_db(); cur = conn.cursor()
        cur.execute("""
SELECT s.status, s.current_period_end, s.cancel_at_period_end, u.is_comped, u.email, u.username
FROM users u LEFT JOIN subscriptions s ON s.user_id = u.id
WHERE u.id = %s ORDER BY s.created_at DESC LIMIT 1""", (user_id,))
        row = cur.fetchone()
        cur.close(); conn.close()
        if row:
            sub_info = dict(row)
            if sub_info.get("current_period_end"):
                sub_info["current_period_end"] = sub_info["current_period_end"].isoformat()
    return render_template("billing.html", sub=sub_info)


@app.route("/api/billing/portal", methods=["POST"])
def api_billing_portal():
    if not session.get("authenticated"):
        return jsonify({"error": "Not authenticated"}), 401
    if not stripe or not STRIPE_SECRET_KEY:
        return jsonify({"error": "Stripe not configured"}), 503

    user_id = _uid()
    if not user_id:
        return jsonify({"error": "No user session"}), 401

    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT stripe_customer_id FROM subscriptions WHERE user_id = %s ORDER BY created_at DESC LIMIT 1", (user_id,))
    row = cur.fetchone()
    cur.close(); conn.close()
    if not row:
        return jsonify({"error": "No subscription found"}), 404

    try:
        portal = stripe.billing_portal.Session.create(
            customer=row["stripe_customer_id"],
            return_url=request.host_url.rstrip("/") + "/billing",
        )
        return jsonify({"url": portal.url})
    except Exception as e:
        log.error("Billing portal create: %s", e)
        return jsonify({"error": str(e)}), 500


# ── SaaS: Admin routes ────────────────────────────────────────────────────────

@app.route("/api/admin/pricing", methods=["GET", "POST"])
def api_admin_pricing():
    if not session.get("admin_authenticated"):
        return jsonify({"error": "Not authorized"}), 403

    conn = get_db(); cur = conn.cursor()
    if request.method == "GET":
        cur.execute("SELECT stripe_price_id, monthly_cents FROM pricing_config WHERE id = 1")
        row = cur.fetchone()
        cur.close(); conn.close()
        if row:
            return jsonify({"stripe_price_id": row["stripe_price_id"], "monthly_cents": row["monthly_cents"],
                            "monthly_display": f"${row['monthly_cents']/100:.2f}"})
        return jsonify({"stripe_price_id": "", "monthly_cents": 999, "monthly_display": "$9.99"})

    data = request.get_json(force=True) or {}
    monthly_dollars = float(data.get("monthly_dollars", 9.99))
    monthly_cents = int(round(monthly_dollars * 100))

    new_price_id = ""
    if stripe and STRIPE_SECRET_KEY and STRIPE_PRODUCT_ID:
        try:
            price = stripe.Price.create(
                unit_amount=monthly_cents,
                currency="usd",
                recurring={"interval": "month"},
                product=STRIPE_PRODUCT_ID,
            )
            new_price_id = price.id
        except Exception as e:
            log.error("Stripe price create: %s", e)
            cur.close(); conn.close()
            return jsonify({"error": f"Stripe error: {e}"}), 500
    else:
        new_price_id = data.get("stripe_price_id", "")

    cur.execute("""INSERT INTO pricing_config (id, stripe_price_id, monthly_cents, updated_at)
VALUES (1, %s, %s, NOW()) ON CONFLICT (id) DO UPDATE
SET stripe_price_id=EXCLUDED.stripe_price_id, monthly_cents=EXCLUDED.monthly_cents, updated_at=NOW()""",
                (new_price_id, monthly_cents))
    conn.commit()
    cur.close(); conn.close()
    return jsonify({"status": "ok", "stripe_price_id": new_price_id, "monthly_cents": monthly_cents})


@app.route("/api/admin/access-codes", methods=["GET"])
def api_admin_access_codes_list():
    if not session.get("admin_authenticated"):
        return jsonify({"error": "Not authorized"}), 403
    conn = get_db(); cur = conn.cursor()
    cur.execute("""
SELECT ac.id, ac.code, ac.bypass_payment, ac.created_at, ac.expires_at, ac.notes,
       ac.redeemed_at, u.username as redeemed_by_username
FROM access_codes ac
LEFT JOIN users u ON u.id = ac.redeemed_by
ORDER BY ac.created_at DESC""")
    rows = [dict(r) for r in cur.fetchall()]
    cur.close(); conn.close()
    for r in rows:
        if r["created_at"]: r["created_at"] = r["created_at"].isoformat()
        if r["expires_at"]: r["expires_at"] = r["expires_at"].isoformat()
        if r["redeemed_at"]: r["redeemed_at"] = r["redeemed_at"].isoformat()
    return jsonify({"codes": rows})


@app.route("/api/admin/access-codes", methods=["POST"])
def api_admin_access_codes_create():
    if not session.get("admin_authenticated"):
        return jsonify({"error": "Not authorized"}), 403
    data = request.get_json(force=True) or {}
    count        = min(int(data.get("count", 1)), 20)
    bypass       = bool(data.get("bypass_payment", False))
    notes        = str(data.get("notes", ""))[:200]
    expires_days = data.get("expires_days")
    expires_at   = None
    if expires_days:
        expires_at = datetime.now(TZ) + timedelta(days=int(expires_days))

    conn = get_db(); cur = conn.cursor()
    created = []
    for _ in range(count):
        code = "JARVIS-" + secrets.token_hex(3).upper()
        cur.execute("""
INSERT INTO access_codes (code, bypass_payment, expires_at, notes)
VALUES (%s, %s, %s, %s) ON CONFLICT (code) DO NOTHING RETURNING id, code""",
            (code, bypass, expires_at, notes))
        row = cur.fetchone()
        if row:
            created.append({"id": str(row["id"]), "code": row["code"], "bypass_payment": bypass})
    conn.commit()
    cur.close(); conn.close()
    return jsonify({"created": created})


@app.route("/api/admin/access-codes/<code_id>", methods=["DELETE"])
def api_admin_access_codes_delete(code_id):
    if not session.get("admin_authenticated"):
        return jsonify({"error": "Not authorized"}), 403
    conn = get_db(); cur = conn.cursor()
    cur.execute("DELETE FROM access_codes WHERE id = %s AND redeemed_by IS NULL", (code_id,))
    deleted = cur.rowcount
    conn.commit()
    cur.close(); conn.close()
    if deleted:
        return jsonify({"status": "revoked"})
    return jsonify({"error": "Code not found or already redeemed"}), 404


@app.route("/api/admin/subscribers", methods=["GET"])
def api_admin_subscribers():
    if not session.get("admin_authenticated"):
        return jsonify({"error": "Not authorized"}), 403
    conn = get_db(); cur = conn.cursor()
    cur.execute("""
SELECT u.id, u.username, u.email, u.display_name, u.created_at, u.last_login_at,
       u.active, u.is_comped,
       s.status as sub_status, s.current_period_end, s.cancel_at_period_end
FROM users u
LEFT JOIN subscriptions s ON s.user_id = u.id
  AND s.created_at = (SELECT MAX(s2.created_at) FROM subscriptions s2 WHERE s2.user_id = u.id)
ORDER BY u.created_at DESC""")
    rows = [dict(r) for r in cur.fetchall()]

    # MRR calc
    cur.execute("SELECT monthly_cents FROM pricing_config WHERE id = 1")
    pc = cur.fetchone()
    monthly_cents = pc["monthly_cents"] if pc else 999
    cur.close(); conn.close()

    paying_active = sum(1 for r in rows if not r["is_comped"] and r.get("sub_status") == "active")
    mrr_cents = paying_active * monthly_cents

    for r in rows:
        r["id"] = str(r["id"])
        if r["created_at"]: r["created_at"] = r["created_at"].isoformat()
        if r["last_login_at"]: r["last_login_at"] = r["last_login_at"].isoformat()
        if r["current_period_end"]: r["current_period_end"] = r["current_period_end"].isoformat()

    return jsonify({"subscribers": rows, "mrr_cents": mrr_cents,
                    "mrr_display": f"${mrr_cents/100:.2f}", "monthly_cents": monthly_cents})


@app.route("/api/admin/subscribers/<user_id>/cancel", methods=["POST"])
def api_admin_subscriber_cancel(user_id):
    if not session.get("admin_authenticated"):
        return jsonify({"error": "Not authorized"}), 403
    if not stripe or not STRIPE_SECRET_KEY:
        return jsonify({"error": "Stripe not configured"}), 503
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT stripe_subscription_id FROM subscriptions WHERE user_id = %s ORDER BY created_at DESC LIMIT 1", (user_id,))
    row = cur.fetchone()
    cur.close(); conn.close()
    if not row or not row["stripe_subscription_id"]:
        return jsonify({"error": "No active subscription found"}), 404
    try:
        stripe.Subscription.delete(row["stripe_subscription_id"])
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"status": "canceled"})


@app.route("/api/admin/subscribers/<user_id>/revoke", methods=["POST"])
def api_admin_subscriber_revoke(user_id):
    if not session.get("admin_authenticated"):
        return jsonify({"error": "Not authorized"}), 403
    conn = get_db(); cur = conn.cursor()
    cur.execute("UPDATE users SET active = FALSE WHERE id = %s AND is_comped = TRUE", (user_id,))
    affected = cur.rowcount
    conn.commit()
    cur.close(); conn.close()
    if affected:
        return jsonify({"status": "revoked"})
    return jsonify({"error": "User not found or not a comped user"}), 404


# ── End of SaaS routes ────────────────────────────────────────────────────────


# ── Waitlist / Approval signup flow ───────────────────────────────────────────

def _basic_email_ok(email):
    return bool(email) and "@" in email and "." in email.split("@")[-1]


@app.route("/api/signup/request-access", methods=["POST"])
def api_signup_request_access():
    data = request.get_json(force=True, silent=True) or {}
    name = str(data.get("name", "")).strip()
    email = str(data.get("email", "")).strip().lower()
    message = str(data.get("message", "")).strip()[:2000]
    if not name or not email:
        return jsonify({"error": "Name and email are required"}), 400
    if not _basic_email_ok(email):
        return jsonify({"error": "Invalid email"}), 400

    conn = get_db(); cur = conn.cursor()
    try:
        cur.execute("SELECT id FROM users WHERE email = %s", (email,))
        if cur.fetchone():
            cur.close(); conn.close()
            return jsonify({"error": "An account with this email already exists"}), 409

        cur.execute(
            "SELECT id FROM access_requests WHERE email = %s AND status = 'pending'",
            (email,),
        )
        if cur.fetchone():
            cur.close(); conn.close()
            return jsonify({"status": "ok", "message": "Request already pending"})

        cur.execute(
            "INSERT INTO access_requests (name, email, message, status) "
            "VALUES (%s, %s, %s, 'pending')",
            (name, email, message),
        )
        conn.commit()
    finally:
        cur.close(); conn.close()

    try:
        send_ntfy_notification(
            "New access request",
            f"{name} <{email}> requested access",
            priority="default",
            tags=["mailbox"],
        )
    except Exception as _e:
        log.debug("ntfy on access request failed: %s", _e)

    return jsonify({"status": "ok"})


@app.route("/api/admin/access-requests", methods=["GET"])
def api_admin_access_requests_list():
    if not session.get("admin_authenticated"):
        return jsonify({"error": "Unauthorized"}), 401
    status = (request.args.get("status") or "all").strip().lower()
    conn = get_db(); cur = conn.cursor()
    try:
        if status in ("pending", "approved", "denied"):
            cur.execute(
                "SELECT id, name, email, message, status, token, token_used, "
                "created_at, reviewed_at, reviewed_by "
                "FROM access_requests WHERE status = %s ORDER BY created_at DESC",
                (status,),
            )
        else:
            cur.execute(
                "SELECT id, name, email, message, status, token, token_used, "
                "created_at, reviewed_at, reviewed_by "
                "FROM access_requests ORDER BY created_at DESC"
            )
        rows = cur.fetchall() or []
    finally:
        cur.close(); conn.close()

    out = []
    for r in rows:
        out.append({
            "id": r["id"],
            "name": r["name"],
            "email": r["email"],
            "message": r.get("message") or "",
            "status": r["status"],
            "token": r.get("token") or "",
            "token_used": bool(r.get("token_used")),
            "created_at": r["created_at"].isoformat() if r.get("created_at") else "",
            "reviewed_at": r["reviewed_at"].isoformat() if r.get("reviewed_at") else "",
            "reviewed_by": r.get("reviewed_by") or "",
        })
    return jsonify({"requests": out})


@app.route("/api/admin/access-requests/<int:req_id>/approve", methods=["POST"])
def api_admin_access_request_approve(req_id):
    if not session.get("admin_authenticated"):
        return jsonify({"error": "Unauthorized"}), 401
    token = secrets.token_urlsafe(32)
    conn = get_db(); cur = conn.cursor()
    try:
        cur.execute(
            "UPDATE access_requests SET status='approved', token=%s, token_used=FALSE, "
            "reviewed_at=NOW(), reviewed_by='admin' WHERE id = %s "
            "RETURNING email, name",
            (token, req_id),
        )
        row = cur.fetchone()
        conn.commit()
    finally:
        cur.close(); conn.close()

    if not row:
        return jsonify({"error": "Request not found"}), 404

    approval_url = f"{request.host_url.rstrip('/')}/signup/complete?token={token}"
    body = f"""
    <p>Hi {row['name']},</p>
    <p>Your request for access to Jarvis Student AI has been approved.</p>
    <p>Click the link below to complete your signup:</p>
    <p><a href=\"{approval_url}\">{approval_url}</a></p>
    <p>This link is single-use. If you didn't request access, you can ignore this email.</p>
    """
    try:
        send_email(row["email"], "You've been approved! — Jarvis Student AI", body)
    except Exception as _e:
        log.warning("approval email failed: %s", _e)

    return jsonify({"status": "ok", "token": token})


@app.route("/api/admin/access-requests/<int:req_id>/deny", methods=["POST"])
def api_admin_access_request_deny(req_id):
    if not session.get("admin_authenticated"):
        return jsonify({"error": "Unauthorized"}), 401
    conn = get_db(); cur = conn.cursor()
    try:
        cur.execute(
            "UPDATE access_requests SET status='denied', reviewed_at=NOW(), "
            "reviewed_by='admin' WHERE id = %s RETURNING email, name",
            (req_id,),
        )
        row = cur.fetchone()
        conn.commit()
    finally:
        cur.close(); conn.close()

    if not row:
        return jsonify({"error": "Request not found"}), 404

    try:
        send_email(
            row["email"],
            "Access request update — Jarvis Student AI",
            f"<p>Hi {row['name']},</p><p>Thanks for your interest in Jarvis Student AI. "
            f"At this time we're unable to grant access. You're welcome to reach out later.</p>",
        )
    except Exception as _e:
        log.debug("denial email failed: %s", _e)

    return jsonify({"status": "ok"})


@app.route("/signup/complete", methods=["GET"])
def signup_complete_page():
    token = (request.args.get("token") or "").strip()
    if not token:
        return render_template("signup.html", approval_error="Missing token.")

    conn = get_db(); cur = conn.cursor()
    try:
        cur.execute(
            "SELECT id, name, email, status, token_used FROM access_requests "
            "WHERE token = %s",
            (token,),
        )
        row = cur.fetchone()
    finally:
        cur.close(); conn.close()

    if not row or row["status"] != "approved" or row["token_used"]:
        return render_template(
            "signup.html",
            approval_error="This signup link is invalid or has already been used.",
        )

    return render_template(
        "signup.html",
        approval_token=token,
        approval_email=row["email"],
        approval_name=row["name"],
    )


@app.route("/api/signup/complete-approved", methods=["POST"])
def api_signup_complete_approved():
    data = request.get_json(force=True, silent=True) or {}
    token = str(data.get("token", "")).strip()
    email = str(data.get("email", "")).strip().lower()
    username = str(data.get("username", "")).strip()
    password = str(data.get("password", "")).strip()

    if not all([token, email, username, password]):
        return jsonify({"error": "All fields are required"}), 400
    if len(username) < 3 or len(password) < 6:
        return jsonify({"error": "Username must be ≥3 chars and password ≥6 chars"}), 400

    conn = get_db(); cur = conn.cursor()
    cur.execute(
        "SELECT id, email, status, token_used FROM access_requests WHERE token = %s",
        (token,),
    )
    ar = cur.fetchone()
    if not ar or ar["status"] != "approved" or ar["token_used"]:
        cur.close(); conn.close()
        return jsonify({"error": "Invalid or already-used approval token"}), 400
    if (ar["email"] or "").lower() != email:
        cur.close(); conn.close()
        return jsonify({"error": "Email does not match the approved request"}), 400

    cur.execute("SELECT id FROM users WHERE username = %s OR email = %s", (username, email))
    if cur.fetchone():
        cur.close(); conn.close()
        return jsonify({"error": "Username or email already taken"}), 409
    cur.close(); conn.close()

    # If Stripe is configured, route through checkout. Otherwise create directly.
    if stripe and STRIPE_SECRET_KEY:
        pc = get_db(); pcur = pc.cursor()
        pcur.execute("SELECT stripe_price_id FROM pricing_config WHERE id = 1")
        pc_row = pcur.fetchone()
        pcur.close(); pc.close()
        price_id = pc_row["stripe_price_id"] if pc_row else ""
        if not price_id:
            return jsonify({"error": "Pricing not configured. Contact admin."}), 503

        # Stash calendar URLs under the token (reuses pending_signups keyed by access_code)
        cals_json = json.dumps({k: str(data.get(k, "")).strip() for k in _CAL_KEYS})
        sconn = get_db(); scur = sconn.cursor()
        try:
            scur.execute("""
INSERT INTO pending_signups (access_code, calendar_data, created_at)
VALUES (%s, %s, NOW())
ON CONFLICT (access_code) DO UPDATE SET calendar_data = EXCLUDED.calendar_data, created_at = NOW()""",
                         (f"TOKEN:{token}", cals_json))
            sconn.commit()
        except Exception as _pe:
            log.warning("pending_signups insert (token) failed: %s", _pe)
            sconn.rollback()
        finally:
            scur.close(); sconn.close()

        host = request.host_url.rstrip("/")
        try:
            checkout = stripe.checkout.Session.create(
                mode="subscription",
                payment_method_types=["card"],
                line_items=[{"price": price_id, "quantity": 1}],
                customer_email=email,
                success_url=f"{host}/signup/complete-success?session_id={{CHECKOUT_SESSION_ID}}&token={token}&username={username}",
                cancel_url=f"{host}/signup/cancelled",
                metadata={
                    "approval_token": token,
                    "username": username,
                    "password_hash": generate_password_hash(password),
                },
            )
        except Exception as e:
            log.error("Stripe checkout (approval) error: %s", e)
            return jsonify({"error": "Payment setup failed. Try again."}), 500
        return jsonify({"url": checkout.url})

    # No Stripe: create the user directly and burn the token.
    user_id = str(uuid.uuid4())
    conn = get_db(); cur = conn.cursor()
    try:
        cur.execute("""
INSERT INTO users (id, email, username, password_hash, display_name, is_comped, active)
VALUES (%s, %s, %s, %s, %s, TRUE, TRUE)""",
            (user_id, email, username, generate_password_hash(password), username.title()))
        cur.execute(
            "UPDATE access_requests SET token_used = TRUE WHERE token = %s",
            (token,),
        )
        conn.commit()
    finally:
        cur.close(); conn.close()

    _init_user_defaults(user_id)
    _save_calendar_urls(user_id, data)
    log.info("Approved signup (no Stripe): %s (%s)", username, email)
    return jsonify({"status": "ok", "redirect": "/login"})


@app.route("/signup/complete-success", methods=["GET"])
def signup_complete_success():
    session_id = request.args.get("session_id", "")
    token = (request.args.get("token", "") or "").strip()
    username = request.args.get("username", "")

    if not stripe or not session_id or not token:
        return redirect("/login")

    try:
        checkout = stripe.checkout.Session.retrieve(session_id)
    except Exception as e:
        log.error("Stripe session retrieve (approval): %s", e)
        return redirect("/login")

    if checkout.payment_status not in ("paid", "no_payment_required"):
        return redirect("/signup/cancelled")

    conn = get_db(); cur = conn.cursor()
    cur.execute(
        "SELECT id, email, status, token_used FROM access_requests WHERE token = %s",
        (token,),
    )
    ar = cur.fetchone()
    if not ar or ar["status"] != "approved" or ar["token_used"]:
        cur.close(); conn.close()
        return render_template("signup_success.html", already_exists=True)

    email = (checkout.customer_details.email if checkout.customer_details else checkout.customer_email) or ar["email"] or ""
    pw_hash = checkout.metadata.get("password_hash", generate_password_hash(secrets.token_urlsafe(16)))
    un = checkout.metadata.get("username") or username

    user_id = str(uuid.uuid4())
    cur.execute("""
INSERT INTO users (id, email, username, password_hash, display_name, is_comped, active)
VALUES (%s, %s, %s, %s, %s, FALSE, TRUE)
ON CONFLICT (email) DO UPDATE SET last_login_at = NOW() RETURNING id""",
        (user_id, email, un, pw_hash, un.title()))
    result = cur.fetchone()
    if result:
        user_id = str(result["id"])

    stripe_sub_id = None
    stripe_price_id = ""
    period_end = None
    if checkout.subscription:
        try:
            sub = stripe.Subscription.retrieve(checkout.subscription)
            stripe_sub_id = sub.id
            if sub.items.data:
                stripe_price_id = sub.items.data[0].price.id
            period_end = datetime.fromtimestamp(sub.current_period_end, tz=TZ)
        except Exception as _e:
            log.warning("Subscription retrieve (approval): %s", _e)

    cur.execute("""
INSERT INTO subscriptions (user_id, stripe_customer_id, stripe_subscription_id, stripe_price_id, status, current_period_end)
VALUES (%s, %s, %s, %s, 'active', %s)
ON CONFLICT (stripe_customer_id) DO UPDATE SET status='active', updated_at=NOW()""",
        (user_id, checkout.customer or "", stripe_sub_id, stripe_price_id, period_end))

    cur.execute("UPDATE access_requests SET token_used = TRUE WHERE token = %s", (token,))

    cal_data = {}
    try:
        cur.execute("SELECT calendar_data FROM pending_signups WHERE access_code = %s", (f"TOKEN:{token}",))
        prow = cur.fetchone()
        if prow and prow["calendar_data"]:
            cal_data = json.loads(prow["calendar_data"])
        cur.execute("DELETE FROM pending_signups WHERE access_code = %s", (f"TOKEN:{token}",))
    except Exception as _ce:
        log.warning("pending_signups retrieve (token): %s", _ce)

    conn.commit()
    cur.close(); conn.close()

    _init_user_defaults(user_id)
    if cal_data:
        _save_calendar_urls(user_id, cal_data)
    log.info("Approved+paid signup success: %s (%s)", un, email)
    return render_template("signup_success.html", username=un, email=email)


# ── End of waitlist routes ────────────────────────────────────────────────────


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
