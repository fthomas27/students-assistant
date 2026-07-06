# Students Assistant - Jarvis AI

An intelligent student assistant application powered by Claude AI that helps high school students manage assignments, tasks, and schedules with the persona of Jarvis from Iron Man.

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
- **Health & Fitness** — WHOOP metrics (recovery/strain/sleep), recent heart rate, recent workouts, personal records tracker (longest run, fastest mile, longest swim), and an interactive workout planner
- **Current Projects** — grid of project cards, each with its granular action items inline (complete/add tasks in place), plus project deadlines/milestones

**AI Calendar Categorization Engine** (`categorize_events` in app.py): every calendar item is routed to exactly one category — `school`, `health`, `projects`, or `general` — via persistent cache → deterministic source/keyword rules → Claude Haiku batch classification → `general` fallback. No item is ever dropped; Home shows everything regardless of category, sub-dashboards filter by it. Tasks are routed the same way via `categorize_task` (school/club/health/general), with manual override through `POST/PATCH /api/tasks` `category`.

## Database Schema

Key tables include:
- `config` - User settings (name, timezone, morning briefing time)
- `tasks` - Pending user tasks with urgency, due dates, and dashboard `category`
- `calendar_categories` - Persistent cache for the calendar categorization engine
- `planned_workouts` - Workout planner entries (Health dashboard)
- `personal_records` - Manual PR overrides (longest run, fastest mile, longest swim)
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

Required:
- `ANTHROPIC_API_KEY` - Claude API key
- `DATABASE_URL` - PostgreSQL connection string
- `SECRET_KEY` - Flask session secret

Optional:
- `APP_PASSWORD` - User login password (default: "finn2025")
- `ADMIN_PASSWORD` - Admin panel password
- `AVERAGE_USER` - Standard user username
- `ADMIN_USER` - Admin user username
- `PERSONAL_ICAL_URL` - User's personal calendar
- `CANVAS_ICAL_URL` - Canvas/LMS assignment calendar (titles + due dates only)
- `CANVAS_API_TOKEN` - Canvas personal access token; unlocks live grades, course names, and full assignment descriptions/rubrics for Jarvis
- `CANVAS_BASE_URL` - Canvas instance root, e.g. `https://parkcityschools.instructure.com` (no trailing slash)
- `WHOOP_CLIENT_ID` / `WHOOP_CLIENT_SECRET` - WHOOP developer app credentials (from developer.whoop.com); required to show the "Connect WHOOP" flow
- `WHOOP_REDIRECT_URI` - Override for the OAuth callback URL; defaults to `<app root>/whoop-auth/callback`
- `SPORTS_ICAL_URL` - Sports/activities calendar
- `RED_DAY_ICAL_URL` - Park City Schools Red Day schedule
- `WHITE_DAY_ICAL_URL` - Park City Schools White Day schedule
- `NOAA_API_TOKEN` - NOAA Climate Data Online API token for historical weather/snow data (free at www.ncdc.noaa.gov/cdo-web/token)
- `GUARDIAN_API_KEY` - The Guardian Open Platform API key for news search (free at open-platform.theguardian.com)

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
