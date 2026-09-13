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
import base64
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo
from urllib.parse import urlencode

import psycopg2
import psycopg2.extras
import psycopg2.pool
from psycopg2 import sql as pgsql
import requests
from functools import wraps
from flask import Flask, request, jsonify, render_template, session, redirect, Response
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import generate_password_hash, check_password_hash
from icalendar import Calendar
import recurring_ical_events
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.events import EVENT_JOB_ERROR
import anthropic
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
    PREFERRED_URL_SCHEME='https',
    # Only re-emit the session cookie when the session is actually modified.
    # With the default (True), *every* response for a permanent session rewrites
    # the cookie — so a burst of parallel page-load requests can clobber the
    # csrf_token that the /api/csrf-token request just stored (the requests that
    # loaded the session before the token existed re-serialize it without the
    # token, and whichever response lands last wins). Disabling per-request
    # refresh means only the request that mints the token emits a Set-Cookie,
    # eliminating that race. See _ensure_session_csrf_token below.
    SESSION_REFRESH_EACH_REQUEST=False,
)


_GZIP_TYPES = ('text/html', 'text/css', 'text/javascript', 'application/javascript', 'application/json', 'image/svg+xml')


@app.after_request
def no_cache_html(response):
    """Force the browser to revalidate HTML pages — the embedded CSRF
    interceptor JS would otherwise get stuck on a stale page after a deploy.
    API responses are live data (heart rate, tasks, calendar): mark them
    no-store so no browser, service worker, or proxy ever replays a stale
    reading as if it were current."""
    try:
        if (response.mimetype or '').lower() == 'text/html':
            response.headers['Cache-Control'] = 'no-store, must-revalidate'
            response.headers.pop('ETag', None)
        elif request.path.startswith('/api/'):
            response.headers['Cache-Control'] = 'no-store'
    except Exception:
        pass
    return response


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


def _ensure_session_csrf_token():
    """Mint a per-session CSRF token if the authenticated session lacks one.

    Crucially this runs on the page-navigation request (GET / , /admin, /parent),
    which is a *single* request issued before the page's JS fires its parallel
    API burst. Storing the token here means the cookie already carries it by the
    time those parallel requests run, so they all serialize the same token back
    and none can clobber it. Returns the token (existing or freshly minted)."""
    token = session.get('csrf_token')
    if not token:
        token = secrets.token_hex(32)
        session['csrf_token'] = token
        session.permanent = True
        session.modified = True
    return token


@app.before_request
def ensure_csrf_token_for_authenticated():
    """Backfill csrf_token for any authenticated session that doesn't have one
    yet (e.g. sessions created before this code shipped). Only mints on GET so
    we never mutate the session mid-CSRF-check on a state-changing request."""
    if request.method not in ("GET", "HEAD"):
        return None
    if (session.get("authenticated")
            or session.get("admin_authenticated")
            or session.get("parent_authenticated")):
        if not session.get("csrf_token"):
            _ensure_session_csrf_token()
    return None


@app.before_request
def require_csrf():
    if request.method not in ("POST", "PUT", "PATCH", "DELETE"):
        return None
    path = request.path.rstrip('/')
    if path in _CSRF_EXEMPT_PATHS or not path.startswith('/api/'):
        return None
    if path.startswith('/api/admin/login') or path.startswith('/api/parent/login'):
        return None
    # Public signup + Stripe webhook endpoints: the caller has no authenticated
    # session yet (or is Stripe), so CSRF protection adds no value.
    if path.startswith('/api/signup/') or path.startswith('/api/webhooks/'):
        return None
    # Admin endpoints: each guards on session.admin_authenticated and the
    # session cookie is SameSite=Lax, so a cross-origin POST can't send it.
    # Token-based CSRF on top of that has been brittle in practice.
    if path.startswith('/api/admin/'):
        return None
    expected = session.get('csrf_token')
    provided = request.headers.get('X-CSRF-Token', '')
    if not expected or not provided or not secrets.compare_digest(str(expected), str(provided)):
        reason = ("no-session-token" if not expected
                  else "no-header" if not provided
                  else "mismatch")
        log.warning("CSRF check failed for %s %s (%s; session_keys=%s)",
                    request.method, path, reason, sorted(session.keys()))
        return jsonify({"error": "CSRF token missing or invalid"}), 403
    return None


@app.before_request
def require_auth():
    path = request.path.rstrip('/')
    if path in ('/login', '/logout', '/admin', '/parent', '/manifest.json', '/sw.js'):
        return None
    if path.startswith('/signup'):
        return None
    if path in ('/api/lockdown-status', '/api/test-lockdown-status', '/api/test-security-code', '/api/test-admin-password', '/api/csrf-token'):
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


