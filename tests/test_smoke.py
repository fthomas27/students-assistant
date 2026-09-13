"""Smoke tests for the Flask app.

These run without a live database. We monkeypatch get_db() to a stub so
auth / CSRF / route wiring can be exercised in isolation.
"""

import os
import sys
import time
import types
from datetime import datetime
from unittest import mock

import pytest
import requests

os.environ.setdefault("FLASK_BOOT_DEV", "1")
os.environ.setdefault("FLASK_SKIP_BOOT", "1")
os.environ.setdefault("DATABASE_URL", "postgresql://stub:stub@localhost/stub")

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


class FakeCursor:
    def __init__(self):
        self._row = None

    def execute(self, *_args, **_kwargs):
        self._row = None

    def fetchone(self):
        return self._row

    def fetchall(self):
        return []

    def close(self):
        pass


class FakeConn:
    def __init__(self):
        self.closed = False

    def cursor(self):
        return FakeCursor()

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        self.closed = True


@pytest.fixture
def client():
    with mock.patch("psycopg2.pool.ThreadedConnectionPool"):
        import app as flask_app  # noqa: WPS433

    flask_app.app.config["TESTING"] = True
    flask_app.app.config["SESSION_COOKIE_SECURE"] = False
    flask_app.get_db = lambda: FakeConn()

    with flask_app.app.test_client() as c:
        yield c, flask_app


def test_root_redirects_when_unauthenticated(client):
    c, _ = client
    resp = c.get("/")
    assert resp.status_code in (302, 301)
    assert "/login" in resp.headers.get("Location", "")


def test_login_page_renders(client):
    c, _ = client
    resp = c.get("/login")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True).lower()
    assert "<title" in body


def test_csrf_token_endpoint(client):
    c, _ = client
    with c.session_transaction() as s:
        s["authenticated"] = True
    resp = c.get("/api/csrf-token")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data and "csrf_token" in data and len(data["csrf_token"]) > 16


def test_post_without_csrf_token_returns_403(client):
    c, _ = client
    with c.session_transaction() as s:
        s["authenticated"] = True
    resp = c.post("/api/tasks", json={"title": "test"})
    assert resp.status_code == 403


def test_authenticated_get_mints_csrf_token_into_session(client):
    """An authenticated page-navigation GET must seed csrf_token into the
    session so the cookie carries it before the page's parallel API burst —
    this is what prevents the refresh-each-request cookie race that was
    intermittently breaking CSRF (and thus settings saves)."""
    c, _ = client
    with c.session_transaction() as s:
        s["authenticated"] = True
        s.pop("csrf_token", None)
    # /login renders without auth; use /api/sync-status which is a plain
    # authenticated GET that flows through the before_request hooks.
    resp = c.get("/api/sync-status")
    assert resp.status_code == 200
    with c.session_transaction() as s:
        token = s.get("csrf_token")
    assert token and len(token) > 16
    # The freshly minted token must now satisfy CSRF on a state-changing POST.
    # POST /api/config with no recognised fields returns ok without touching
    # the DB, so this isolates the CSRF check from storage.
    resp = c.post("/api/config", json={}, headers={"X-CSRF-Token": token})
    assert resp.status_code == 200
    # And the same POST without the header is still rejected.
    resp = c.post("/api/config", json={})
    assert resp.status_code == 403


def test_csrf_token_stable_across_repeated_fetches(client):
    """Repeated csrf-token fetches must return the *same* token for a session,
    so a token cached by the client stays valid for later POSTs."""
    c, _ = client
    with c.session_transaction() as s:
        s["authenticated"] = True
        s.pop("csrf_token", None)
    first = c.get("/api/csrf-token").get_json()["csrf_token"]
    second = c.get("/api/csrf-token").get_json()["csrf_token"]
    assert first and first == second


def test_manifest_served(client):
    c, _ = client
    resp = c.get("/manifest.json")
    assert resp.status_code == 200
    assert "manifest" in resp.headers.get("Content-Type", "")


def test_service_worker_served(client):
    c, _ = client
    resp = c.get("/sw.js")
    assert resp.status_code == 200
    assert "javascript" in resp.headers.get("Content-Type", "")
    body = resp.get_data(as_text=True)
    assert "serviceWorker" not in body  # the SW itself doesn't reference navigator
    assert "addEventListener" in body


def test_admin_login_uses_constant_time_compare(client):
    """Admin login source should not use raw == on password/security_code values."""
    _, flask_app = client
    src = open(flask_app.__file__).read()
    # No bare equality on the secret values themselves remains in the source.
    assert "password.strip() == ADMIN_PASSWORD" not in src
    assert "password.strip() == APP_PASSWORD" not in src
    assert "security_code.strip() == security_code_env" not in src
    # And constant-time comparisons are present.
    assert "secrets.compare_digest" in src


