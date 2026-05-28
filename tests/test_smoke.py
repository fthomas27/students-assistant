"""Smoke tests for the Flask app.

These run without a live database. We monkeypatch get_db() to a stub so
auth / CSRF / route wiring can be exercised in isolation.
"""

import os
import sys
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


def test_chat_system_prompt_has_cache_block(client):
    """Inspect the api_chat handler to ensure the cache_control block is wired."""
    _, flask_app = client
    src = open(flask_app.__file__).read()
    assert 'cache_control' in src
    assert '"type": "ephemeral"' in src
    assert "system_static" in src
    assert "system_dynamic" in src


def test_tasks_get_includes_all_project_tasks(client, monkeypatch):
    """All project tasks from active projects should sync into /api/tasks,
    regardless of assignee, and include project_id/project_title linkage."""
    c, flask_app = client

    now = datetime(2026, 5, 6, 12, 0, 0)

    class StubCursor(FakeCursor):
        def __init__(self):
            super().__init__()
            self._call = 0
            self._rows = []

        def execute(self, sql, *_args, **_kwargs):
            self._call += 1
            sql_l = (sql or "").lower()
            if "from tasks" in sql_l and "project_tasks" not in sql_l:
                self._rows = [
                    {
                        "id": 1, "title": "Regular task", "notes": "",
                        "urgency": "low", "completed": False,
                        "completed_at": None, "due_date": None,
                        "created_at": now, "project_id": None,
                        "project_title": None, "hidden_from_parent": False,
                    },
                ]
            elif "from project_tasks" in sql_l:
                self._rows = [
                    {
                        "id": 10, "title": "Linked PT (assignee=me)",
                        "notes": "", "urgency": "medium", "completed": False,
                        "completed_at": None, "due_date": None,
                        "created_at": now, "project_id": 7,
                        "assignee": "me", "project_title": "Science Fair",
                        "hidden_from_parent": False,
                    },
                    {
                        "id": 11, "title": "Linked PT (assignee=teammate)",
                        "notes": "", "urgency": "medium", "completed": False,
                        "completed_at": None, "due_date": None,
                        "created_at": now, "project_id": 7,
                        "assignee": "Alex", "project_title": "Science Fair",
                        "hidden_from_parent": False,
                    },
                    {
                        "id": 12, "title": "Linked PT (no assignee)",
                        "notes": "", "urgency": "medium", "completed": False,
                        "completed_at": None, "due_date": None,
                        "created_at": now, "project_id": 7,
                        "assignee": "", "project_title": "Science Fair",
                        "hidden_from_parent": False,
                    },
                ]
            else:
                self._rows = []

        def fetchall(self):
            return list(self._rows)

    class StubConn(FakeConn):
        def cursor(self):
            return StubCursor()

    monkeypatch.setattr(flask_app, "get_db", lambda: StubConn())
    with c.session_transaction() as s:
        s["authenticated"] = True

    resp = c.get("/api/tasks")
    assert resp.status_code == 200
    data = resp.get_json()
    tasks = data["tasks"]

    project_tasks = [t for t in tasks if t.get("source") == "project_task"]
    regular_tasks = [t for t in tasks if t.get("source") == "task"]

    # All three project tasks should sync, regardless of assignee
    assert len(project_tasks) == 3, f"expected 3 project tasks, got {project_tasks}"
    assert len(regular_tasks) == 1

    assignees = {t["title"]: t.get("assignee", "") for t in project_tasks}
    assert "Linked PT (assignee=me)" in assignees
    assert "Linked PT (assignee=teammate)" in assignees
    assert "Linked PT (no assignee)" in assignees

    # Each project task preserves linkage back to its project
    for t in project_tasks:
        assert t["project_id"] == 7
        assert t["project_title"] == "Science Fair"


def test_tasks_get_query_has_no_assignee_filter(client):
    """Guard against regressing the project-task sync to assignee-only.

    A previous version filtered project tasks to assignee IN ('me','finn');
    the sync should now surface every project task on an active project.
    """
    _, flask_app = client
    src = open(flask_app.__file__).read()
    # Locate the api_tasks_get handler and inspect only its body
    idx = src.find("def api_tasks_get(")
    assert idx > 0
    end = src.find("\n@app.route", idx)
    body = src[idx:end if end > 0 else len(src)]
    assert "FROM project_tasks pt" in body
    assert "LOWER(pt.assignee) IN" not in body, (
        "api_tasks_get should not filter project tasks by assignee — "
        "every project task on an active project must sync"
    )


def test_pomodoro_state_default(client, monkeypatch):
    c, flask_app = client

    class StubCursor(FakeCursor):
        def fetchone(self):
            return {
                "id": 1,
                "estimate_minutes": 25.0,
                "started_at": None,
                "paused_at": None,
                "accumulated_seconds": 0,
                "active": False,
            }

    class StubConn(FakeConn):
        def cursor(self):
            return StubCursor()

    monkeypatch.setattr(flask_app, "get_db", lambda: StubConn())
    with c.session_transaction() as s:
        s["authenticated"] = True
    resp = c.get("/api/pomodoro/state")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["active"] is False
    assert data["estimate_minutes"] == 25.0


def test_briefing_locks_are_independent(client):
    """Briefing, debrief, and weekly insight should not contend on a single lock."""
    _, flask_app = client
    assert flask_app._briefing_lock is not flask_app._debrief_lock
    assert flask_app._briefing_lock is not flask_app._weekly_insight_lock
    assert flask_app._debrief_lock is not flask_app._weekly_insight_lock


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
    # variables (personal_url, sports_url, job_url, canvas_url), NOT call
    # u_*_ical() directly inside the closure body.
    idx = src.find("def api_calendar(")
    assert idx > 0
    end = src.find("\n@app.route", idx)
    body = src[idx:end if end > 0 else len(src)]
    assert "personal_url = u_personal_ical()" in body
    assert "canvas_url   = u_canvas_ical()" in body or "canvas_url = u_canvas_ical()" in body
    # The worker functions must not call u_*_ical() directly.
    for marker in ("def get_personal():", "def get_sports():", "def get_job():", "def get_canvas():"):
        m_idx = body.find(marker)
        assert m_idx > 0, f"missing {marker}"
        # Look at the next ~6 lines for direct u_*_ical() calls
        snippet = body[m_idx:m_idx + 400]
        assert "u_personal_ical()" not in snippet
        assert "u_sports_ical()" not in snippet
        assert "u_job_schedule_ical()" not in snippet
        assert "u_canvas_ical()" not in snippet

    # /api/daily-outlook — same pattern.
    idx = src.find("def api_daily_outlook(")
    assert idx > 0
    end = src.find("\n@app.route", idx)
    body = src[idx:end if end > 0 else len(src)]
    assert "canvas_url" in body and "personal_url" in body
    for marker in ("def _get_assignments():", "def _get_events():"):
        m_idx = body.find(marker)
        assert m_idx > 0, f"missing {marker}"
        snippet = body[m_idx:m_idx + 600]
        assert "u_canvas_ical()" not in snippet
        assert "u_personal_ical()" not in snippet


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
    monkeypatch.setattr(flask_app, "u_job_schedule_ical", lambda: "")

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
