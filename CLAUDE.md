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
- **Google Calendar Write Access** - Once connected, Jarvis can create, update, delete, and list events directly on the student's Google Calendar from chat
- **Apple iCloud Calendar (CalDAV)** - Connects to iCloud Calendar via CalDAV so Jarvis can create, update, delete, and list events on shared family calendars
- **Push Notifications** - Time-sensitive alerts (assignments due soon, briefings, urgent items) delivered via ntfy and/or a Telegram bot, whichever the student has connected

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

### 7. Google Calendar Write Access
- **OAuth2 Connect**: Student connects their Google account from Settings (`/google-auth/start` → `/google-auth/callback`, `google` scope among others); the refresh token is stored server-side (`config.google_refresh_token`) and access tokens are refreshed automatically per-request
- **Jarvis tools**: `create_calendar_event`, `update_calendar_event`, `delete_calendar_event`, `list_google_calendar_events` — available in chat once connected, so the student can just ask ("put my dentist appointment on my calendar Thursday at 3") and Jarvis creates it via the Google Calendar API
- Events land on whichever `calendar_id` is targeted (defaults to `primary`); if `PERSONAL_ICAL_URL` (or another configured feed) is that same calendar's iCal export, AI-created events appear there automatically — though Google's public/private `.ics` feeds can lag live data by up to ~24h, so don't expect instant propagation into the iCal-based dashboards

### 8. Apple iCloud Calendar (CalDAV)
- **Manual Credentials**: Student enters their Apple ID email and app-specific password in Settings; credentials are stored server-side (`config.caldav_url`, `config.caldav_username`, `config.caldav_password`)
- **Shared Family Calendars**: Works with calendars shared via Apple Calendar's family sharing — the mom (or another family member) shares their calendar with the student's Apple ID with "Can Edit" permissions; the student accepts the invite on their device, then configures the shared calendar in the app
- **Jarvis tools**: `create_caldav_event`, `update_caldav_event`, `delete_caldav_event`, `list_caldav_events` — available in chat once connected; the student can ask Jarvis to add events to their iCloud Calendar just like Google Calendar
- Server connects to `caldav.icloud.com` using WebDAV/CalDAV protocol to read and write calendar events
- **Event lookup is deliberately defensive** (`_caldav_locate_event`): iCloud's server-side UID filter (`event_by_uid`) is unreliable and reports "not found" for events it will happily return in a plain listing, so lookup falls back to scanning events client-side, and searches the *other* shared calendars on the account after the configured one. New events are still created on the configured calendar only. Times are written through `_caldav_set_datetime`, which reuses the timezone already on the event — assigning a `ZoneInfo` makes vobject emit `TZID=MST`, which can land the event an hour off — and clears `VALUE=DATE` when an all-day event becomes timed. Updating only the start shifts the end by the same amount, so events never end before they begin
- Updating a recurring event by UID edits the whole series; per-occurrence edits (`RECURRENCE-ID`) are not implemented

### 9. Push Notifications (ntfy + Telegram)
- Two independent, optional push channels — either or both can be active. `send_push_notification()` fans out to whichever are configured
- **ntfy**: set `NTFY_TOPIC` (+ optional `NTFY_SERVER`, `NTFY_TOKEN`); no student-facing connect step
- **Telegram**: set `TELEGRAM_BOT_TOKEN` (create a bot via @BotFather); the student then connects their own chat from Settings by messaging the bot once and tapping "Detect Chat ID" (`POST /api/telegram/detect-chat-id`, backed by the bot's `getUpdates`); chat id is stored in `config.telegram_chat_id`
- Scheduled jobs (assignment due, overdue tasks, AP countdown, meeting reminders, idle nudge, trash reminder, weather alerts, stock alerts) and the morning briefing all push through this same fan-out; each gates on `_notifications_configured()` rather than a single channel
- Jarvis also has a `send_notification` chat tool for proactive, ad-hoc alerts, delivered through the same channels
- **Telegram two-way chat**: **on by default** once a chat id is connected — the webhook (`/api/webhooks/telegram`, guarded by a per-install secret header derived from `SECRET_KEY`+bot token) auto-registers on the next authenticated page load over https, and immediately after "Detect Chat ID". Settings has an opt-out (`POST /api/telegram/disable-chat` sets `config.telegram_chat_disabled` so auto-enable stays off; `POST /api/telegram/enable-chat` clears it). Texting the bot runs a fast Jarvis turn — no extended thinking, short conversational replies, full tool access — in a background thread; a typing indicator shows immediately and an interim "a moment, sir" message is sent if the turn takes >7s (tool calls, web search). Replies over 4096 chars are chunked. The conversation persists to `chat_messages` under conversation_id `telegram`, so the web chat's memory/summaries see it. Only messages from the configured `telegram_chat_id` are answered; `getUpdates`-based chat-id detection requires two-way chat to be disabled first (webhook conflicts with polling)
- **Telegram attachments (photos, screenshots, PDFs)**: the student can send an image or document to the bot and Jarvis reads it. The webhook picks the largest `photo` size and/or the `document`, takes the message's `caption` as the prompt text (attachment messages have no `text`), and `_telegram_attachment_blocks()` downloads each file via `getFile` + the file CDN and converts it to an Anthropic content block — images → `image` blocks, PDFs → `document` blocks, plain-text-ish files (`.txt/.md/.csv/.json/.ics/…`) → inlined `text`. Anything else (e.g. `.docx`) is reported back to the student rather than silently dropped. Caps: 18MB download (Bot API serves ≤20MB), 4MB per image (Anthropic's own limit), 5 attachments per message. A cache breakpoint on the last block stops a large PDF being re-uploaded on every tool-loop iteration, and the client timeout goes 45s → 90s when an attachment is present
- Attachment turns get extra system-prompt guidance: event invites/flyers/tickets are extracted and put straight on the calendar (created, then confirmed — not asked about first); deadline/syllabus documents become tasks or a project; reference material is written into `save_memory` in enough detail to be useful later, **because the file itself is not retained** — `chat_messages` stores text only, so the transcript records a `[sent a PDF (invite.pdf)] …` label rather than the bytes

