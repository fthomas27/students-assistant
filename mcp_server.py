"""MCP server exposing Schola Registry to an agent.

A thin client over the app's read-only agent API. It holds no database
connection and no Canvas credentials — it forwards to the deployed app with
AGENT_API_KEY, so the agent can never see anything the dashboard doesn't.

Run it over stdio:

    SCHOLA_BASE_URL=https://your-app  AGENT_API_KEY=...  python mcp_server.py

Every tool here is read-only; the upstream API refuses anything but GET.
"""

from __future__ import annotations

import os
import sys
from typing import Any

import requests
from mcp.server.mcpserver import MCPServer

BASE_URL = os.environ.get("SCHOLA_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
API_KEY = os.environ.get("AGENT_API_KEY", "")
TIMEOUT = float(os.environ.get("SCHOLA_TIMEOUT", "30"))

mcp = MCPServer(
    name="schola-registry",
    instructions=(
        "Academic dashboard for a high school student: Canvas course grades and "
        "assignments, calendar feeds, and WHOOP recovery/sleep/strain.\n\n"
        "Read-only. Call get_snapshot first for a whole picture; the narrower "
        "tools are for follow-ups.\n\n"
        "Two things to be careful about:\n"
        "- Readiness may be sample data. Check the `mock` flag before quoting a "
        "recovery or strain number as real; if it is true, say so.\n"
        "- A course may report a letter grade with no percentage, or a "
        "percentage with no letter. Do not convert between them — the school's "
        "grading scale is not the standard 10-point one."
    ),
)


def _today_in(tz_name: str | None) -> str:
    """Today's date in the student's timezone, for the due-today filter."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    try:
        return datetime.now(ZoneInfo(tz_name or "America/Denver")).date().isoformat()
    except Exception:
        return datetime.now().date().isoformat()


def _get(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """GET one agent endpoint, turning transport problems into plain messages.

    Errors are returned rather than raised so the agent gets something it can
    reason about and relay, instead of an opaque tool failure.
    """
    if not API_KEY:
        return {"error": "AGENT_API_KEY is not set for this MCP server."}
    try:
        r = requests.get(
            f"{BASE_URL}/api/agent{path}",
            headers={"Authorization": f"Bearer {API_KEY}", "Accept": "application/json"},
            params=params or {},
            timeout=TIMEOUT,
        )
    except requests.RequestException as e:
        return {"error": f"Could not reach Schola Registry at {BASE_URL}: {e}"}

    if r.status_code == 401:
        return {"error": "Schola Registry rejected the agent key (401)."}
    if r.status_code == 503:
        return {"error": "Schola Registry has no AGENT_API_KEY configured, so its agent API is off."}
    try:
        r.raise_for_status()
        return r.json()
    except ValueError:
        return {"error": f"Schola Registry returned a non-JSON response ({r.status_code})."}
    except requests.HTTPError as e:
        return {"error": f"Schola Registry error: {e}"}


@mcp.tool(
    description=(
        "Everything the dashboard knows, in one call: course grades, "
        "assignments with counts, upcoming calendar, WHOOP readiness, today's "
        "bell schedule and connector health. Start here."
    )
)
def get_snapshot() -> dict[str, Any]:
    return _get("/snapshot")


@mcp.tool(
    description=(
        "Course standing from Canvas. Each course carries current_grade (a "
        "letter) and/or current_score (a percentage); either may be null."
    )
)
def get_grades() -> dict[str, Any]:
    return _get("/grades")


@mcp.tool(
    description=(
        "Canvas assignments with due dates, time estimates, and whether each is "
        "done or overdue, plus counts for open/overdue/due-today. "
        "status filters the list: 'all', 'open', 'overdue' or 'due_today'."
    )
)
def get_assignments(status: str = "open") -> dict[str, Any]:
    data = _get("/assignments")
    # The upstream payload always carries an "error" key, often null — test the
    # value, not the key's presence.
    if data.get("error"):
        return data
    items = data.get("assignments", [])
    today = _today_in(data.get("timezone"))
    if status == "open":
        items = [a for a in items if not a.get("done")]
    elif status == "overdue":
        items = [a for a in items if a.get("overdue") and not a.get("done")]
    elif status == "due_today":
        items = [a for a in items
                 if not a.get("done") and (a.get("due_iso") or "")[:10] == today]
    elif status != "all":
        return {"error": "status must be one of: all, open, overdue, due_today"}
    return {"assignments": items, "counts": data.get("counts", {}),
            "filter": status, "timezone": data.get("timezone")}


@mcp.tool(
    description=(
        "Calendar events across every configured feed (Canvas assignments, "
        "personal, sports, school bell schedule) for the next N days."
    )
)
def get_calendar(days: int = 21) -> dict[str, Any]:
    return _get("/calendar", {"days": max(1, min(int(days), 90))})


@mcp.tool(
    description=(
        "WHOOP recovery, sleep, strain and recent workouts, plus a recommended "
        "bedtime. Check the 'mock' field: when true these are sample numbers, "
        "not the student's real data, and must not be quoted as real."
    )
)
def get_readiness() -> dict[str, Any]:
    return _get("/readiness")


@mcp.tool(
    description=(
        "Whether a given date is a school day, which rotation it is (Red or "
        "White), and the bell times. Defaults to today. Date is YYYY-MM-DD."
    )
)
def get_schedule(date: str = "") -> dict[str, Any]:
    return _get("/schedule", {"date": date} if date else None)


@mcp.tool(
    description=(
        "Health of the Canvas and WHOOP connectors and the most recent sync "
        "activity. Use this when data looks stale or missing."
    )
)
def get_sync_status() -> dict[str, Any]:
    return _get("/status")


if __name__ == "__main__":
    if not API_KEY:
        print("AGENT_API_KEY is not set; every tool will return an error.",
              file=sys.stderr)
    mcp.run(transport="stdio")