def test_no_password_or_security_code_hash_logging(client):
    """Sensitive token hashes should not be written to the log stream."""
    _, flask_app = client
    src = open(flask_app.__file__).read()
    for needle in ("password_hash=", "admin_hash=", "received_hash=", "env_hash="):
        assert needle not in src, f"Sensitive hash log marker still present: {needle}"


def test_calendar_urls_resolved_outside_worker_threads(client):
    """Per-user calendar URLs must be resolved in the request thread before
    being passed into ThreadPoolExecutor workers. Flask's `session` is bound to
    the request thread, so calling u_*_ical() inside a worker silently falls
    back to (usually empty) env vars and the user's saved settings are ignored.
    """
    _, flask_app = client
    src = open(flask_app.__file__).read()

    # /api/calendar — the worker closures should reference the pre-resolved
    # variables (personal_url, sports_url, canvas_url), NOT call
    # u_*_ical() directly inside the closure body.
    idx = src.find("def api_calendar(")
    assert idx > 0
    end = src.find("\n@app.route", idx)
    body = src[idx:end if end > 0 else len(src)]
    assert "personal_url = u_personal_ical()" in body
    assert "canvas_url   = u_canvas_ical()" in body or "canvas_url = u_canvas_ical()" in body
    # The worker functions must not call u_*_ical() directly.
    for marker in ("def get_personal():", "def get_sports():", "def get_canvas():"):
        m_idx = body.find(marker)
        assert m_idx > 0, f"missing {marker}"
        # Look at the next ~6 lines for direct u_*_ical() calls
        snippet = body[m_idx:m_idx + 400]
        assert "u_personal_ical()" not in snippet
        assert "u_sports_ical()" not in snippet
        assert "u_canvas_ical()" not in snippet


def test_uid_safe_outside_request_context(client):
    """_uid() must return None instead of raising when called from a worker
    thread (no Flask request context). Defensive against silently breaking
    per-user features that touch session in background work."""
    import threading
    _, flask_app = client

    captured = {}

    def worker():
        try:
            captured["value"] = flask_app._uid()
            captured["raised"] = False
        except Exception as e:
            captured["raised"] = True
            captured["error"] = repr(e)

    t = threading.Thread(target=worker)
    t.start()
    t.join(timeout=2.0)
    assert captured.get("raised") is False, (
        f"_uid() should not raise outside request context, got: {captured.get('error')}"
    )
    assert captured.get("value") is None


def test_request_access_missing_fields(client):
    """POST /api/signup/request-access with missing name/email should 400."""
    c, _ = client
    with c.session_transaction() as s:
        s["csrf_token"] = "tt"
    resp = c.post(
        "/api/signup/request-access",
        json={"name": "", "email": ""},
        headers={"X-CSRF-Token": "tt"},
    )
    assert resp.status_code == 400
    data = resp.get_json()
    assert data and "error" in data


def test_request_access_success(client, monkeypatch):
    """Valid POST should return 200 and insert a row."""
    c, flask_app = client
    inserts = []

    class StubCursor(FakeCursor):
        def execute(self, sql, params=None, *_a, **_kw):
            sql_l = (sql or "").lower()
            self._row = None
            if "from users where email" in sql_l:
                self._row = None  # no existing user
            elif "from access_requests where email" in sql_l:
                self._row = None  # no pending dup
            elif "insert into access_requests" in sql_l:
                inserts.append(params)

        def fetchone(self):
            return self._row

    class StubConn(FakeConn):
        def cursor(self):
            return StubCursor()

    monkeypatch.setattr(flask_app, "get_db", lambda: StubConn())
    with c.session_transaction() as s:
        s["csrf_token"] = "tt"
    resp = c.post(
        "/api/signup/request-access",
        json={"name": "Ada", "email": "ada@example.com", "message": "hi"},
        headers={"X-CSRF-Token": "tt"},
    )
    assert resp.status_code == 200, resp.get_data(as_text=True)
    data = resp.get_json()
    assert data and data.get("status") == "ok"
    assert len(inserts) == 1
    assert inserts[0][0] == "Ada"
    assert inserts[0][1] == "ada@example.com"


def test_admin_access_requests_requires_auth(client):
    """GET /api/admin/access-requests with no admin session should 401."""
    c, _ = client
    resp = c.get("/api/admin/access-requests")
    assert resp.status_code == 401


