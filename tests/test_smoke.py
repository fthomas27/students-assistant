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

os.environ.setdefault("FLASK_BOOT_DEV", "1")
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
