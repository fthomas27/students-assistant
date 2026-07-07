# Students Assistant - Jarvis AI

An intelligent student assistant application powered by Claude AI that helps high school students manage assignments, tasks, and schedules with the persona of Jarvis from Iron Man.

## Multi-Tenant SaaS Architecture (READ FIRST)

This is a **multi-tenant SaaS**, not a single-user app. Every row of student data belongs to a user (`users.id`), and the codebase enforces strict tenant isolation. When editing `app.py`, follow these non-negotiable conventions:

- **Every query on a user-data table MUST filter by `user_id`.** Use `_require_uid()` (aborts 401 if no session user) in request handlers; pass `uid` explicitly into background/scheduler code. A static test (`tests/test_isolation_static.py`) fails the build if any query on a user table lacks `user_id` and lacks an explicit `# GLOBAL-OK` (intentional cross-tenant aggregate) or `# PHASE5` comment. Do not add new `# GLOBAL-OK` exemptions without care — the counts are pinned.
- **Bring-your-own key (BYOK):** every Claude call uses the *user's* Anthropic API key, resolved via `_user_api_key(uid)` / `_anthropic_client_for(uid)`. Never read `os.environ["ANTHROPIC_API_KEY"]` for tenant traffic. Missing/invalid keys raise `MissingApiKeyError` and surface to the UI via `error_code` (`no_api_key` / `bad_api_key` / `key_quota`).
- **Sensitive per-user config is encrypted at rest** (Fernet, `CONFIG_ENCRYPTION_KEY`). `SENSITIVE_CONFIG_KEYS` (API keys, OAuth tokens, PowerSchool password) are transparently encrypted by `set_user_config`/`get_user_config`.
- **Schools, not hardcoded Park City:** each user belongs to a `schools` row (timezone, bell schedule, year dates). Use `_school_for(uid)`, `user_tz(uid)`, and `_school_prompt_block(uid)` — never hardcode schedule facts. Park City High School is seeded; users create their school at signup if they're the first.
- **Per-user integrations:** WHOOP, Google, PowerSchool, Mem0, and ntfy notification topics are all keyed by user. Pass `uid` through their helpers.
- **Scheduler is multi-user:** background jobs iterate `_active_user_ids()` via `_for_each_active_user()`; one user's failure never stops the others. A single hourly tick (`hourly_user_tick`) fires briefings/debriefs/plans at each user's *local* hour.

See `DEPLOY.md` for the Railway + Stripe launch runbook.

## Overview

This Flask-based web application provides a comprehensive student management system featuring:

- **Daily Briefings** - Morning plans synthesizing assignments, calendar events, and tasks
- **Evening Debriefs** - End-of-day summaries of accomplishments and upcoming priorities
- **Intelligent Chat** - Conversational AI assistant with context awareness
- **Task Management** - Smart task creation, prioritization, and tracking
- **Schedule Optimization** - Automated daily schedule generation using available time windows
- **Calendar Integration** - Syncs with Canvas (assignments), personal calendars, and school events
- **WHOOP Integration** - Connects a WHOOP account (OAuth2) to surface recovery, sleep, and strain on the home dashboard and in Jarvis's chat/briefing context

## Tech Stack

- **Backend**: Python Flask with ProxyFix middleware for reverse proxy support
- **Database**: PostgreSQL with psycopg2 for data persistence
- **AI Engine**: Anthropic Claude API (Sonnet 4.6 model)
- **Calendar Parsing**: icalendar + recurring-ical-events
- **Scheduling**: APScheduler for background jobs (briefings, debriefs, recurring tasks)
- **Authentication**: Session-based with login/logout and admin controls

## Key Features

### 1. Briefing System
- **Morning Briefing** (7:00 AM by default): Generates today's priority list
  - Overdue and due-today assignments
  - Calendar events and schedule
  - Study prep for upcoming quizzes/tests
  - Pending tasks and projects
  
- **Evening Debrief** (6:30 PM): Summarizes the day
  - Accomplishments with productivity metrics
  - Time breakdown by class
  - Remaining work and tomorrow's outlook