def test_admin_approve_deny(client, monkeypatch):
    """With admin session, approve sets a token; deny sets status=denied."""
    c, flask_app = client

    class StubCursor(FakeCursor):
        def __init__(self):
            super().__init__()
            self._row = None
            self.rowcount = 1

        def execute(self, sql, params=None, *_a, **_kw):
            sql_l = (sql or "").lower()
            if "select id, email, name, status, token, token_used from access_requests" in sql_l:
                # approve's lookup — pretend the row is pending so we hit the UPDATE branch
                self._row = {
                    "id": 1, "email": "ada@example.com", "name": "Ada",
                    "status": "pending", "token": None, "token_used": False,
                }
            elif "update access_requests set status='denied'" in sql_l:
                self._row = {"email": "ada@example.com", "name": "Ada"}
            else:
                self._row = None

        def fetchone(self):
            return self._row

    class StubConn(FakeConn):
        def cursor(self):
            return StubCursor()

    monkeypatch.setattr(flask_app, "get_db", lambda: StubConn())

    with c.session_transaction() as s:
        s["admin_authenticated"] = True
        s["csrf_token"] = "test-csrf-token"

    hdrs = {"X-CSRF-Token": "test-csrf-token"}

    # Approve
    resp = c.post("/api/admin/access-requests/1/approve", headers=hdrs)
    assert resp.status_code == 200, resp.get_data(as_text=True)
    data = resp.get_json()
    assert data and data.get("status") == "ok"
    assert data.get("token") and len(data["token"]) > 16
    assert data.get("approval_url") and "/signup/complete?token=" in data["approval_url"]

    # Deny
    resp = c.post("/api/admin/access-requests/2/deny", headers=hdrs)
    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert resp.get_json().get("status") == "ok"


def test_admin_approve_is_idempotent(client, monkeypatch):
    """Approving an already-approved-not-used request must return the same token."""
    c, flask_app = client
    existing_token = "preexisting-token-abcdef0123456789"

    class StubCursor(FakeCursor):
        def __init__(self):
            super().__init__()
            self._row = None
            self.updates = []

        def execute(self, sql, params=None, *_a, **_kw):
            sql_l = (sql or "").lower()
            if "select id, email, name, status, token, token_used from access_requests" in sql_l:
                self._row = {
                    "id": 1, "email": "ada@example.com", "name": "Ada",
                    "status": "approved", "token": existing_token, "token_used": False,
                }
            elif "update access_requests set status='approved'" in sql_l:
                self.updates.append(params)
                self._row = None
            else:
                self._row = None

        def fetchone(self):
            return self._row

    cursors = []

    class StubConn(FakeConn):
        def cursor(self):
            c = StubCursor(); cursors.append(c); return c

    monkeypatch.setattr(flask_app, "get_db", lambda: StubConn())

    with c.session_transaction() as s:
        s["admin_authenticated"] = True
        s["csrf_token"] = "tt"

    resp = c.post("/api/admin/access-requests/1/approve", headers={"X-CSRF-Token": "tt"})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["token"] == existing_token
    # Crucially, no UPDATE was issued on a still-valid approval
    assert all(not c.updates for c in cursors)


def test_complete_signup_invalid_token(client):
    """GET /signup/complete?token=bad should not crash; should render the page."""
    c, _ = client
    resp = c.get("/signup/complete?token=definitely-not-a-real-token")
    # Should render the signup page with an error banner — must not 500.
    assert resp.status_code in (200, 302)


def test_reduced_motion_styles_present(client):
    """Primary templates should respect the prefers-reduced-motion media query."""
    c, _ = client
    for path in ("/login",):
        body = c.get(path).get_data(as_text=True)
        assert "prefers-reduced-motion" in body


def _reset_ical_state(flask_app):
    flask_app._ical_cache.clear()
    flask_app._ical_neg_cache.clear()
    with flask_app._ical_sync_lock:
        flask_app._ical_last_error.clear()


def _http_error_get(status, counter):
    """A requests.get stand-in that always raises an HTTPError of `status`."""
    def fake_get(*_args, **_kwargs):
        counter["n"] += 1
        resp = mock.Mock()
        resp.status_code = status
        resp.content = b""

        def raise_for_status():
            err = requests.exceptions.HTTPError(f"{status} error")
            err.response = resp
            raise err

        resp.raise_for_status = raise_for_status
        return resp
    return fake_get


def test_fetch_ical_skips_retry_and_backs_off_on_permanent_404(client):
    """A stale Canvas feed (404) must be fetched once, then served from the
    negative cache — not re-fetched on every request (the 404-storm bug)."""
    _, flask_app = client
    _reset_ical_state(flask_app)
    url = "https://example.test/canvas-stale-feed.ics"
    calls = {"n": 0}
    with mock.patch.object(flask_app.requests, "get", side_effect=_http_error_get(404, calls)), \
         mock.patch.object(flask_app.time, "sleep") as sleep_mock:
        assert flask_app.fetch_ical(url) is None
        assert calls["n"] == 1                 # no retry on a permanent 404
        sleep_mock.assert_not_called()         # and no 1.5s back-off sleep
        assert url in flask_app._ical_neg_cache
        # A second call inside the back-off window must NOT hit the network.
        assert flask_app.fetch_ical(url) is None
        assert calls["n"] == 1


def test_fetch_ical_retries_transient_server_error(client):
    """A 5xx is transient, so the existing single-retry behavior is preserved."""
    _, flask_app = client
    _reset_ical_state(flask_app)
    url = "https://example.test/transient-500.ics"
    calls = {"n": 0}
    with mock.patch.object(flask_app.requests, "get", side_effect=_http_error_get(500, calls)), \
         mock.patch.object(flask_app.time, "sleep"):
        assert flask_app.fetch_ical(url) is None
        assert calls["n"] == 2                 # transient → retried once