def _init_user_defaults(user_id):
    """Insert default user_config entries for a new student."""
    defaults = {
        "name": "Student",
        "wake_time": "07:00",
        "anthropic_api_key": "",
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
RED_DAY_ICAL_URL = os.environ.get("RED_DAY_ICAL_URL", "https://calendar.google.com/calendar/ical/pcschools.us_7ufb5f1vj8aks1shds5ou4fhe8%40group.calendar.google.com/public/basic.ics")
WHITE_DAY_ICAL_URL = os.environ.get("WHITE_DAY_ICAL_URL", "https://calendar.google.com/calendar/ical/pcschools.us_64ohm1bccvi50iti8fe455stkg%40group.calendar.google.com/public/basic.ics")

# ── WHOOP OAuth2 ────────────────────────────────────────────────────────────────
WHOOP_CLIENT_ID = os.environ.get("WHOOP_CLIENT_ID", "").strip()
WHOOP_CLIENT_SECRET = os.environ.get("WHOOP_CLIENT_SECRET", "").strip()
WHOOP_REDIRECT_URI = os.environ.get("WHOOP_REDIRECT_URI", "").strip()

# ── PowerSchool ───────────────────────────────────────────────────────────────
POWER_USERN = os.environ.get("POWER_USERN", "").strip()
POWER_PASS  = os.environ.get("POWER_PASS", "").strip()
PS_BASE_URL = "https://powerschool.pcschools.us"

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
        ("login_attempts", "CREATE TABLE IF NOT EXISTS login_attempts (id SERIAL PRIMARY KEY, ip_address TEXT NOT NULL, success BOOLEAN NOT NULL, username TEXT DEFAULT '', attempted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), user_agent TEXT)"),
        ("login_lockouts", "CREATE TABLE IF NOT EXISTS login_lockouts (ip_address TEXT PRIMARY KEY, locked_until TIMESTAMPTZ NOT NULL, failure_count INT NOT NULL DEFAULT 1, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())"),
        ("lockdown_state", "CREATE TABLE IF NOT EXISTS lockdown_state (id INT PRIMARY KEY DEFAULT 1, is_locked_down BOOLEAN NOT NULL DEFAULT FALSE, activated_at TIMESTAMPTZ, activated_by TEXT, CHECK (id = 1))"),
        ("blocked_ips", "CREATE TABLE IF NOT EXISTS blocked_ips (id SERIAL PRIMARY KEY, ip_address TEXT UNIQUE NOT NULL, ip_name TEXT DEFAULT '', blocked_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), blocked_by TEXT NOT NULL DEFAULT 'admin', reason TEXT DEFAULT '')"),
        ("ip_names", "CREATE TABLE IF NOT EXISTS ip_names (ip_address TEXT PRIMARY KEY, ip_name TEXT NOT NULL, tracked_at TIMESTAMPTZ NOT NULL DEFAULT NOW())"),
        ("canvas_assignments_cache", "CREATE TABLE IF NOT EXISTS canvas_assignments_cache (uid TEXT PRIMARY KEY, title TEXT NOT NULL, class_name TEXT NOT NULL DEFAULT '', due_iso TEXT NOT NULL, due_display TEXT NOT NULL DEFAULT '', description TEXT NOT NULL DEFAULT '', urgency TEXT NOT NULL DEFAULT 'low', promoted_to_task BOOLEAN NOT NULL DEFAULT FALSE, first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW())"),
        ("sync_events", "CREATE TABLE IF NOT EXISTS sync_events (id SERIAL PRIMARY KEY, connector TEXT NOT NULL, event TEXT NOT NULL, status TEXT NOT NULL DEFAULT '', detail TEXT NOT NULL DEFAULT '', duration_ms INT NOT NULL DEFAULT 0, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())"),
        ("sync_events_idx", "CREATE INDEX IF NOT EXISTS idx_sync_events_at ON sync_events(created_at DESC)"),
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
        cur.execute("ALTER TABLE completions ADD COLUMN submitted BOOLEAN NOT NULL DEFAULT FALSE")
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

    # Personal records tracker — manual overrides layered on top of values
    # computed from the WHOOP workout pipeline.
    try:
        cur.execute("""CREATE TABLE IF NOT EXISTS personal_records (
            record_key TEXT PRIMARY KEY,
            user_id UUID,
            label TEXT NOT NULL DEFAULT '',
            value_display TEXT NOT NULL DEFAULT '',
            value_numeric REAL,
            achieved_on DATE,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )""")
        conn.commit()
    except psycopg2.Error:
        conn.rollback()
        conn = get_db()
        cur = conn.cursor()

    # ── SaaS: add user_id column to all per-user data tables ──────────────────────
    _user_id_tables = [
        "completions", "assignment_estimates",
        "canvas_assignments_cache",
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
    defaults = {"name": "Jarvis", "wake_time": "07:00", "anthropic_api_key": "", "formal_signoff_name": "Finley Thomas"}
    for k, v in defaults.items():
        try:
            cur.execute("INSERT INTO config (key, value) VALUES (%s, %s) ON CONFLICT (key) DO NOTHING", (k, v))
        except Exception:
            pass
    conn.commit()

    # Initialize singleton records
    try:
        cur.execute("INSERT INTO lockdown_state (id, is_locked_down) VALUES (1, FALSE) ON CONFLICT (id) DO NOTHING")
        conn.commit()
    except Exception as e:
        log.debug(f"Singleton records: {e}")
        conn.rollback()
        conn = get_db()
        cur = conn.cursor()

    # Create indexes
    indexes = ["CREATE INDEX IF NOT EXISTS idx_completions_assignment_title ON completions(assignment_title)", "CREATE INDEX IF NOT EXISTS idx_completions_completed_at ON completions(completed_at DESC)", "CREATE INDEX IF NOT EXISTS idx_login_attempts_ip ON login_attempts(ip_address, attempted_at DESC)", "CREATE INDEX IF NOT EXISTS idx_login_lockouts_ip ON login_lockouts(ip_address)"]
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


# ── iCal caching ──────────────────────────────────────────────────────────────
_ical_cache = {}  # url -> (monotonic_time, Calendar)
_ical_cache_lock = threading.Lock()
_ical_inflight = {}  # url -> threading.Event for request coalescing
_ical_last_error = {}  # url -> {"at": iso, "msg": str}
_ical_neg_cache = {}  # url -> (monotonic_time, ttl_seconds) — back-off after a failed fetch
_ical_sync_lock = threading.Lock()
ICAL_CACHE_TTL = 300  # 5 minutes
ICAL_NEG_CACHE_TTL = 120  # back off 2 min after a transient fetch failure
ICAL_NEG_CACHE_TTL_PERMANENT = 600  # back off 10 min after a permanent (4xx) failure


def _ical_http_status(exc):
    """HTTP status code behind a requests error, or None for network errors."""
    resp = getattr(exc, "response", None)
    return getattr(resp, "status_code", None) if resp is not None else None


def _ical_error_is_permanent(exc):
    """A 4xx (other than 408/429) won't succeed on retry — don't hammer the feed."""
    code = _ical_http_status(exc)
    return code is not None and 400 <= code < 500 and code not in (408, 429)


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

        # Back off if this URL failed recently — avoid re-fetching a dead feed
        # on every request (a stale Canvas feed 404s on each page load otherwise)
        neg = _ical_neg_cache.get(url)
        if neg:
            failed_at, neg_ttl = neg
            if now - failed_at < neg_ttl:
                if url in _ical_cache:
                    return _ical_cache[url][1]  # serve stale rather than nothing
                return None
            _ical_neg_cache.pop(url, None)

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
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; StudentsAssistant/1.0; +https://github.com)",
        "Accept": "text/calendar, text/plain, */*",
    }
    last_exc = None
    try:
        for attempt in range(2):
            try:
                log.info(f"iCal: fetching {url} (attempt {attempt + 1})")
                resp = requests.get(url, timeout=20, headers=headers, allow_redirects=True)
                resp.raise_for_status()
                cal = Calendar.from_ical(resp.content)
                with _ical_cache_lock:
                    _ical_cache[url] = (time.monotonic(), cal)
                    _ical_neg_cache.pop(url, None)
                # Clear any prior error now that we have a successful fetch
                with _ical_sync_lock:
                    _ical_last_error.pop(url, None)
                new_event.set()  # Signal other waiting threads
                log.info(f"iCal: successfully cached {url}")
                return cal
            except Exception as e:
                last_exc = e
                if _ical_error_is_permanent(e):
                    # 404/401/403/410 etc. — a retry would fail identically.
                    log.warning("iCal: permanent error %s for %s; not retrying",
                                _ical_http_status(e), url)
                    break
                if attempt == 0:
                    log.info(f"iCal: attempt 1 failed for {url} ({e}); retrying once")
                    time.sleep(1.5)
        # Both attempts failed (or a permanent error short-circuited the retry)
        log.warning("iCal fetch failed for %s: %s", url, last_exc)
        new_event.set()  # Signal other waiting threads even on failure
        neg_ttl = (ICAL_NEG_CACHE_TTL_PERMANENT if _ical_error_is_permanent(last_exc)
                   else ICAL_NEG_CACHE_TTL)
        with _ical_cache_lock:
            _ical_neg_cache[url] = (time.monotonic(), neg_ttl)
        with _ical_sync_lock:
            _ical_last_error[url] = {"at": datetime.now(TZ).isoformat(), "msg": str(last_exc)}
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


def _ical_forget(url):
    """Drop all cached state for a feed URL so the next fetch starts fresh.

    Called when a user updates a calendar URL in settings, so a corrected feed
    recovers immediately instead of waiting out the failure back-off.
    """
    if not url:
        return
    if url.startswith("webcal://"):
        url = "https://" + url[9:]
    with _ical_cache_lock:
        _ical_neg_cache.pop(url, None)
        _ical_cache.pop(url, None)
    with _ical_sync_lock:
        _ical_last_error.pop(url, None)


# Friendly labels for calendar feeds, shared by validation + sync-status.
ICAL_FEED_LABELS = {
    "personal_ical_url":     "Personal",
    "canvas_ical_url":       "Canvas",
    "sports_ical_url":       "Sports",
}


def _validate_ical_url(url):
    """Lightweight reachability + format check for a user-supplied calendar URL.

    Returns a short, human-readable problem description (suitable for showing to
    the student), or None if the feed looks OK. Used at settings-save time so a
    dead/expired link (e.g. a reset Canvas feed token that 404s) is caught
    immediately instead of silently importing zero assignments.
    """
    url = (url or "").strip()
    if not url:
        return None
    if url.startswith("webcal://"):
        url = "https://" + url[9:]
    if not url.lower().startswith(("http://", "https://")):
        return "doesn't look like a valid URL — paste the full https:// feed link"
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; StudentsAssistant/1.0; +https://github.com)",
        "Accept": "text/calendar, text/plain, */*",
    }
    try:
        resp = requests.get(url, timeout=10, headers=headers, allow_redirects=True)
    except requests.exceptions.Timeout:
        return "timed out — the calendar server didn't respond"
    except Exception:
        return "could not be reached — double-check the URL"
    if resp.status_code == 404:
        return "returned 404 Not Found — the link is likely expired; copy a fresh feed URL from the calendar"
    if resp.status_code in (401, 403):
        return (f"returned HTTP {resp.status_code} (access denied) — the feed may be "
                "private or the link has expired")
    if resp.status_code >= 400:
        return f"returned HTTP {resp.status_code}"
    body = resp.text or ""
    if "BEGIN:VCALENDAR" not in body[:8000].upper():
        return "didn't return calendar data — make sure it's an iCal (.ics) feed URL"
    return None


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


# ── WHOOP OAuth2 + API helpers ────────────────────────────────────────────────
# Recovery / sleep / strain data from the WHOOP wearable. Uses OAuth2 with a
# refresh token (WHOOP has no long-lived static API key), stored in the global
# config table like the Google integration. Silently no-ops when not connected.

