"""Static tenant-isolation audit of app.py.

Every SQL statement touching a user-data table must reference user_id on the
same logical statement (or sit under an explicit escape-hatch comment):

- ``# GLOBAL-OK``  — intentionally global (aggregates, age-based cleanup).
- ``# PHASE5``     — scheduler-context query awaiting the per-user job loop.

The exemption counts are pinned so a new unscoped query fails this test
instead of silently shipping a cross-tenant leak.
"""

import os
import re

APP_PATH = os.path.join(os.path.dirname(__file__), "..", "app.py")

USER_TABLES = [
    "tasks", "recurring_tasks", "completions", "assignment_estimates",
    "projects", "project_tasks", "project_notes", "daily_plans",
    "daily_plan_items", "timer_state", "briefing_cache", "debrief_cache",
    "insight_cache", "chat_messages", "chat_summaries", "stock_transactions",
    "stock_notes", "bucket_list", "people_profiles", "gmail_drafts",
    "notification_log", "canvas_assignments_cache", "news_preferences",
    "planned_workouts", "personal_records",
]

# Tables whose sub-resources are scoped via an ownership check on the parent
# (project_id / plan_id verified against the user before the query runs).
PARENT_SCOPED_HINTS = ("project_id", "plan_id")

QUERY_RE = re.compile(
    r"(FROM|INTO|UPDATE|DELETE\s+FROM|JOIN)\s+(" + "|".join(USER_TABLES) + r")\b"
)
DDL_HINTS = ("CREATE TABLE", "CREATE INDEX", "CREATE UNIQUE INDEX",
             "ALTER TABLE", "CREATE SEQUENCE", "DROP CONSTRAINT", "setval", "ADD COLUMN")


def _statements():
    """Yield (start_line, text) for each cur.execute(...) style statement,
    approximated as the query line plus the 6 lines around it."""
    with open(APP_PATH) as f:
        lines = f.readlines()
    for i, line in enumerate(lines):
        if not QUERY_RE.search(line):
            continue
        if any(h in line for h in DDL_HINTS):
            continue
        lo = max(0, i - 6)
        hi = min(len(lines), i + 7)
        yield i + 1, "".join(lines[lo:hi])


def test_every_user_table_query_is_scoped_or_exempted():
    unscoped = []
    global_ok = 0
    phase5 = 0
    for lineno, ctx in _statements():
        if any(h in ctx for h in DDL_HINTS):
            continue
        if "user_id" in ctx or any(h in ctx for h in PARENT_SCOPED_HINTS):
            continue
        if "GLOBAL-OK" in ctx:
            global_ok += 1
            continue
        if "PHASE5" in ctx:
            phase5 += 1
            continue
        unscoped.append(lineno)

    assert not unscoped, (
        "Unscoped user-table queries at app.py lines %s — add a user_id filter "
        "or an explicit # GLOBAL-OK / # PHASE5 comment." % unscoped
    )


def test_exemption_counts_are_pinned():
    """A growing GLOBAL-OK/PHASE5 count means someone added a new global
    query — review it, then update these pins deliberately."""
    src = open(APP_PATH).read()
    assert src.count("# GLOBAL-OK") <= 5, "New GLOBAL-OK exemption added — review for tenant leaks"
    assert src.count("# PHASE5") <= 8, "New PHASE5 deferral added — should scheduler work be per-user already?"