def test_sync_status_only_reports_configured_feeds(client, monkeypatch):
    """An error logged for a since-replaced URL must not stick in the banner."""
    c, flask_app = client
    canvas_url = "https://canvas.example/current-feed.ics"
    old_url = "https://canvas.example/OLD-replaced-feed.ics"
    monkeypatch.setattr(flask_app, "u_canvas_ical", lambda: canvas_url)
    monkeypatch.setattr(flask_app, "u_personal_ical", lambda: "")
    monkeypatch.setattr(flask_app, "u_sports_ical", lambda: "")

    now_iso = datetime.now(flask_app.TZ).isoformat()
    with flask_app._ical_sync_lock:
        flask_app._ical_last_error.clear()
        flask_app._ical_last_error[canvas_url] = {"at": now_iso, "msg": "404 Not Found"}
        flask_app._ical_last_error[old_url] = {"at": now_iso, "msg": "404 Not Found"}

    with c.session_transaction() as sess:
        sess["authenticated"] = True
    resp = c.get("/api/sync-status")
    assert resp.status_code == 200
    feeds = [i["feed"] for i in resp.get_json()["issues"]]
    assert "Canvas" in feeds                   # the configured feed is reported
    assert "Calendar" not in feeds             # the replaced URL is dropped


def _mock_get_response(status_code, text):
    resp = mock.Mock()
    resp.status_code = status_code
    resp.text = text
    return resp


def test_validate_ical_url_flags_404(client):
    _, flask_app = client
    with mock.patch.object(flask_app.requests, "get",
                           return_value=_mock_get_response(404, "")):
        problem = flask_app._validate_ical_url("https://canvas.example/dead.ics")
    assert problem and "404" in problem


def test_validate_ical_url_flags_non_calendar_body(client):
    _, flask_app = client
    with mock.patch.object(flask_app.requests, "get",
                           return_value=_mock_get_response(200, "<html>nope</html>")):
        problem = flask_app._validate_ical_url("https://example.test/notacal")
    assert problem and "calendar" in problem.lower()


def test_validate_ical_url_accepts_good_feed(client):
    _, flask_app = client
    body = "BEGIN:VCALENDAR\nVERSION:2.0\nEND:VCALENDAR\n"
    with mock.patch.object(flask_app.requests, "get",
                           return_value=_mock_get_response(200, body)):
        assert flask_app._validate_ical_url("https://example.test/good.ics") is None


def test_validate_ical_url_empty_is_ok(client):
    _, flask_app = client
    assert flask_app._validate_ical_url("") is None
    assert flask_app._validate_ical_url("not-a-url") is not None


def test_config_post_returns_warning_for_dead_canvas_feed(client, monkeypatch):
    """Saving a 404ing Canvas feed should still persist but warn the student."""
    c, flask_app = client
    monkeypatch.setattr(flask_app, "set_user_config", lambda *a, **k: None)
    monkeypatch.setattr(flask_app, "set_config", lambda *a, **k: None)
    with c.session_transaction() as s:
        s["authenticated"] = True
        s["csrf_token"] = "tok"
    with mock.patch.object(flask_app.requests, "get",
                           return_value=_mock_get_response(404, "")):
        resp = c.post("/api/config",
                      json={"canvas_ical_url": "https://canvas.example/dead.ics"},
                      headers={"X-CSRF-Token": "tok"})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["status"] == "ok"
    assert data.get("warnings") and any("Canvas" in w for w in data["warnings"])


# ── CalDAV event write path ───────────────────────────────────────────────────
# iCloud's event_by_uid() prop-filter REPORT is unreliable (notably on shared
# family calendars): it reports "not found" for events a plain listing returns
# fine. These build real caldav.Event objects over a fake calendar that
# reproduces that behaviour.

UFC_ICS = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Test//EN
BEGIN:VEVENT
UID:ufc-fight-1234
SUMMARY:UFC Fight
DTSTART;TZID=America/Denver:20260810T140000
DTEND;TZID=America/Denver:20260810T150000
LOCATION:Old Place
DTSTAMP:20260808T000000Z
END:VEVENT
END:VCALENDAR
"""

ALLDAY_ICS = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Test//EN
BEGIN:VEVENT
UID:allday-999
SUMMARY:Family Trip
DTSTART;VALUE=DATE:20260812
DTEND;VALUE=DATE:20260813
DTSTAMP:20260808T000000Z
END:VEVENT
END:VCALENDAR
"""


def _saved_lines(cal, prefix):
    return [l for data in cal.saved for l in data.splitlines()
            if l.startswith(prefix)]