WHOOP_AUTH_URL = "https://api.prod.whoop.com/oauth/oauth2/auth"
WHOOP_TOKEN_URL = "https://api.prod.whoop.com/oauth/oauth2/token"
WHOOP_API_BASE = "https://api.prod.whoop.com/developer/v2"
WHOOP_SCOPES = "read:recovery read:cycles read:sleep read:workout read:profile read:body_measurement offline"
WHOOP_CACHE_TTL = 900  # 15 minutes
# Failed WHOOP requests are remembered briefly so the once-a-minute heart-rate
# poll doesn't re-run full network timeouts on every tick while WHOOP is down
# (which held /api/whoop/heart-rate past the frontend's 15s abort and froze
# the widget). Short enough that recovery is quick; connect/disconnect clears
# it explicitly so reconnects still take effect immediately.
WHOOP_FAIL_TTL = 120  # 2 minutes

# WHOOP issues a single-use, rotating refresh token: each refresh call both
# consumes the current refresh token and returns a new one, and WHOOP treats
# reuse of an already-consumed refresh token as a replay and revokes the
# whole token family. The Health & Fitness dashboard fires several requests
# in parallel (status, summary, workouts, heart-rate, PRs) that can all see
# an expired access token at once; without serialization each one refreshes
# with the same stale refresh token, only the first succeeds, and the losers'
# reuse attempts can get the account booted off WHOOP entirely — surfacing to
# the student as "WHOOP keeps disconnecting." This lock makes the
# check-and-refresh atomic so concurrent callers share one refresh.
_whoop_token_lock = threading.Lock()


def _whoop_clear_cache():
    """Drop cached WHOOP records (and remembered failures) so a fresh
    connect/disconnect takes effect immediately instead of waiting out a TTL."""
    with _simple_cache_lock:
        for key in ("whoop:recovery", "whoop:sleep", "whoop:cycles", "whoop:workouts"):
            _simple_cache.pop(key, None)
            _simple_cache.pop(key + ":fail", None)
        _simple_cache.pop("whoop:token_fail", None)


def _whoop_configured():
    return bool(WHOOP_CLIENT_ID and WHOOP_CLIENT_SECRET)


def _whoop_connected():
    return bool(_whoop_configured() and get_config().get("whoop_refresh_token", "").strip())


def _get_whoop_access_token():
    """Return a valid WHOOP access token, refreshing it if expired. None if not connected.

    Serialized by _whoop_token_lock: WHOOP's refresh token rotates on every
    use, so two concurrent refreshes racing on the same stale refresh token
    would cost the account its connection (see comment on WHOOP_FAIL_TTL)."""
    if not _whoop_configured():
        return None
    with _whoop_token_lock:
        cfg = get_config()
        refresh_token = cfg.get("whoop_refresh_token", "").strip()
        if not refresh_token:
            return None
        access_token = cfg.get("whoop_access_token", "").strip()
        try:
            expires_at = float(cfg.get("whoop_token_expires_at", "0") or 0)
        except ValueError:
            expires_at = 0
        if access_token and expires_at - 60 > time.time():
            return access_token
        if _cache_get("whoop:token_fail", WHOOP_FAIL_TTL):
            return None
        try:
            # Refresh grants may only request scopes from the original consent, so
            # ask for "offline" (needed for the rotating refresh token) rather than
            # WHOOP_SCOPES — tokens granted before a scope was added to that list
            # would otherwise fail every refresh until the user reconnects.
            resp = requests.post(WHOOP_TOKEN_URL, data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": WHOOP_CLIENT_ID,
                "client_secret": WHOOP_CLIENT_SECRET,
                "scope": "offline",
            }, timeout=12)
            resp.raise_for_status()
            data = resp.json()
            new_access = data.get("access_token", "")
            set_config({
                "whoop_access_token": new_access,
                "whoop_refresh_token": data.get("refresh_token") or refresh_token,
                "whoop_token_expires_at": str(time.time() + float(data.get("expires_in", 3600))),
            })
            return new_access or None
        except Exception as e:
            log.warning("WHOOP token refresh failed: %s", e)
            _cache_set("whoop:token_fail", True)
            return None