### 2. Chat Interface
- Contextually aware assistant that knows:
  - Current date, time, and school schedule
  - Upcoming assignments from Canvas (titles + due dates from iCal; full descriptions/rubrics + live grades when Canvas REST API is configured)
  - Pending tasks and project work
  - Park City High School bell schedule (Red/White day rotation)
  - Student availability during/after school hours
  - Recall of recent prior conversations (server-side history + auto-summaries)
- **Live web access**: Anthropic-native `web_search` and `web_fetch` tools — Jarvis can look up current events, definitions, study material, or read any URL the student pastes
- **Streaming responses (SSE)**: replies stream token-by-token; tool calls (search, fetch, Canvas, task ops) surface as inline activity chips so the student sees what Jarvis is doing in real time
- **Persistent memory**: messages and rolling summaries are stored in `chat_messages` / `chat_summaries`; the server injects the 5 most recent prior-conversation summaries (and last few in-conversation messages on tab refresh) into every chat turn

### 3. Task Management
- **Manual Task Creation**: User-created pending tasks with urgency levels
- **Smart Suggestions**: Claude analyzes upcoming assignments and events to suggest new tasks
- **Recurring Tasks**: Daily processing at midnight maintains recurring task instances
- **Filtering**: Only suggests tasks not completed and due within 14 days

### 5. Schedule Planning
- **Free Window Detection**: Analyzes calendar to find available time slots
- **Smart Prioritization**: MUST-include assignments > critical tasks > medium tasks > projects
- **JSON-Based Scheduling**: Returns structured schedule items with exact time blocks

### 6. WHOOP Integration
- **OAuth2 Connect**: Student links their WHOOP account from Settings (`/whoop-auth/start` → `/whoop-auth/callback`); the refresh token is stored server-side and access tokens are refreshed automatically
- **Health dashboard data**: recovery/sleep/strain summary, recent workouts, and heart rate feed the Health & Fitness dashboard; a deterministic mock pipeline (same response shape) fills in when WHOOP is not connected
- **AI Context**: The latest recovery/sleep/strain snapshot is injected into `/api/chat`, the morning briefing, and the evening debrief so Jarvis can factor recovery into pacing advice

### 7. Multi-Dashboard Architecture
The UI is four dense, above-the-fold grid dashboards (each widget scrolls internally; the page itself does not scroll on desktop):
- **Home** — master aggregated view of *everything*: all calendar items (with category chips), upcoming tasks, health metrics, and project statuses
- **School** — academics only: active assignments, school tasks, and club/student-org tasks (`tasks.category` = `school` / `club`)
- **Health & Fitness** — WHOOP metrics (recovery/strain/sleep), recent heart rate, recent workouts, personal records tracker (longest run, fastest mile, longest swim, highest strain), and an interactive workout planner
- **Current Projects** — grid of project cards, each with its granular action items inline (complete/add tasks in place), plus project deadlines/milestones

**AI Calendar Categorization Engine** (`categorize_events` in app.py): every calendar item is routed to exactly one category — `school`, `health`, `projects`, or `general` — via persistent cache → deterministic source/keyword rules → Claude Haiku batch classification → `general` fallback. No item is ever dropped; Home shows everything regardless of category, sub-dashboards filter by it. Tasks are routed the same way via `categorize_task` (school/club/health/general), with manual override through `POST/PATCH /api/tasks` `category`.

## Database Schema

SaaS/multi-tenant tables:
- `users` - Accounts (UUID id, email, username, password_hash, is_comped, `school_id`)
- `user_config` - Per-user settings (name, timezone, API key, OAuth tokens, ntfy topic); sensitive values encrypted
- `schools` - Per-school schedule (timezone, bell schedules, year dates, day-type rotation, calendar feeds)
- `subscriptions` / `billing_events` / `pricing_config` / `access_codes` / `pending_signups` - Stripe billing + signup
- `config` - **Legacy global** key/value store; per-user settings live in `user_config`

Per-user data tables (all `user_id`-scoped, NOT NULL + FK to `users`):
- `tasks` - Pending user tasks with urgency, due dates, and dashboard `category`
- `calendar_categories` - Persistent cache for the calendar categorization engine
- `planned_workouts` - Workout planner entries (Health dashboard)
- `personal_records` - Manual PR overrides (longest run, fastest mile, longest swim, highest strain)
- `completions` - Logged task/assignment completions with time tracking
- `projects` - Active projects with status tracking
- `project_tasks` - Tasks within projects
- `project_notes` - Collaborative notes for projects
- `briefing_cache` - Cached morning briefing content
- `debrief_cache` - Cached evening debrief content
- `timer_state` - Timer state for work sessions
- `daily_plans` - Generated daily schedules
- `chat_messages` - Persisted chat history (per `conversation_id`)
- `chat_summaries` - Rolling 2-3 sentence summaries per conversation, used for cross-session recall