def _telegram_webhook_post(c, flask_app, message):
    """POST an update to the webhook with the per-install secret header."""
    secret = "s" * 32
    flask_app.get_config = lambda: {
        "telegram_webhook_secret": secret,
        "telegram_chat_id": "12345",
        "telegram_last_update_id": "0",
    }
    flask_app.set_config = lambda *_a, **_k: None
    return c.post(
        "/api/webhooks/telegram",
        json={"update_id": 999, "message": message},
        headers={"X-Telegram-Bot-Api-Secret-Token": secret},
    )


def _run_telegram_turn(flask_app, user_text, attachments, history):
    """Drive one _telegram_run_jarvis turn with the network fully stubbed.
    Returns (messages_sent_to_api, persisted_rows, replies)."""
    sent, persisted, replies = {}, [], []

    class FakeMessages:
        def create(self, **kwargs):
            sent.update(kwargs)
            return _FakeResponse("Noted, sir.")

    class FakeClient:
        def __init__(self, **_kw):
            self.beta = types.SimpleNamespace(messages=FakeMessages())

    with mock.patch.object(flask_app.anthropic, "Anthropic", FakeClient), \
         mock.patch.object(flask_app, "_telegram_api", return_value=None), \
         mock.patch.object(flask_app, "_telegram_history_messages", return_value=history), \
         mock.patch.object(flask_app, "_chat_recent_summaries", return_value=[]), \
         mock.patch.object(flask_app, "_build_active_tools", return_value=[]), \
         mock.patch.object(flask_app, "_google_configured", return_value=False), \
         mock.patch.object(flask_app, "_caldav_configured", return_value=False), \
         mock.patch.object(flask_app, "get_config", return_value={}), \
         mock.patch.object(flask_app, "_telegram_download_file", return_value=b"%PDF-1.4 x"), \
         mock.patch.object(flask_app, "_chat_persist_message",
                           side_effect=lambda _c, role, content: persisted.append((role, content))), \
         mock.patch.object(flask_app, "_telegram_send_chunked",
                           side_effect=lambda _c, t: replies.append(t)), \
         mock.patch.dict(flask_app.os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
        flask_app._telegram_run_jarvis(user_text, "12345", attachments=attachments)

    return sent.get("messages"), persisted, replies


def _ics(summaries):
    """Minimal iCal document with one future VEVENT per summary."""
    from datetime import datetime, timedelta, timezone
    due = (datetime.now(timezone.utc) + timedelta(days=3)).strftime("%Y%m%dT%H%M%SZ")
    out = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//test//EN"]
    for i, s in enumerate(summaries):
        out += ["BEGIN:VEVENT", f"UID:u{i}@test", f"DTSTAMP:{due}",
                f"DTSTART:{due}", f"SUMMARY:{s}", "END:VEVENT"]
    out.append("END:VCALENDAR")
    return "\r\n".join(out) + "\r\n"


def test_canvas_parser_splits_bracketed_course_name(client):
    """Canvas publishes 'Title [Course]'. The course must land in class_name,
    not get glued onto the title."""
    from icalendar import Calendar
    _, flask_app = client
    cal = Calendar.from_ical(_ics(["Lab Report: Titration Curves [AP Chemistry]"]))
    out = flask_app.parse_canvas_assignments(cal)
    assert len(out) == 1
    assert out[0]["title"] == "Lab Report: Titration Curves"
    assert out[0]["class_name"] == "AP Chemistry"


def test_canvas_parser_still_splits_dash_form(client):
    from icalendar import Calendar
    _, flask_app = client
    cal = Calendar.from_ical(_ics(["Chapter 12 Quiz - AP US History"]))
    out = flask_app.parse_canvas_assignments(cal)
    assert out[0]["title"] == "Chapter 12 Quiz"
    assert out[0]["class_name"] == "AP US History"


def test_canvas_parser_leaves_plain_summary_alone(client):
    from icalendar import Calendar
    _, flask_app = client
    cal = Calendar.from_ical(_ics(["Read chapters 4-6"]))
    out = flask_app.parse_canvas_assignments(cal)
    assert out[0]["title"] == "Read chapters 4-6"
    assert out[0]["class_name"] == ""


def test_ps_login_helpers_are_defined(client):
    """_ps_md5 / _ps_session_cache / _ps_session_lock were referenced by the
    PowerSchool login path but never defined — a guaranteed NameError."""
    _, flask_app = client
    assert flask_app._ps_md5("abc") == "900150983cd24fb0d6963f7d28e17f72"
    assert set(flask_app._ps_session_cache) == {"session", "home_url", "expires"}
    assert flask_app._ps_session_lock is not None


def test_sync_run_rejects_unknown_connector(client):
    c, flask_app = client
    with c.session_transaction() as sess:
        sess["authenticated"] = True
        sess["csrf_token"] = "tok"
    r = c.post("/api/sync/run", json={"connector": "nope"}, headers={"X-CSRF-Token": "tok"})
    assert r.status_code == 400


def test_connector_state_reports_all_three(client):
    _, flask_app = client
    assert flask_app.CONNECTORS == ("canvas", "powerschool", "whoop")
    for name in flask_app.CONNECTORS:
        st = flask_app._connector_state(name)
        assert set(["name", "label", "configured", "connected", "last_run", "next_run"]) <= set(st)


# ── Canvas password login ─────────────────────────────────────────────────────

_CANVAS_LOGIN_HTML = (
    '<html><body><form id="login_form" action="/login/canvas" method="post">'
    '<input name="authenticity_token" value="tok-abc">'
    '<input name="pseudonym_session[unique_id]">'
    '<input type="password" name="pseudonym_session[password]">'
    '</form></body></html>'
)


class _Resp:
    def __init__(self, text="", status=200, json_data=None, url="https://pcsd.instructure.com/"):
        self.text = text
        self.status_code = status
        self._json = json_data
        self.url = url

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))