def _whoop_get(path, params=None, timeout=12):
    token = _get_whoop_access_token()
    if not token:
        return None
    url = WHOOP_API_BASE + (path if path.startswith("/") else "/" + path)
    headers = {"Authorization": "Bearer " + token}
    try:
        resp = requests.get(url, headers=headers, params=params or {}, timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        log.warning("WHOOP API GET %s failed: %s", path, e)
        return None


def _whoop_records(cache_key, path, limit):
    """Fetch a WHOOP record list with success caching (WHOOP_CACHE_TTL) and
    short failure caching (WHOOP_FAIL_TTL). Failures must be remembered:
    with no failure cache, every minute-poll re-runs the full stack of
    network timeouts and the request outlives the frontend's abort deadline.
    Connect/disconnect clears both caches, so reconnects retry immediately."""
    cached = _cache_get(cache_key, WHOOP_CACHE_TTL)
    if cached is not None:
        return cached
    if _cache_get(cache_key + ":fail", WHOOP_FAIL_TTL):
        return []
    data = _whoop_get(path, params={"limit": limit})
    if data is None:
        _cache_set(cache_key + ":fail", True)
        return []
    records = data.get("records") or []
    _cache_set(cache_key, records)
    return records


def whoop_recovery_recent(limit=10):
    return _whoop_records("whoop:recovery", "/recovery", limit)


def whoop_sleep_recent(limit=10):
    return _whoop_records("whoop:sleep", "/activity/sleep", limit)


def whoop_cycles_recent(limit=10):
    return _whoop_records("whoop:cycles", "/cycle", limit)


def whoop_workouts_recent(limit=25):
    return _whoop_records("whoop:workouts", "/activity/workout", limit)


# Partial WHOOP sport_id → name map (v2 records usually carry sport_name; this
# is the fallback for older records).
_WHOOP_SPORT_IDS = {
    -1: "Activity", 0: "Running", 1: "Cycling", 16: "Baseball", 17: "Basketball",
    18: "Rowing", 22: "Golf", 24: "Ice Hockey", 27: "Lacrosse", 30: "Soccer",
    33: "Swimming", 34: "Tennis", 36: "Volleyball", 42: "Skiing", 43: "Snowboarding",
    44: "Softball", 45: "Weightlifting", 48: "Functional Fitness", 52: "Hiking",
    57: "Pilates", 59: "Spin", 63: "Walking", 66: "Yoga", 71: "Track & Field",
    97: "Swim Practice", 98: "Climbing",
}


def _normalize_whoop_workout(rec):
    """Flatten a WHOOP v2 workout record into the shape the dashboard consumes."""
    score = rec.get("score") or {}
    start = rec.get("start") or ""
    end = rec.get("end") or ""
    duration_min = None
    try:
        s = datetime.fromisoformat(start.replace("Z", "+00:00"))
        e = datetime.fromisoformat(end.replace("Z", "+00:00"))
        duration_min = round((e - s).total_seconds() / 60)
    except Exception:
        pass
    sport = rec.get("sport_name") or _WHOOP_SPORT_IDS.get(rec.get("sport_id"), "Workout")
    kj = score.get("kilojoule")
    return {
        "id": str(rec.get("id", "")),
        "sport": sport,
        "start": start,
        "end": end,
        "date": start[:10],
        "duration_min": duration_min,
        "strain": score.get("strain"),
        "avg_hr": score.get("average_heart_rate"),
        "max_hr": score.get("max_heart_rate"),
        "calories": round(kj * 0.239006) if kj else None,
        "distance_m": score.get("distance_meter"),
    }


def _mock_whoop_workouts(days=14):
    """Deterministic sample workouts so the Health dashboard pipeline works
    end-to-end before a WHOOP account is connected. Same shape as the
    normalized real records — swapping in live data changes nothing downstream."""
    plan = [  # rotating weekly pattern seeded off the weekday
        ("Running",       45, 6200,  148, 176, 14.2),
        ("Weightlifting", 60, None,  118, 152, 10.1),
        ("Swimming",      50, 1800,  132, 158, 11.6),
        ("Running",       35, 5000,  152, 181, 12.8),
        (None,) * 6,  # rest day
        ("Running",       80, 12100, 145, 172, 16.9),
        ("Functional Fitness", 40, None, 126, 160, 9.4),
    ]
    out = []
    today = datetime.now(TZ).date()
    for d in range(days):
        day = today - timedelta(days=d)
        sport, mins, dist, avg, mx, strain = plan[day.weekday() % 7]
        if not sport:
            continue
        start_dt = datetime(day.year, day.month, day.day, 16, 30, tzinfo=TZ)
        out.append({
            "id": "mock-%s" % day.isoformat(),
            "sport": sport,
            "start": start_dt.isoformat(),
            "end": (start_dt + timedelta(minutes=mins)).isoformat(),
            "date": day.isoformat(),
            "duration_min": mins,
            "strain": strain,
            "avg_hr": avg,
            "max_hr": mx,
            "calories": round(mins * 9.5),
            "distance_m": dist,
        })
    return out


def fitness_workouts(limit=25):
    """Recent workouts for the Health dashboard: live WHOOP data when the
    account is connected, otherwise the mock pipeline. Returns (workouts, is_mock)."""
    if _whoop_connected():
        records = whoop_workouts_recent(limit=limit)
        if records:
            return [_normalize_whoop_workout(r) for r in records], False
    return _mock_whoop_workouts(), True


def _mock_heart_rate_samples():
    """Plausible last-30-minute HR series (WHOOP's REST API has no live HR
    stream — broadcast HR is BLE-only — so this seat-fills the display slot).
    Phase is driven by each sample's epoch minute, so the series is
    deterministic within a minute but guaranteed to move between polls, and
    is independent of the server's configured timezone. Each sample carries
    an epoch-ms "ts" — the client labels times itself from those, so a wrong
    server clock or timezone can never show up as a stale-looking reading.
    The legacy "t" label is kept as a fallback only."""
    import math
    now = datetime.now(TZ)
    base = 62
    samples = []
    for i in range(30):
        t = now - timedelta(minutes=29 - i)
        m = int(t.timestamp() // 60)  # epoch minute: timezone-independent
        bpm = base + round(6 * math.sin(m / 4.7) + 3 * math.sin(m / 1.9)) + (m % 3)
        samples.append({"t": t.strftime("%H:%M"), "ts": int(t.timestamp() * 1000), "bpm": bpm})
    return samples


def fitness_heart_rate():
    """Recent/current heart-rate payload. Uses the latest WHOOP recovery (RHR)
    to anchor the numbers when connected; mock otherwise. This is polled every
    minute by the Health dashboard, so it only touches the recovery feed (one
    upstream call, usually cached) — not the full daily summary, whose three
    upstream calls could outlast the frontend's fetch timeout when WHOOP is slow."""
    samples = _mock_heart_rate_samples()
    source = "mock"
    if _whoop_connected():
        try:
            scores = ((r.get("score") or {}) for r in whoop_recovery_recent())
            rhr = next((s.get("resting_heart_rate") for s in scores
                        if s.get("resting_heart_rate")), None)
            if rhr:
                delta = rhr + 4 - samples[-1]["bpm"]
                for s in samples:
                    s["bpm"] = max(40, s["bpm"] + delta)
                source = "derived"  # anchored to live WHOOP resting HR
        except Exception as e:
            log.warning("fitness_heart_rate: %s", e)
    return {"current_bpm": samples[-1]["bpm"], "samples": samples, "source": source}


_MILE_M = 1609.34

PR_LABELS = {
    "longest_run": "Longest Run",
    "fastest_mile": "Fastest Mile",
    "longest_swim": "Longest Swim",
    "highest_strain": "Highest Strain",
}


def _fmt_pace(seconds):
    m, s = divmod(int(round(seconds)), 60)
    return "%d:%02d" % (m, s)


def compute_personal_records(workouts):
    """Historical bests derived from the workout pipeline (real or mock)."""
    runs = [w for w in workouts if "run" in (w["sport"] or "").lower()]
    swims = [w for w in workouts if "swim" in (w["sport"] or "").lower()]

    prs = {}
    dist_runs = [w for w in runs if w.get("distance_m")]
    if dist_runs:
        best = max(dist_runs, key=lambda w: w["distance_m"])
        prs["longest_run"] = {
            "label": "Longest Run",
            "value_display": "%.1f mi" % (best["distance_m"] / _MILE_M),
            "value_numeric": best["distance_m"],
            "achieved_on": best["date"],
        }
    pace_runs = [w for w in dist_runs if w.get("duration_min") and w["distance_m"] >= _MILE_M]
    if pace_runs:
        best = min(pace_runs, key=lambda w: w["duration_min"] * 60 / (w["distance_m"] / _MILE_M))
        secs = best["duration_min"] * 60 / (best["distance_m"] / _MILE_M)
        prs["fastest_mile"] = {
            "label": "Fastest Mile",
            "value_display": _fmt_pace(secs) + " /mi",
            "value_numeric": secs,
            "achieved_on": best["date"],
        }
    dist_swims = [w for w in swims if w.get("distance_m")]
    if dist_swims:
        best = max(dist_swims, key=lambda w: w["distance_m"])
        prs["longest_swim"] = {
            "label": "Longest Swim",
            "value_display": "%d m" % round(best["distance_m"]),
            "value_numeric": best["distance_m"],
            "achieved_on": best["date"],
        }
    # Unlike the run/swim records above, strain is logged on every workout
    # type (lifting, functional fitness, etc.), so this is the one PR that
    # reflects the full training mix rather than just endurance sports.
    strain_workouts = [w for w in workouts if w.get("strain") is not None]
    if strain_workouts:
        best = max(strain_workouts, key=lambda w: w["strain"])
        prs["highest_strain"] = {
            "label": "Highest Strain",
            "value_display": "%.1f" % best["strain"],
            "value_numeric": best["strain"],
            "achieved_on": best["date"],
        }
    return prs


def _mock_whoop_daily_summary(days=7):
    """Deterministic sample recovery/sleep/strain history so the Health
    dashboard's summary-driven widgets (stat tiles, sleep-score chart) still
    work end-to-end before a WHOOP account is connected."""
    plan = [  # (recovery, hrv_ms, rhr, sleep_performance, sleep_hours, strain) rotating by weekday
        (58, 62, 58, 74, 7.1, 14.2),
        (71, 71, 54, 88, 7.9, 10.1),
        (64, 66, 56, 79, 7.4, 11.6),
        (49, 55, 61, 65, 6.3, 12.8),
        (82, 80, 51, 93, 8.4, 6.5),
        (75, 74, 53, 85, 8.0, 16.9),
        (67, 68, 55, 81, 7.6, 9.4),
    ]
    today = datetime.now(TZ).date()
    out = []
    for d in range(days):
        day = today - timedelta(days=d)
        recovery, hrv, rhr, sleep_perf, sleep_hrs, strain = plan[day.weekday() % 7]
        out.append({
            "date": day.isoformat(),
            "recovery_score": recovery,
            "hrv_ms": hrv,
            "rhr": rhr,
            "sleep_performance": sleep_perf,
            "sleep_hours": sleep_hrs,
            "strain": strain,
        })
    return out


def fitness_daily_summary(days=7):
    """Recovery/sleep/strain history for the Health dashboard: live WHOOP data
    when connected, otherwise the mock pipeline. Returns (days, is_mock)."""
    if _whoop_connected():
        try:
            live = whoop_daily_summary(days)
            if live:
                return live, False
        except Exception as e:
            log.warning("fitness_daily_summary: %s", e)
    return _mock_whoop_daily_summary(days), True


def whoop_daily_summary(days=7):
    """Merge recovery/sleep/strain records into one row per calendar day, newest first."""
    if not _whoop_connected():
        return []
    limit = days + 3
    recovery = whoop_recovery_recent(limit=limit)
    sleep = whoop_sleep_recent(limit=limit)
    cycles = whoop_cycles_recent(limit=limit)

    def day_key(iso):
        return iso[:10] if iso else None

    by_date = {}
    for c in cycles:
        d = day_key(c.get("start"))
        if not d:
            continue
        score = c.get("score") or {}
        by_date.setdefault(d, {})["strain"] = score.get("strain")

    for r in recovery:
        d = day_key(r.get("created_at"))
        if not d:
            continue
        score = r.get("score") or {}
        entry = by_date.setdefault(d, {})
        entry["recovery_score"] = score.get("recovery_score")
        entry["hrv_ms"] = score.get("hrv_rmssd_milli")
        entry["rhr"] = score.get("resting_heart_rate")

    for s in sleep:
        d = day_key(s.get("end") or s.get("start"))
        if not d:
            continue
        score = s.get("score") or {}
        entry = by_date.setdefault(d, {})
        entry["sleep_performance"] = score.get("sleep_performance_percentage")
        # WHOOP's stage_summary has no single "total sleep" field — sum the
        # actual sleep stages (light + slow-wave + REM), excluding awake time.
        stage_summary = score.get("stage_summary") or {}
        sleep_ms = (
            (stage_summary.get("total_light_sleep_time_milli") or 0)
            + (stage_summary.get("total_slow_wave_sleep_time_milli") or 0)
            + (stage_summary.get("total_rem_sleep_time_milli") or 0)
        )
        entry["sleep_hours"] = round(sleep_ms / 3600000, 1) if sleep_ms else None

    ordered = sorted(by_date.items(), key=lambda kv: kv[0], reverse=True)[:days]
    return [{"date": d, **vals} for d, vals in ordered]


def _mock_sleep_need_hours():
    """Deterministic sample sleep-need estimate (weekday-seeded) so the
    Bedtime widget has something to show before WHOOP is connected."""
    plan = [8.3, 8.0, 8.1, 8.4, 7.8, 8.6, 8.9]
    return plan[datetime.now(TZ).date().weekday() % 7]


def whoop_bedtime_recommendation():
    """Recommended bedtime tonight, worked backward from the student's usual
    wake time minus their
    current sleep need. Uses WHOOP's own sleep_needed breakdown (baseline +
    debt + recent strain + recent naps) when connected, a deterministic
    estimate otherwise."""
    cfg = get_config()
    # Older installs stored this under the morning-briefing key.
    wake_str = (cfg.get("wake_time") or cfg.get("morning_briefing_time") or "07:00").strip()
    try:
        wake_h, wake_m = (int(x) for x in wake_str.split(":"))
    except (ValueError, TypeError):
        wake_h, wake_m = 7, 0

    need_hours = _mock_sleep_need_hours()
    is_mock = True
    if _whoop_connected():
        try:
            records = whoop_sleep_recent(limit=1)
            need = ((records[0].get("score") or {}).get("sleep_needed") or {}) if records else {}
            total_ms = (
                (need.get("baseline_milli") or 0)
                + (need.get("need_from_sleep_debt_milli") or 0)
                + (need.get("need_from_recent_strain_milli") or 0)
                + (need.get("need_from_recent_nap_milli") or 0)
            )
            if total_ms:
                need_hours = total_ms / 3600000
                is_mock = False
        except Exception as e:
            log.warning("whoop_bedtime_recommendation: %s", e)

    now = datetime.now(TZ)
    wake_dt = now.replace(hour=wake_h, minute=wake_m, second=0, microsecond=0) + timedelta(days=1)
    bedtime_dt = wake_dt - timedelta(hours=need_hours)

    return {
        "wake_time": "%02d:%02d" % (wake_h, wake_m),
        "sleep_need_hours": round(need_hours, 1),
        "bedtime_iso": bedtime_dt.isoformat(),
        "bedtime_display": bedtime_dt.strftime("%-I:%M %p"),
        "mock": is_mock,
    }


# ── PowerSchool Scraper (Playwright + Claude Vision) ──────────────────────────
# Uses a headless Chromium browser to log in as a real user, screenshots the
# grades page, then sends the image to Claude vision for extraction.
# No HTML parsing — works regardless of PowerSchool's JS rendering.

PS_GRADES_TTL     = 1800   # 30 minutes — screenshot + vision result lifetime
PS_ATTENDANCE_TTL = 3600   # 1 hour

# PowerSchool's login form hashes the password client-side as
# md5(user.lower() + ":" + md5(password) + ":" + pstoken).
def _ps_md5(value: str) -> str:
    return hashlib.md5(value.encode("utf-8")).hexdigest()

_ps_session_lock = threading.Lock()
_ps_session_cache = {"session": None, "home_url": "", "expires": 0.0}


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

    # 3. Merge in overdue assignments from cache that are not yet completed
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


_SOURCE_CATEGORY = {
    "canvas":   "school",
    "school":   "school",
    "red_day":  "school",
    "white_day": "school",
    "sports":   "health",
    "personal": "general",
}


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


scheduler = BackgroundScheduler(timezone=TZ)


def _on_scheduler_job_error(event):
    msg = str(event.exception) if event.exception else "unknown"
    log.error("APScheduler job %s failed: %s", event.job_id, msg, exc_info=event.exception)
    _scheduler_last_error_set(event.job_id, msg)


scheduler.add_listener(_on_scheduler_job_error, EVENT_JOB_ERROR)


def cleanup_old_data():
    """Prune the sync audit log so it stays a rolling window."""
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("DELETE FROM sync_events WHERE created_at < NOW() - INTERVAL '14 days'")
        deleted = cur.rowcount
        conn.commit()
        cur.close()
        conn.close()
        if deleted:
            log.info("cleanup_old_data: pruned %d sync_events rows", deleted)
    except Exception as e:
        log.error("cleanup_old_data error: %s", e)


# ── Connector sync pipeline ────────────────────────────────────────────────────
# Three connectors feed the app: Canvas (iCal + REST), PowerSchool (scraper) and
# WHOOP (OAuth2). Each run appends to sync_events, which is what the Sync & Feeds
# page renders as its audit trail.

CONNECTORS = ("canvas", "powerschool", "whoop")

_sync_run_lock = threading.Lock()
_sync_last_run = {}          # connector -> {"at", "status", "detail", "duration_ms"}
_sync_last_run_lock = threading.Lock()


def record_sync_event(connector, event, status, detail="", duration_ms=0):
    """Append one row to the audit log and remember it as the connector's last run."""
    with _sync_last_run_lock:
        _sync_last_run[connector] = {
            "at": datetime.now(TZ).isoformat(),
            "status": status,
            "detail": detail,
            "duration_ms": duration_ms,
        }
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO sync_events (connector, event, status, detail, duration_ms) "
            "VALUES (%s, %s, %s, %s, %s)",
            (connector[:40], event[:80], status[:40], detail[:500], int(duration_ms)),
        )
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        log.debug("record_sync_event failed: %s", e)


def _timed(connector, event, fn):
    """Run fn, timing it and logging the outcome to the audit trail."""
    t0 = time.time()
    try:
        status, detail = fn()
        ms = int((time.time() - t0) * 1000)
        record_sync_event(connector, event, status, detail, ms)
        return True
    except Exception as e:
        ms = int((time.time() - t0) * 1000)
        record_sync_event(connector, event, "Error", str(e)[:500], ms)
        log.warning("sync %s/%s failed: %s", connector, event, e)
        return False


def sync_canvas():
    """Refresh the Canvas iCal feed and re-cache its assignments."""
    def run():
        url = u_canvas_ical()
        if not url:
            return "Skipped", "No Canvas iCal URL configured"
        _ical_forget(url)
        cal = fetch_ical(url)
        if cal is None:
            raise RuntimeError("Canvas iCal fetch returned nothing")
        live = parse_canvas_assignments(cal)
        _cache_canvas_assignments(live)
        return "200 OK", f"Imported {len(live)} upcoming events"
    return _timed("canvas", "canvas.ical.fetch", run)


def sync_powerschool():
    """Re-run the PowerSchool scrape and refresh the grade cache."""
    def run():
        if not _ps_configured():
            return "Skipped", "No PowerSchool credentials configured"
        grades = ps_refresh_cache()
        return "Cached", f"Parsed {len(grades)} courses into local client registry"
    return _timed("powerschool", "pschool.grades.cached", run)


def sync_whoop():
    """Pull the latest WHOOP recovery/sleep/strain snapshot."""
    def run():
        if not _whoop_connected():
            return "Skipped", "WHOOP not connected"
        _whoop_clear_cache()
        summary = whoop_daily_summary(days=1)
        today = (summary or {}).get("today") or {}
        bits = []
        for key, label in (("hrv", "hrv"), ("resting_hr", "rhr"), ("recovery", "score")):
            if today.get(key) is not None:
                bits.append(f"{label}={today[key]}")
        return "200 OK", " · ".join(bits) or "Snapshot refreshed"
    return _timed("whoop", "whoop.recovery.updated", run)


def run_full_pipeline():
    """Run every configured connector once. Serialised so two runs can't overlap."""
    if not _sync_run_lock.acquire(blocking=False):
        return {"ok": False, "error": "A pipeline run is already in progress"}
    try:
        results = {
            "canvas": sync_canvas(),
            "powerschool": sync_powerschool(),
            "whoop": sync_whoop(),
        }
        return {"ok": True, "results": results}
    finally:
        _sync_run_lock.release()


def _connector_state(name):
    """Configured/connected state plus the last recorded run for one connector."""
    if name == "canvas":
        configured = bool(u_canvas_ical())
        connected = configured
        label = "Canvas LMS Feed"
    elif name == "powerschool":
        configured = _ps_configured()
        connected = configured
        label = "PowerSchool Gradebook"
    else:
        configured = _whoop_configured()
        connected = _whoop_connected()
        label = "WHOOP Biometrics"
    with _sync_last_run_lock:
        last = dict(_sync_last_run.get(name) or {})
    job = scheduler.get_job(f"sync_{name}") if scheduler.running else None
    return {
        "name": name,
        "label": label,
        "configured": configured,
        "connected": connected,
        "last_run": last or None,
        "next_run": job.next_run_time.isoformat() if job and job.next_run_time else None,
    }


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
    valid_users = [ADMIN_USER, AVERAGE_USER, "admin", "user"]
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
        is_env_student = (username == AVERAGE_USER)

        if is_admin:
            expected_password = ADMIN_PASSWORD
        elif is_env_student:
            expected_password = APP_PASSWORD
        else:
            expected_password = None

        # Try DB-backed student auth for non-admin users
        if not is_admin:
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

        if is_admin:
            expected_password = ADMIN_PASSWORD
        else:
            expected_password = APP_PASSWORD

        # Allow login with security code if:
        # 1. System is in lockdown, OR
        # 2. IP is blocked
        if is_locked_down or ip_is_blocked:
            log.warning(
                "Login security code attempt: username=%s, is_admin=%s, ip_blocked=%s, locked_down=%s",
                username, is_admin, ip_is_blocked, is_locked_down,
            )
            if expected_password and secrets.compare_digest(password.strip(), expected_password) and secrets.compare_digest(security_code.strip(), security_code_env):
                record_login_attempt(ip_addr, True, username)
                session.permanent = True
                if is_admin:
                    session["admin_authenticated"] = True
                else:
                    session["authenticated"] = True
                session.modified = True

                if is_admin:
                    redirect_url = "/admin"
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
    return jsonify({"csrf_token": _ensure_session_csrf_token()})


@app.route("/api/sync-status")
def api_sync_status():
    """Report which calendar feeds last failed to fetch and when."""
    label_for = {
        u_canvas_ical(): "Canvas",
        u_personal_ical(): "Personal",
        u_sports_ical(): "Sports",
        RED_DAY_ICAL_URL: "Red Day",
        WHITE_DAY_ICAL_URL: "White Day",
    }
    label_for.pop("", None)  # ignore unconfigured feeds
    cutoff = datetime.now(TZ) - timedelta(hours=6)
    issues = []
    with _ical_sync_lock:
        snapshot = dict(_ical_last_error)
    for url, info in snapshot.items():
        # Only surface errors for feeds currently configured for this user; a
        # stale error for a replaced URL must not keep sticking in the banner.
        if url not in label_for:
            continue
        try:
            at = datetime.fromisoformat(info["at"])
        except Exception:
            continue
        if at < cutoff:
            continue
        issues.append({
            "feed": label_for[url],
            "at": info["at"],
            "message": info.get("msg", ""),
        })
    return jsonify({
        "issues": issues,
        "connectors": [_connector_state(c) for c in CONNECTORS],
        "scheduler_running": bool(scheduler.running),
        "last_scheduler_error": _scheduler_last_error_get(),
    })


@app.route("/api/sync/events")
def api_sync_events():
    """Recent connector activity, newest first — the Sync & Feeds audit trail."""
    try:
        limit = max(1, min(int(request.args.get("limit", 25)), 200))
    except (TypeError, ValueError):
        limit = 25
    rows = []
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            "SELECT connector, event, status, detail, duration_ms, created_at "
            "FROM sync_events ORDER BY created_at DESC LIMIT %s",
            (limit,),
        )
        for r in cur.fetchall():
            created = r["created_at"]
            rows.append({
                "connector": r["connector"],
                "event": r["event"],
                "status": r["status"],
                "detail": r["detail"] or "",
                "duration_ms": r["duration_ms"] or 0,
                "at": created.isoformat() if hasattr(created, "isoformat") else str(created),
            })
        cur.close()
        conn.close()
    except Exception as e:
        log.warning("/api/sync/events failed: %s", e)
    return jsonify({"events": rows})