## Environment Variables

These are now **app-level only** — per-student settings (API keys, calendars, integration tokens, school) live in `user_config`/`schools`, set through signup and Settings. See `DEPLOY.md` for the full list.

Required:
- `DATABASE_URL` - PostgreSQL connection string
- `SECRET_KEY` - Flask session secret
- `CONFIG_ENCRYPTION_KEY` - Fernet key encrypting sensitive per-user config at rest (losing it bricks all stored keys/tokens)
- `STRIPE_SECRET_KEY` / `STRIPE_WEBHOOK_SECRET` - Stripe billing
- `ADMIN_USER` / `ADMIN_PASSWORD` - Break-glass admin login (boot refuses the default password)

Optional (app-level shared): `GOOGLE_CLIENT_ID/SECRET/REDIRECT_URI`, `WHOOP_CLIENT_ID/SECRET/REDIRECT_URI` (one OAuth app each; users connect their own accounts), `MEM0_API_KEY` (owner-paid), `NTFY_SERVER`, `FINNHUB_API_KEY`, `NOAA_API_TOKEN`, `GUARDIAN_API_KEY`, `PARENT_USER`/`PARENT_PASSWORD` (single-family parent portal).

**Removed / no longer used:** `ANTHROPIC_API_KEY` (BYOK — each user supplies their own), `APP_PASSWORD`, `AVERAGE_USER`, `NTFY_TOPIC`, `SECURITY_CODE`, `POWER_USERN`/`POWER_PASS`, all `*_ICAL_URL` and `CANVAS_*` (per-user now), `RED_DAY_ICAL_URL`/`WHITE_DAY_ICAL_URL` (seeded into the Park City school row).

## Park City School Specific

The system is configured for Park City High School (Utah) with:
- **School Year**: Aug 18, 2025 - Jun 5, 2026
- **Bell Schedule**: Red Day (7:30 AM-11:53 AM) vs. White Day (7:30 AM-2:25 PM)
- **Holiday Dates**: Built-in calendar for 2025-2026 with all breaks and holidays
- **Timezone**: Mountain Time (America/Denver)

## Jarvis Personality

All AI responses adopt the personality of Jarvis from Iron Man:
- Sophisticated and articulate communication
- Refined, professional tone with subtle wit
- Analytical and logical approach
- Respectful address of the student
- Reliable and competent assistance
- High intelligence reflected in vocabulary and phrasing

## API Endpoints

Key endpoints include:
- `POST /api/chat` - Chat with Jarvis
- `GET /api/briefing` - Today's morning briefing
- `GET /api/debrief` - Today's evening debrief
- `POST /api/tasks` - Create/manage tasks (accepts optional `category`: school/club/health/general)
- `GET /api/task-suggestions` - AI task suggestions
- `GET /api/calendar` - All calendar events, each with a routed `category`
- `GET /api/plan-my-day` - Generate daily schedule (used by chat flows)
- `GET /api/whoop/workouts` - Recent workouts (live WHOOP or mock pipeline; `mock` flag)
- `GET /api/whoop/heart-rate` - Recent/current heart-rate series
- `GET|POST /api/fitness/prs` - Personal records (computed from workouts + manual overrides)
- `GET|POST /api/fitness/planned-workouts` (+ `PATCH|DELETE /<id>`) - Workout planner CRUD

## Security Features

- Session-based authentication with httponly/secure/samesite cookies
- ProxyFix middleware for reverse proxy environments
- Admin login for sensitive operations
- IP-based lockdown controls for testing

## Development Notes

- Prompts are designed for JSON parsing where required (task suggestions, schedule generation)
- API usage is tracked for quota management
- Background scheduler manages recurring operations (briefings at 7 AM, debriefs at 6:30 PM)
- Lock mechanisms (`_briefing_lock`, `_timer_lock`, `_plan_lock`) prevent race conditions
- All times reference student's configured timezone (default: Mountain Time)