class _FakeCanvasSession:
    """Stands in for requests.Session during a Canvas login."""

    def __init__(self, post_result, api_json=None):
        self.headers = {}
        self.cookies = {}
        self._post_result = post_result
        self._api_json = api_json
        self.posted = None
        self.gets = []

    def get(self, url, **kw):
        self.gets.append(url)
        if "/login/canvas" in url:
            return _Resp(_CANVAS_LOGIN_HTML, url=url)
        return _Resp("", json_data=self._api_json if self._api_json is not None else [], url=url)

    def post(self, url, data=None, **kw):
        self.posted = data
        return self._post_result


def _canvas_setup(flask_app, monkeypatch, session_obj):
    monkeypatch.setattr(flask_app, "u_canvas_base_url", lambda: "https://pcsd.instructure.com")
    monkeypatch.setattr(flask_app, "u_canvas_api_token", lambda: "")
    monkeypatch.setattr(flask_app, "u_canvas_username", lambda: "someone@example.com")
    monkeypatch.setattr(flask_app, "u_canvas_password", lambda: "pw")
    monkeypatch.setattr(flask_app.requests, "Session", lambda: session_obj)
    flask_app._canvas_invalidate_session()


def test_canvas_auth_mode_prefers_token_then_password(client, monkeypatch):
    _, flask_app = client
    monkeypatch.setattr(flask_app, "u_canvas_base_url", lambda: "https://x.instructure.com")
    monkeypatch.setattr(flask_app, "u_canvas_api_token", lambda: "t")
    monkeypatch.setattr(flask_app, "u_canvas_username", lambda: "u")
    monkeypatch.setattr(flask_app, "u_canvas_password", lambda: "p")
    assert flask_app._canvas_auth_mode() == "token"
    monkeypatch.setattr(flask_app, "u_canvas_api_token", lambda: "")
    assert flask_app._canvas_auth_mode() == "password"
    monkeypatch.setattr(flask_app, "u_canvas_password", lambda: "")
    assert flask_app._canvas_auth_mode() is None


def test_canvas_login_posts_authenticity_token_and_credentials(client, monkeypatch):
    _, flask_app = client
    sess = _FakeCanvasSession(_Resp("<html>Dashboard</html>", url="https://pcsd.instructure.com/?login_success=1"))
    sess.cookies = {"canvas_session": "abc"}
    _canvas_setup(flask_app, monkeypatch, sess)
    out = flask_app._canvas_login()
    assert out is sess
    assert sess.posted["authenticity_token"] == "tok-abc"
    assert sess.posted["pseudonym_session[unique_id]"] == "someone@example.com"
    assert sess.posted["pseudonym_session[password]"] == "pw"


def test_canvas_login_detects_bad_credentials(client, monkeypatch):
    """A rejected login re-renders the login form; that must not read as success."""
    _, flask_app = client
    sess = _FakeCanvasSession(_Resp(_CANVAS_LOGIN_HTML))
    _canvas_setup(flask_app, monkeypatch, sess)
    assert flask_app._canvas_login() is None
    assert "login page" in flask_app._canvas_last_login_error["message"]


def test_canvas_login_reports_missing_session_cookie(client, monkeypatch):
    _, flask_app = client
    sess = _FakeCanvasSession(_Resp("<html>Somewhere else</html>"))
    sess.cookies = {}
    _canvas_setup(flask_app, monkeypatch, sess)
    assert flask_app._canvas_login() is None
    assert "canvas_session" in flask_app._canvas_last_login_error["message"]


def test_canvas_grades_keeps_observer_enrollments(client, monkeypatch):
    """A view-only account is an ObserverEnrollment. Filtering to
    StudentEnrollment would silently return no grades at all."""
    _, flask_app = client
    monkeypatch.setattr(flask_app, "canvas_courses",
                        lambda: [{"id": 1, "name": "AP Chemistry"}, {"id": 2, "name": "AP Physics C"}])
    monkeypatch.setattr(flask_app, "_canvas_get", lambda *a, **k: [
        {"course_id": 1, "type": "ObserverEnrollment",
         "grades": {"current_score": 94.2, "current_grade": "A"}},
        {"course_id": 2, "type": "ObserverEnrollment", "grades": {}},
    ])
    with flask_app._simple_cache_lock:
        flask_app._simple_cache.pop("canvas:grades", None)
    out = flask_app.canvas_grades()
    assert len(out) == 1
    assert out[0]["course"] == "AP Chemistry"
    assert out[0]["current_score"] == 94.2
    assert out[0]["enrollment_type"] == "ObserverEnrollment"