@app.route("/api/sync/run", methods=["POST"])
def api_sync_run():
    """Run one connector, or the whole pipeline when no connector is named."""
    data = request.get_json(silent=True) or {}
    which = str(data.get("connector", "")).strip().lower()
    if which and which not in CONNECTORS:
        return jsonify({"error": f"unknown connector: {which}"}), 400
    if which:
        ok = {"canvas": sync_canvas, "powerschool": sync_powerschool, "whoop": sync_whoop}[which]()
        return jsonify({"ok": ok, "results": {which: ok}})
    result = run_full_pipeline()
    return (jsonify(result), 200) if result.get("ok") else (jsonify(result), 409)


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
    for ev in events:
        ev.setdefault("category", _SOURCE_CATEGORY.get(ev.get("source", ""), "general"))
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
    """Health check: database reachable, API key present, connectors configured."""
    has_db = True
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT 1")
        cur.close()
        conn.close()
    except Exception:
        has_db = False
    return jsonify({
        "has_api_key": bool(get_config().get("anthropic_api_key", "") or os.environ.get("ANTHROPIC_API_KEY", "")),
        "has_db": has_db,
        "canvas_configured": bool(u_canvas_ical()),
        "powerschool_configured": _ps_configured(),
        "whoop_connected": _whoop_connected(),
        "scheduler_running": bool(scheduler.running),
        "timezone": str(TZ),
        "now": datetime.now(TZ).isoformat(),
    })


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
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({"status": "ok"})
    except Exception as e:
        log.exception("Error submitting assignment")
        return jsonify({"error": str(e)}), 500


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