### 10. Scheduled Reminders (AI-created)
- The student can just say "remind me to take out the trash at 3pm" or "give me the news every morning" in chat (web **or** Telegram) and Jarvis schedules it immediately via the `create_reminder` tool — no confirmation step and no explicit "set a routine" phrasing needed. `list_reminders` / `cancel_reminder` manage them
- Stored in the `reminders` table; a per-minute APScheduler job (`check_scheduled_reminders`) fires anything due through the same `send_push_notification()` fan-out (ntfy + Telegram)
- **One-time** (`recurrence='once'` + `remind_at`) or **recurring** (`daily` / `weekdays` / `weekly` + `time_of_day`, plus `day_of_week` for weekly)
- Two content modes:
  - `needs_generation=false` — `message` is pushed **verbatim** (a plain reminder)
  - `needs_generation=true` — `message` is an *instruction* re-run through Claude at fire time with full tool access including live web search, and whatever it produces is pushed. This is what makes "the news every morning" work. Reminder-management and `send_notification` tools are excluded from that turn so it can't schedule more reminders or double-notify
- Each due reminder is **claimed atomically** (its `next_run` is advanced, or it's deactivated, guarded on the value just read) *before* delivery, so a slow generation can never cause a double-send; delivery then runs on its own thread so one slow reminder doesn't hold up the rest
- Gated on `_notifications_configured()` — the tools aren't offered when no push channel exists

### 11. Multi-Dashboard Architecture
The UI is four dense, above-the-fold grid dashboards (each widget scrolls internally; the page itself does not scroll on desktop):
- **Home** — master aggregated view of *everything*: all calendar items (with category chips), upcoming tasks, health metrics, and project statuses
- **School** — academics only: active assignments, school tasks, and club/student-org tasks (`tasks.category` = `school` / `club`)
- **Health & Fitness** — WHOOP metrics (recovery/strain/sleep/heart rate/resting HR as five stat tiles), recent workouts, personal records tracker (longest run, fastest mile, longest swim, highest strain), and an interactive workout planner
- **Current Projects** — grid of project cards, each with its granular action items inline (complete/add tasks in place), plus project deadlines/milestones
- **Personal Improvement** (nav: "Growth") — a widget grid for self-growth tools, built to hold more widgets over time. Current widgets: a **Reading List** book tracker (add books you want to read, check them off as you finish, with a progress bar; `books` table via `/api/books`); a **Current Skill Focus** tracker (list of skills you're developing with exactly one starred as the active focus; `skills` table via `/api/skills`); and a **Daily Verse** card that shows a public-domain (KJV) Bible verse chosen deterministically from the date so a fresh one appears each day (`/api/verse-of-the-day`, no external API).

**AI Calendar Categorization Engine** (`categorize_events` in app.py): every calendar item is routed to exactly one category — `school`, `health`, `projects`, or `general` — via persistent cache → deterministic source/keyword rules → Claude Haiku batch classification → `general` fallback. No item is ever dropped; Home shows everything regardless of category, sub-dashboards filter by it. Tasks are routed the same way via `categorize_task` (school/club/health/general), with manual override through `POST/PATCH /api/tasks` `category`.

## Database Schema

Key tables include:
- `config` - User settings (name, timezone, morning briefing time)
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
- `reminders` - AI-created scheduled reminders (title, message, needs_generation, recurrence, time_of_day, day_of_week, next_run, last_fired_at, active)
- `books` - Reading list for the Personal Improvement page (title, author, notes, completed/completed_at)
- `skills` - Current Skill Focus tracker for the Personal Improvement page (name, notes, focus flag, completed/completed_at); exactly one row is the active `focus`

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
- `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` - Google Cloud OAuth2 client credentials; required to show the "Connect Google Calendar" flow (and Drive/Docs/Sheets/Slides/Forms/Classroom access) in Settings
- `GOOGLE_REDIRECT_URI` - Override for the OAuth callback URL; defaults to `<app root>/google-auth/callback`
- `NTFY_TOPIC` - ntfy.sh (or self-hosted) topic to publish push notifications to; unset disables the ntfy channel
- `NTFY_SERVER` - ntfy server root, default `https://ntfy.sh`
- `NTFY_TOKEN` - Bearer token for a protected/self-hosted ntfy topic
- `TELEGRAM_BOT_TOKEN` - Bot token from @BotFather; required to show the Telegram section in Settings. The chat to notify is set by the student from Settings (message the bot once, then "Detect Chat ID") — no per-student env var needed
- CalDAV credentials are **not** environment variables — they're entered by the student in Settings and stored in `config` table (`caldav_url`, `caldav_username`, `caldav_password`)

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
- `GET|POST /api/books` (+ `PATCH|DELETE /<id>`) - Reading list / book tracker CRUD (Personal Improvement page)
- `GET|POST /api/skills` (+ `PATCH|DELETE /<id>`) - Current Skill Focus tracker CRUD; `PATCH {focus:true}` makes a skill the sole active focus (Personal Improvement page)
- `GET /api/verse-of-the-day` - Deterministic daily Bible verse (KJV, curated in-app; no external API)
- `GET /api/google/status` / `POST /api/google/disconnect` - Google Calendar connection status / disconnect
- `GET /api/caldav/status` - Apple iCloud Calendar connection status
- `POST /api/caldav/configure` - Configure CalDAV credentials (url, username, password)
- `POST /api/caldav/disconnect` - Disconnect Apple iCloud Calendar
- `GET /api/telegram/status` - Telegram push notification connection status
- `POST /api/telegram/detect-chat-id` - Look up the chat id from the bot's most recent message (student messages the bot, then calls this)
- `POST /api/telegram/set-chat-id` - Manually set (or clear) the Telegram chat id
- `POST /api/telegram/test` - Send a test push notification via Telegram

## Jarvis Tool Surface

All Jarvis tools live in `JARVIS_TOOLS` and are dispatched by `_execute_jarvis_tool()` — a single dispatcher shared by the web chat (`/api/chat`), the Telegram webhook turn, and scheduled reminder generation. `_build_active_tools()` trims the list per request, hiding tools whose integration isn't configured (Google, CalDAV, NOAA, Guardian, push channels).

**This matters for Telegram**: the web chat injects a lot of live state into its system prompt (assignments, tasks, and — in summer mode — bucket list and daily plan), but the Telegram turn builds its own much smaller prompt with none of that. Anything Jarvis should be able to reach *over text* has to exist as a **tool**, not just as injected context. Prefer adding a tool over adding another prompt injection.

Coverage by area:
- **Tasks**: get/create/complete/delete/update, plus recurring tasks (create/list/delete)
- **Assignments & grades**: get_assignments, get_assignment_details, get_grades, complete_assignment
- **Projects**: get_projects, create_project, add_project_task, complete_project_task, add_project_note
- **Calendars**: Google (create/update/delete/list) and Apple CalDAV (create/update/delete/list)
- **Health & fitness**: get_health_metrics (recovery/HRV/resting HR/sleep/strain + personal records), get_workouts, plan_workout, list_planned_workouts, complete_planned_workout
- **Personal Improvement (Growth)**: get_growth_lists, add_growth_item, complete_growth_item, set_skill_focus — one tool set covering the structurally identical `books` / `skills` / `bucket_list` tables, selected by a `list` parameter
- **Planning & summaries**: get_daily_plan, generate_daily_plan, get_briefing, get_debrief
- **Notifications**: send_notification (immediate), create_reminder / list_reminders / cancel_reminder (scheduled)
- **Google Workspace**: Drive, Docs, Sheets, Slides, Forms, Gmail, Classroom
- **External data**: web_search / web_fetch (Anthropic server tools), get_weather, get_climate_history, get_news, stocks (get_portfolio, log_stock_transaction, save_stock_note), get_activity_suggestion
- **Memory**: save_memory, remember_person, get_person_profile, list_people

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