def test_canvas_get_retries_once_when_session_expires(client, monkeypatch):
    """An expired Canvas session returns the login page with a 200. That must
    invalidate and re-login exactly once, not loop."""
    _, flask_app = client
    calls = {"n": 0}

    class Expiring(_FakeCanvasSession):
        def get(self, url, **kw):
            if "/login/canvas" in url:
                return _Resp(_CANVAS_LOGIN_HTML, url=url)
            calls["n"] += 1
            return _Resp(_CANVAS_LOGIN_HTML, url=url)  # always looks logged out

    sess = Expiring(_Resp("<html>ok</html>"))
    sess.cookies = {"canvas_session": "abc"}
    _canvas_setup(flask_app, monkeypatch, sess)
    assert flask_app._canvas_get("/api/v1/users/self/enrollments") is None
    assert calls["n"] == 2  # original attempt plus exactly one retry


# ── Canvas /grades HTML parsing ───────────────────────────────────────────────

_GRADES_PAGE = """
<html><body>
<table class="course_details student_grades">
  <thead><tr><th>Course</th><th>Term</th><th>Enrolled as</th><th>Grades</th></tr></thead>
  <tbody>
    <tr>
      <td><a href="/courses/1021/grades/55">AP Chemistry</a></td>
      <td>Fall 2025</td><td>Observer</td>
      <td><span class="percent">94.2%</span> <span class="letter_grade">A</span></td>
    </tr>
    <tr>
      <td><a href="/courses/1022/grades/55">AP US History</a></td>
      <td>Fall 2025</td><td>Observer</td>
      <td><span class="percent">88.6%</span> <span class="letter_grade">B+</span></td>
    </tr>
    <tr>
      <td><a href="/courses/1023/grades/55">Advisory</a></td>
      <td>Fall 2025</td><td>Observer</td>
      <td><span class="percent">N/A</span></td>
    </tr>
  </tbody>
</table>
</body></html>
"""


def test_canvas_grades_page_parses_courses_and_scores(client):
    _, flask_app = client
    out = flask_app._canvas_parse_grades_html(_GRADES_PAGE)
    assert len(out) == 2, "the ungraded Advisory row must be dropped"
    assert out[0]["course"] == "AP Chemistry"
    assert out[0]["course_id"] == 1021
    assert out[0]["current_score"] == 94.2
    assert out[0]["current_grade"] == "A"
    assert out[1]["current_grade"] == "B+"


def test_canvas_grades_page_tolerates_different_markup(client):
    """Parsing keys off /courses/<id> links and row text, not class names, so a
    differently themed Canvas still works."""
    _, flask_app = client
    html = """<table><tr>
        <td><a href="https://x.instructure.com/courses/77">Physics C</a></td>
        <td>current score: 91.5% (A-)</td></tr></table>"""
    out = flask_app._canvas_parse_grades_html(html)
    assert len(out) == 1
    assert out[0]["course"] == "Physics C"
    assert out[0]["course_id"] == 77
    assert out[0]["current_score"] == 91.5
    assert out[0]["current_grade"] == "A-"


def test_canvas_grades_page_ignores_rows_without_a_course_link(client):
    _, flask_app = client
    html = "<table><tr><th>Course</th><th>Grades</th></tr><tr><td>Totals</td><td>92%</td></tr></table>"
    assert flask_app._canvas_parse_grades_html(html) == []


def test_canvas_grades_prefers_html_over_api(client, monkeypatch):
    """/grades reflects the observed student for a view-only login; the
    self-scoped API may not, so HTML wins when it returns anything."""
    _, flask_app = client
    monkeypatch.setattr(flask_app, "_canvas_get_html", lambda p, **k: _GRADES_PAGE)
    monkeypatch.setattr(flask_app, "canvas_courses", lambda: [])
    called = {"api": False}

    def _no_api(*a, **k):
        called["api"] = True
        return []
    monkeypatch.setattr(flask_app, "_canvas_get", _no_api)
    with flask_app._simple_cache_lock:
        flask_app._simple_cache.pop("canvas:grades", None)
    out = flask_app.canvas_grades()
    assert len(out) == 2
    assert called["api"] is False


def test_canvas_grades_falls_back_to_api_when_page_is_empty(client, monkeypatch):
    _, flask_app = client
    monkeypatch.setattr(flask_app, "_canvas_get_html", lambda p, **k: "<html></html>")
    monkeypatch.setattr(flask_app, "canvas_courses", lambda: [{"id": 5, "name": "Spanish IV"}])
    monkeypatch.setattr(flask_app, "_canvas_get", lambda *a, **k: [
        {"course_id": 5, "type": "ObserverEnrollment",
         "grades": {"current_score": 90.0, "current_grade": "A-"}}])
    with flask_app._simple_cache_lock:
        flask_app._simple_cache.pop("canvas:grades", None)
    out = flask_app.canvas_grades()
    assert len(out) == 1
    assert out[0]["course"] == "Spanish IV"