@app.route("/api/config", methods=["GET"])
def api_config_get():
    uid = _uid()
    cfg = get_user_config(uid) if uid else get_config()
    return jsonify({
        "name": cfg.get("name", "Jarvis"),
        "wake_time": cfg.get("wake_time", cfg.get("morning_briefing_time", "07:00")),
        "has_api_key": bool(cfg.get("anthropic_api_key", "")),
        "formal_signoff_name": cfg.get("formal_signoff_name", "Finley Thomas"),
        "timezone": cfg.get("timezone", "America/Denver"),
        # Calendar URLs (per-user)
        "personal_ical_url":     cfg.get("personal_ical_url", ""),
        "canvas_ical_url":       cfg.get("canvas_ical_url", ""),
        "canvas_api_token":      "••••••••" if cfg.get("canvas_api_token", "") else "",
        "canvas_base_url":       cfg.get("canvas_base_url", ""),
        "sports_ical_url":       cfg.get("sports_ical_url", ""),
    })


@app.route("/api/config", methods=["POST"])
def api_config_post():
    data = request.get_json(force=True) or {}
    allowed = {
        "name", "wake_time", "anthropic_api_key", "formal_signoff_name", "timezone",
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
        # A changed calendar URL should retry right away, not sit out the
        # failure back-off from the previous (possibly broken) URL. While we're
        # here, probe each changed feed so a dead/expired link (a reset Canvas
        # feed 404s, etc.) is reported to the student at save time instead of
        # silently importing nothing.
        warnings = []
        for _ical_key in ("personal_ical_url", "canvas_ical_url",
                          "sports_ical_url", "job_schedule_ical_url"):
            if _ical_key in updates:
                _ical_forget(updates[_ical_key])
                problem = _validate_ical_url(updates[_ical_key])
                if problem:
                    warnings.append(f"{ICAL_FEED_LABELS[_ical_key]} calendar {problem}.")
        if warnings:
            return jsonify({"status": "ok", "warnings": warnings})
    return jsonify({"status": "ok"})


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

def schedule_jobs():
    """Register the data-sync jobs. Cadences mirror the Sync & Feeds page."""
    scheduler.remove_all_jobs()
    # Canvas iCal is cheap — poll it often so assignments stay current.
    scheduler.add_job(sync_canvas, "interval", minutes=15,
                      id="sync_canvas", replace_existing=True)
    # PowerSchool drives a headless browser, so run it only when grades move:
    # before school and just after the last period.
    scheduler.add_job(sync_powerschool, "cron", day_of_week="mon-fri", hour="7,15", minute=12,
                      id="sync_powerschool", replace_existing=True)
    # WHOOP updates recovery overnight and strain through the day.
    scheduler.add_job(sync_whoop, "interval", minutes=30,
                      id="sync_whoop", replace_existing=True)
    scheduler.add_job(cleanup_old_data, "cron", hour=2, minute=30,
                      id="cleanup_old_data", replace_existing=True)
    log.info("sync jobs registered (canvas 15m, powerschool weekdays 07:12/15:12, whoop 30m)")


# Guard: only start the scheduler in the first/main worker.
# With gunicorn --workers 1 this always runs. With multiple workers it only runs
# in the first gunicorn worker (SERVER_SOFTWARE is set before fork).
try:
    _worker_id = os.environ.get("GUNICORN_WORKER_ID", "0")
    if not _SKIP_BOOT and _worker_id in ("", "0", "1"):
        schedule_jobs()
        scheduler.start()
        # Warm the caches so the first page load isn't a cold fetch.
        threading.Thread(target=run_full_pipeline, daemon=True).start()
        log.info("Background scheduler started")
except Exception as e:
    log.warning(f"Background scheduler failed to start: {e}")


# ── WHOOP OAuth2 routes ────────────────────────────────────────────────────────

@app.route("/whoop-auth/start")
def whoop_auth_start():
    if not session.get("authenticated"):
        return redirect("/login")
    if not _whoop_configured():
        return "Set WHOOP_CLIENT_ID and WHOOP_CLIENT_SECRET environment variables first.", 400
    redirect_uri = WHOOP_REDIRECT_URI or request.url_root.rstrip("/") + "/whoop-auth/callback"
    state = secrets.token_urlsafe(24)
    session["whoop_oauth_state"] = state
    set_config({"whoop_oauth_pending_state": state})
    params = {
        "response_type": "code",
        "client_id": WHOOP_CLIENT_ID,
        "redirect_uri": redirect_uri,
        "scope": WHOOP_SCOPES,
        "state": state,
    }
    return redirect(WHOOP_AUTH_URL + "?" + urlencode(params))


@app.route("/whoop-auth/callback")
def whoop_auth_callback():
    if not session.get("authenticated"):
        return redirect("/login")
    incoming_state = request.args.get("state")
    state = session.pop("whoop_oauth_state", None) or get_config().get("whoop_oauth_pending_state", "")
    set_config({"whoop_oauth_pending_state": ""})
    if not state or state != incoming_state:
        return "OAuth state mismatch — please try the connection again from /whoop-auth/start.", 400
    code = request.args.get("code")
    if not code:
        return redirect("/?whoop_error=no_code")
    redirect_uri = WHOOP_REDIRECT_URI or request.url_root.rstrip("/") + "/whoop-auth/callback"
    try:
        resp = requests.post(WHOOP_TOKEN_URL, data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": WHOOP_CLIENT_ID,
            "client_secret": WHOOP_CLIENT_SECRET,
        }, timeout=12)
        resp.raise_for_status()
        data = resp.json()
        refresh_token = data.get("refresh_token")
        if not refresh_token:
            log.error("WHOOP OAuth callback: no refresh_token returned — authorization incomplete")
            return redirect("/?whoop_error=no_refresh_token")
        set_config({
            "whoop_refresh_token": refresh_token,
            "whoop_access_token": data.get("access_token", ""),
            "whoop_token_expires_at": str(time.time() + float(data.get("expires_in", 3600))),
        })
        _whoop_clear_cache()
        log.info("WHOOP refresh token stored successfully")
        return redirect("/?whoop_connected=1")
    except Exception as e:
        log.error("whoop_auth_callback error: %s", e)
        return f"WHOOP OAuth error: {e}", 500


@app.route("/api/whoop/status")
def whoop_auth_status():
    if not session.get("authenticated"):
        return jsonify({"error": "Not authenticated"}), 401
    configured = _whoop_configured()
    has_token = bool(configured and get_config().get("whoop_refresh_token", "").strip())
    authorized = bool(has_token and _get_whoop_access_token())
    return jsonify({
        "configured": configured,
        "has_token": has_token,
        "authorized": authorized,
        "auth_url": "/whoop-auth/start" if configured and not authorized else None,
    })


@app.route("/api/whoop/summary")
def api_whoop_summary():
    if not session.get("authenticated"):
        return jsonify({"error": "Not authenticated"}), 401
    days, is_mock = fitness_daily_summary(7)
    return jsonify({
        "configured": _whoop_configured(),
        "connected": _whoop_connected(),
        "mock": is_mock,
        "days": days,
    })


@app.route("/api/whoop/disconnect", methods=["POST"])
def whoop_disconnect():
    if not session.get("authenticated"):
        return jsonify({"error": "Not authenticated"}), 401
    set_config({"whoop_refresh_token": "", "whoop_access_token": "", "whoop_token_expires_at": ""})
    _whoop_clear_cache()
    return jsonify({"status": "disconnected"})


# ── Health & Fitness dashboard API ────────────────────────────────────────────

@app.route("/api/whoop/workouts")
def api_whoop_workouts():
    if not session.get("authenticated"):
        return jsonify({"error": "Not authenticated"}), 401
    workouts, is_mock = fitness_workouts(limit=25)
    return jsonify({
        "configured": _whoop_configured(),
        "connected": _whoop_connected(),
        "mock": is_mock,
        "workouts": workouts,
    })


@app.route("/api/whoop/heart-rate")
def api_whoop_heart_rate():
    if not session.get("authenticated"):
        return jsonify({"error": "Not authenticated"}), 401
    return jsonify(fitness_heart_rate())


@app.route("/api/whoop/bedtime")
def api_whoop_bedtime():
    if not session.get("authenticated"):
        return jsonify({"error": "Not authenticated"}), 401
    return jsonify({
        "configured": _whoop_configured(),
        "connected": _whoop_connected(),
        **whoop_bedtime_recommendation(),
    })


@app.route("/api/fitness/prs", methods=["GET"])
def api_fitness_prs():
    if not session.get("authenticated"):
        return jsonify({"error": "Not authenticated"}), 401
    workouts, is_mock = fitness_workouts(limit=25)
    prs = compute_personal_records(workouts)
    # Manual overrides (or records predating the WHOOP history) win.
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT record_key, label, value_display, value_numeric, achieved_on FROM personal_records")
        for r in cur.fetchall():
            prs[r["record_key"]] = {
                "label": r["label"],
                "value_display": r["value_display"],
                "value_numeric": r["value_numeric"],
                "achieved_on": str(r["achieved_on"]) if r["achieved_on"] else None,
                "manual": True,
            }
        cur.close()
        conn.close()
    except Exception as e:
        log.warning("/api/fitness/prs: override lookup failed: %s", e)
    for key, label in PR_LABELS.items():
        prs.setdefault(key, {"label": label, "value_display": "—", "value_numeric": None, "achieved_on": None})
    return jsonify({"records": prs, "mock": is_mock})