def test_sync_whoop_reads_the_day_list_not_a_dict(client, monkeypatch):
    """whoop_daily_summary returns a LIST of day dicts. Treating it as a dict
    made every WHOOP sync raise AttributeError and show as a connector error,
    while the Readiness page kept working off a different code path."""
    _, flask_app = client
    monkeypatch.setattr(flask_app, "_whoop_connected", lambda: True)
    monkeypatch.setattr(flask_app, "_whoop_clear_cache", lambda: None)
    monkeypatch.setattr(flask_app, "whoop_daily_summary", lambda days=7: [
        {"date": "2026-09-13", "recovery_score": 67, "hrv_ms": 68, "rhr": 55},
    ])
    recorded = {}
    monkeypatch.setattr(flask_app, "record_sync_event",
                        lambda c, e, status, detail="", duration_ms=0:
                            recorded.update(status=status, detail=detail))
    assert flask_app.sync_whoop() is True
    assert recorded["status"] == "200 OK"
    assert "hrv=68" in recorded["detail"]
    assert "score=67" in recorded["detail"]


def test_sync_whoop_handles_an_empty_history(client, monkeypatch):
    _, flask_app = client
    monkeypatch.setattr(flask_app, "_whoop_connected", lambda: True)
    monkeypatch.setattr(flask_app, "_whoop_clear_cache", lambda: None)
    monkeypatch.setattr(flask_app, "whoop_daily_summary", lambda days=7: [])
    recorded = {}
    monkeypatch.setattr(flask_app, "record_sync_event",
                        lambda c, e, status, detail="", duration_ms=0:
                            recorded.update(status=status, detail=detail))
    assert flask_app.sync_whoop() is True
    assert recorded["status"] == "200 OK"


_OBSERVER_GRADES_PAGE = """
<table class="course_details student_grades">
 <tr><td><a href="/courses/1/grades/9">Finley Thomas, SOCS AP US GOVERNMENT - Andres - YR ^</a></td>
     <td><span class="percent">82.59%</span></td></tr>
 <tr><td><a href="/courses/2/grades/9">Finley Thomas, AP STATISTICS (Monson) ^</a></td>
     <td><span class="percent">79.65%</span></td></tr>
 <tr><td><a href="/courses/3/grades/9">Finley Thomas, LANG SPANISH 3117 CE - Fernandez - YR</a></td>
     <td><span class="percent">75%</span></td></tr>
</table>
"""


def test_observer_page_strips_the_repeated_student_name(client):
    """An observer's /grades page prefixes every row with the observed
    student's name. That is not part of the course title."""
    _, flask_app = client
    out = flask_app._canvas_parse_grades_html(_OBSERVER_GRADES_PAGE)
    assert [r["course"] for r in out] == [
        "SOCS AP US GOVERNMENT - Andres - YR",
        "AP STATISTICS (Monson)",
        "LANG SPANISH 3117 CE - Fernandez - YR",
    ]


def test_letter_grade_derived_when_canvas_publishes_none(client):
    """Canvas only prints a letter when the course has a grading scheme. Derive
    one from the percentage, but flag it as derived."""
    _, flask_app = client
    out = flask_app._canvas_parse_grades_html(_OBSERVER_GRADES_PAGE)
    assert [(r["current_score"], r["current_grade"], r["grade_derived"]) for r in out] == [
        (82.59, "B-", True), (79.65, "C+", True), (75.0, "C", True),
    ]


def test_canvas_letter_wins_over_the_derived_one(client):
    _, flask_app = client
    html = ('<table><tr><td><a href="/courses/4">Chem</a></td>'
            '<td>91.0% A-</td></tr></table>')
    out = flask_app._canvas_parse_grades_html(html)
    assert out[0]["current_grade"] == "A-"
    assert out[0]["grade_derived"] is False


def test_letter_scale_boundaries(client):
    _, flask_app = client
    f = flask_app._letter_for_score
    assert (f(93), f(92.9), f(90), f(89.9)) == ("A", "A-", "A-", "B+")
    assert (f(60), f(59.9), f(0)) == ("D-", "F", "F")
    assert f(None) == ""


def test_single_row_page_keeps_its_course_name(client):
    """The shared-prefix rule needs 2+ rows to be safe; one row must not have
    its first comma-separated chunk eaten."""
    _, flask_app = client
    html = ('<table><tr><td><a href="/courses/9">Smith, John AP Chem</a></td>'
            '<td>88%</td></tr></table>')
    out = flask_app._canvas_parse_grades_html(html)
    assert out[0]["course"] == "Smith, John AP Chem"