@app.route("/api/fitness/prs", methods=["POST"])
def api_fitness_prs_set():
    if not session.get("authenticated"):
        return jsonify({"error": "Not authenticated"}), 401
    data = request.get_json(force=True) or {}
    key = str(data.get("record_key", "")).strip()
    if key not in PR_LABELS:
        return jsonify({"error": "invalid record_key"}), 400
    label = PR_LABELS[key]
    value_display = str(data.get("value_display", "")).strip()[:60]
    if not value_display:
        return jsonify({"error": "value_display required"}), 400
    achieved_on = data.get("achieved_on") or None
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
INSERT INTO personal_records (record_key, user_id, label, value_display, value_numeric, achieved_on, updated_at)
VALUES (%s, %s, %s, %s, %s, %s, NOW())
ON CONFLICT (record_key) DO UPDATE
SET value_display = EXCLUDED.value_display, value_numeric = EXCLUDED.value_numeric,
    achieved_on = EXCLUDED.achieved_on, updated_at = NOW()""",
                (key, _uid(), label, value_display, data.get("value_numeric"), achieved_on))
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({"status": "ok"})


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
    data = request.get_json(force=True, silent=True) or {}
    code     = str(data.get("code", "")).strip().upper()
    email    = str(data.get("email", "")).strip().lower()
    username = str(data.get("username", "")).strip()
    password = str(data.get("password", "")).strip()

    if not all([code, email, username, password]):
        return jsonify({"error": "All fields are required"}), 400
    if len(username) < 3 or len(password) < 6:
        return jsonify({"error": "Username must be ≥3 chars and password ≥6 chars"}), 400
    if not _basic_email_ok(email):
        return jsonify({"error": "Invalid email"}), 400

    user_id = str(uuid.uuid4())
    conn = get_db(); cur = conn.cursor()
    try:
        # Lock the access_codes row so two parallel signups can't both claim it
        cur.execute(
            "SELECT id, bypass_payment, redeemed_by, expires_at "
            "FROM access_codes WHERE code = %s FOR UPDATE",
            (code,),
        )
        ac = cur.fetchone()
        if not ac or ac["redeemed_by"]:
            return jsonify({"error": "Invalid or already-used access code"}), 400
        if ac["expires_at"] and ac["expires_at"] < datetime.now(TZ):
            return jsonify({"error": "Access code expired"}), 400
        if not ac["bypass_payment"]:
            return jsonify({"error": "This code requires payment. Use the paid signup."}), 400

        cur.execute("SELECT id FROM users WHERE username = %s OR email = %s", (username, email))
        if cur.fetchone():
            return jsonify({"error": "Username or email already taken"}), 409

        try:
            cur.execute("""
INSERT INTO users (id, email, username, password_hash, display_name, is_comped, active)
VALUES (%s, %s, %s, %s, %s, TRUE, TRUE)""",
                (user_id, email, username, generate_password_hash(password), username.title()))
        except psycopg2.IntegrityError:
            conn.rollback()
            return jsonify({"error": "Username or email already taken"}), 409
        cur.execute(
            "UPDATE access_codes SET redeemed_by = %s, redeemed_at = NOW() WHERE id = %s",
            (user_id, ac["id"]),
        )
        conn.commit()
    finally:
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
    data = request.get_json(force=True, silent=True) or {}
    try:
        count = max(1, min(int(data.get("count", 1) or 1), 20))
    except (TypeError, ValueError):
        return jsonify({"error": "Count must be a number"}), 400
    bypass = bool(data.get("bypass_payment", False))
    notes = str(data.get("notes", ""))[:200]
    expires_at = None
    expires_days_raw = data.get("expires_days")
    if expires_days_raw not in (None, "", 0, "0"):
        try:
            expires_days = int(expires_days_raw)
        except (TypeError, ValueError):
            return jsonify({"error": "Expires-in days must be a number"}), 400
        if expires_days < 1:
            return jsonify({"error": "Expires-in days must be ≥ 1"}), 400
        expires_at = datetime.now(TZ) + timedelta(days=expires_days)

    conn = get_db(); cur = conn.cursor()
    try:
        created = []
        for _ in range(count):
            # Retry a few times if we hit a collision; 24 bits → vanishingly rare
            for _attempt in range(5):
                code = "JARVIS-" + secrets.token_hex(3).upper()
                cur.execute("""
INSERT INTO access_codes (code, bypass_payment, expires_at, notes)
VALUES (%s, %s, %s, %s) ON CONFLICT (code) DO NOTHING RETURNING id, code""",
                    (code, bypass, expires_at, notes))
                row = cur.fetchone()
                if row:
                    created.append({"id": str(row["id"]), "code": row["code"], "bypass_payment": bypass})
                    break
        conn.commit()
    finally:
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
    if not email or "@" not in email:
        return False
    local, _, domain = email.rpartition("@")
    if not local or "." not in domain:
        return False
    head, _, tld = domain.rpartition(".")
    return bool(head) and len(tld) >= 2


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
            return jsonify({"error": "An account with this email already exists"}), 409

        cur.execute(
            "SELECT id FROM access_requests WHERE email = %s AND status = 'pending'",
            (email,),
        )
        if cur.fetchone():
            return jsonify({"status": "ok", "message": "Request already pending"})

        cur.execute(
            "INSERT INTO access_requests (name, email, message, status) "
            "VALUES (%s, %s, %s, 'pending')",
            (name, email, message),
        )
        conn.commit()
    finally:
        cur.close(); conn.close()


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

    conn = get_db(); cur = conn.cursor()
    try:
        cur.execute(
            "SELECT id, email, name, status, token, token_used "
            "FROM access_requests WHERE id = %s FOR UPDATE",
            (req_id,),
        )
        row = cur.fetchone()
        if not row:
            return jsonify({"error": "Request not found"}), 404

        if row["status"] == "approved" and row["token"] and not row["token_used"]:
            # Idempotent: the previously shared link is still valid, return it as-is
            token = row["token"]
            email = row["email"]
            name = row["name"]
            conn.commit()
        else:
            token = secrets.token_urlsafe(32)
            cur.execute(
                "UPDATE access_requests SET status='approved', token=%s, token_used=FALSE, "
                "reviewed_at=NOW(), reviewed_by='admin' WHERE id = %s",
                (token, req_id),
            )
            conn.commit()
            email = row["email"]
            name = row["name"]
    finally:
        cur.close(); conn.close()

    approval_url = f"{request.host_url.rstrip('/')}/signup/complete?token={token}"
    return jsonify({
        "status": "ok",
        "token": token,
        "approval_url": approval_url,
        "name": name,
        "email": email,
    })


@app.route("/api/admin/access-requests/<int:req_id>/deny", methods=["POST"])
def api_admin_access_request_deny(req_id):
    if not session.get("admin_authenticated"):
        return jsonify({"error": "Unauthorized"}), 401
    conn = get_db(); cur = conn.cursor()
    try:
        # Only deny pending requests — won't clobber an already-shared approval link
        cur.execute(
            "UPDATE access_requests SET status='denied', reviewed_at=NOW(), "
            "reviewed_by='admin' WHERE id = %s AND status = 'pending' RETURNING email, name",
            (req_id,),
        )
        row = cur.fetchone()
        conn.commit()
    finally:
        cur.close(); conn.close()

    if not row:
        return jsonify({"error": "Request not found or not pending"}), 404

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
    if not _basic_email_ok(email):
        return jsonify({"error": "Invalid email"}), 400

    conn = get_db(); cur = conn.cursor()
    try:
        # Lock the access_request row so two parallel submits with the same
        # token can't both pass validation
        cur.execute(
            "SELECT id, email, status, token_used FROM access_requests "
            "WHERE token = %s FOR UPDATE",
            (token,),
        )
        ar = cur.fetchone()
        if not ar or ar["status"] != "approved" or ar["token_used"]:
            return jsonify({"error": "Invalid or already-used approval token"}), 400
        if (ar["email"] or "").lower() != email:
            return jsonify({"error": "Email does not match the approved request"}), 400

        cur.execute("SELECT id FROM users WHERE username = %s OR email = %s", (username, email))
        if cur.fetchone():
            return jsonify({"error": "Username or email already taken"}), 409
        conn.commit()  # release the row lock; Stripe round-trip below takes longer than we want to hold it
    finally:
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

    # No Stripe: create the user directly and burn the token in a single tx.
    user_id = str(uuid.uuid4())
    conn = get_db(); cur = conn.cursor()
    try:
        # Atomic token claim: only succeeds if the token is still unused
        cur.execute(
            "UPDATE access_requests SET token_used = TRUE WHERE token = %s "
            "AND status = 'approved' AND token_used = FALSE",
            (token,),
        )
        if cur.rowcount == 0:
            conn.rollback()
            return jsonify({"error": "Invalid or already-used approval token"}), 400
        try:
            cur.execute("""
INSERT INTO users (id, email, username, password_hash, display_name, is_comped, active)
VALUES (%s, %s, %s, %s, %s, TRUE, TRUE)""",
                (user_id, email, username, generate_password_hash(password), username.title()))
        except psycopg2.IntegrityError:
            conn.rollback()
            return jsonify({"error": "Username or email already taken"}), 409
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
