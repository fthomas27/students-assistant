# Schola Registry

A focused academic dashboard for a Park City High School student. Three data
connectors, four pages, no assistant.

## What this app is

It answers three questions and nothing else:

- **What am I being graded on?** — course standing from Canvas and PowerSchool
- **What is due, and when?** — Canvas assignments plus surrounding calendar feeds
- **Am I rested enough to do it?** — WHOOP recovery, sleep and strain

Everything else that used to live here (an AI assistant, a Telegram bot, tasks,
projects, stocks, news, weather, reading lists, a parent portal) has been
removed. If a feature does not serve one of those three questions, it does not
belong.

## Tech Stack

- **Backend**: Python Flask with ProxyFix middleware for reverse proxy support
- **Database**: PostgreSQL via psycopg2 with a threaded connection pool
- **Calendar parsing**: icalendar + recurring-ical-events
- **Scheduling**: APScheduler, running data sync only
- **Scraping**: Playwright + BeautifulSoup for PowerSchool; Claude vision as the
  extraction fallback when HTML parsing fails
- **Frontend**: one server-rendered template, no build step, no framework
- **Auth**: session-based login with admin controls, IP lockout, and Stripe billing

## The three connectors

Everything on screen comes from one of these. They are the app's backbone, and
the Sync & Feeds page exists to show their state.

| Connector | Source | Cadence | Gives us |
|---|---|---|---|
| **Canvas** | iCal feed + REST (token *or* password login) | every 15 min | assignment titles, due dates, course grades |
| **PowerSchool** | headless-browser scrape | weekdays 07:12 and 15:12 | weighted grades, attendance |
| **WHOOP** | OAuth2 API | every 30 min | recovery, sleep, strain, workouts, heart rate |

Each run is timed and written to `sync_events`, which is what the audit trail on
Sync & Feeds renders. `run_full_pipeline()` runs all three under a lock so two
runs can't overlap; it also runs once at boot to warm the caches.

When WHOOP is not connected, a deterministic mock pipeline returns the same
response shape so the Readiness page still renders. The `mock` flag on those
responses is surfaced in the UI as a "Sample Data" chip — never present mock
numbers as real ones.

## The four pages

All four live in `templates/index.html` as sections toggled by hash routing.

1. **Academic Overview** — course standing merged across both gradebooks
   (PowerSchool wins on conflict), today's bell schedule, what needs attention
2. **Assignments & iCal** — the assignment register ordered by real due date,
   with complete/submit actions, plus upcoming calendar events and feed health
3. **WHOOP & Readiness** — five stat tiles, 7-day recovery and strain charts,
   recent workouts, bedtime recommendation, personal records
4. **Sync & Feeds** — connector cards with last/next run, pipeline health, and
   the audit log

## Design language

Editorial, calm, low-noise — the opposite of a dense telemetry dashboard.

- **Type**: Newsreader (serif) for display, JetBrains Mono for micro-labels and
  all numeric data, system sans for prose. Webfonts come from Google Fonts with
  real fallbacks (Georgia, system mono) — the design must still read if the CDN
  is blocked, and it does.
- **Colour**: near-monochrome on warm paper. Status colour is reserved for
  connector state and always paired with a text label, never colour alone.
- **Charts**: drawn with CSS boxes, not a scaled SVG viewBox — a viewBox scales
  its text and geometry with container width, which is why the first version
  rendered 200px-tall bar labels. Recovery uses a validated single-hue
  sequential ramp (magnitude, light→dark), re-stepped for the dark surface.
  Never put recovery and strain on one dual-axis chart; they are two charts.
- **Themes**: light, dark, and system, with tokens defined under bare `:root`,
  `prefers-color-scheme`, and `[data-theme]` so the toggle wins both ways.
  The sequential ramp inverts on dark, so never write copy that claims a colour
  direction ("darker is better") — it is false in one of the two themes.

## Database Schema

- `config` / `user_config` - settings (name, timezone, wake_time, feed URLs)
- `sync_events` - connector audit log, pruned to 14 days
- `canvas_assignments_cache` - keeps assignments after Canvas prunes the feed,
  so overdue work still surfaces
- `completions` - logged assignment completions with time tracking
- `assignment_estimates` - per-assignment manual time estimates
- `personal_records` - manual PR overrides layered over computed WHOOP records
- `users`, `subscriptions`, `access_codes`, `billing_events`, `pricing_config`,
  `pending_signups`, `access_requests` - accounts and billing
- `login_attempts`, `login_lockouts`, `lockdown_state`, `blocked_ips`, `ip_names`
  - auth hardening

Tables for removed features are no longer created, but existing databases keep
their rows — dropping a `CREATE TABLE` destroys nothing.

## Environment Variables

Required:
- `DATABASE_URL` - PostgreSQL connection string
- `SECRET_KEY` - Flask session secret

Optional:
- `ANTHROPIC_API_KEY` - only used by the PowerSchool vision fallback
- `APP_PASSWORD`, `ADMIN_PASSWORD`, `AVERAGE_USER`, `ADMIN_USER` - login
- `CANVAS_ICAL_URL` - Canvas assignment feed (titles + due dates)
- `CANVAS_BASE_URL` - Canvas root, e.g. `https://pcsd.instructure.com`
- `CANVAS_API_TOKEN` - personal access token, when the district allows them
- `CANVAS_USERNAME` / `CANVAS_PASSWORD` - fallback when it doesn't (see below)
- `POWER_USERN` / `POWER_PASS` / `PS_BASE_URL` - PowerSchool credentials
- `WHOOP_CLIENT_ID` / `WHOOP_CLIENT_SECRET` / `WHOOP_REDIRECT_URI` - WHOOP OAuth
- `PERSONAL_ICAL_URL`, `SPORTS_ICAL_URL` - extra calendar feeds
- `RED_DAY_ICAL_URL`, `WHITE_DAY_ICAL_URL` - Park City bell-schedule feeds
- `STRIPE_SECRET_KEY`, `STRIPE_PUBLISHABLE_KEY`, `STRIPE_WEBHOOK_SECRET`,
  `STRIPE_PRODUCT_ID` - billing

Calendar URLs can also be set per-user in the app; the DB value wins over the
env var.

## API Endpoints

- `GET /api/assignments` - assignments with estimates, overdue merged from cache
- `POST /api/complete` / `/api/submit` / `/api/uncomplete` - completion tracking
- `POST /api/assignments/<uid>/estimate` - override a time estimate
- `GET /api/calendar?days=N` - all feed events, each with a source-derived category
- `GET /api/canvas/grades` - live Canvas course grades
- `GET /api/canvas/status` - which auth mode is active, and the last login error
- `POST /api/canvas/configure` - save credentials and immediately test the sign-in
- `POST /api/canvas/disconnect` - clear stored Canvas credentials
- `GET /api/canvas/debug` - step-by-step login / `/grades` / API / parse trace
- `GET /api/powerschool/grades` / `/api/powerschool/attendance` - scraped data
- `POST /api/powerschool/refresh` - bust the scrape cache
- `GET /api/whoop/summary` / `/workouts` / `/heart-rate` / `/bedtime` / `/status`
- `GET|POST /api/fitness/prs` - personal records
- `GET /api/day-info?date=` / `/api/day-type` - Red/White day and bell schedule
- `GET /api/stats` - logged minutes, streak, estimate accuracy
- `GET /api/sync-status` - per-connector state, next run, feed errors
- `GET /api/sync/events` - audit trail
- `POST /api/sync/run` - run one connector (`{"connector": "canvas"}`) or all
- `GET|POST /api/config` - settings

Unconfigured optional connectors answer `200` with `configured: false`, not an
error status — "not set up" is a normal state, and a 503 just makes the console
noisy.

## Canvas grade access

Park City does not let student accounts generate personal access tokens, so
there are two auth modes and `_canvas_auth_mode()` picks between them:

- **token** — `CANVAS_API_TOKEN` is set. Preferred wherever it is available.
- **password** — `CANVAS_USERNAME` + `CANVAS_PASSWORD`. `_canvas_login()` posts
  the login form at `/login/canvas` (carrying the page's `authenticity_token`)
  and keeps the resulting `canvas_session` cookie for 25 minutes.

Grades themselves come from two sources, tried in order by `canvas_grades()`:

1. **`GET /grades`**, Canvas' own summary page, fetched over the logged-in
   session. This is the primary source.
2. **`/api/v1/users/self/enrollments`**, used only when the page yields nothing
   (and always in token mode, where there is no browser session).

The page wins because of *who the account is*. This login is **view-only, i.e.
an observer**, and the self-scoped API returns the observer's own enrollments —
which is an empty list — while `/grades` renders the observed student's actual
grades. Trusting the API alone gives you zero grades and no error.

`_canvas_parse_grades_html()` is deliberately tolerant. It does **not** bind to
Canvas class names, which vary by version and theme — keep it that way, and keep
the API fallback, which covers a real student login.

What the live page actually looks like, and what the parser must handle:

- **It is a list of course links, not a table.** Each link is followed by its
  grade. The parser finds every `/courses/<id>` link and reads the grade from
  its enclosing table row when there is one, or from the text up to the next
  course link when there isn't. Do not narrow it to tables.
- **The page gives a percentage for some courses and a letter for others,
  rarely both.** `canvas_courses()` therefore requests
  `include[]=total_scores`, which returns `computed_current_score` *and*
  `computed_current_grade` per course, and `canvas_grades()` uses it to fill
  whichever half the page omitted. The page wins where it has a value; the API
  only fills gaps and adds graded courses the page missed. A row with a letter
  and no percent is still kept — dropping those once hid a course at an F.
- **Never derive a letter from a percentage.** Park City's scale is not the
  standard 10-point one — 71.61% is a B- there — so derivation disagreed with
  Canvas on 3 of 4 courses when it was tried. Letters come from the page or not
  at all.
- **Letter extraction is layered**: an element Canvas labels as the letter
  grade, else a standalone letter in the percentage's own cell, else anywhere
  in the row. Unicode minus and en/em dashes normalise to ASCII first.
- **"no grade" and `N/A` rows are dropped.** Note `N/A` ends in a standalone A,
  which a naive letter match reads as an A grade — strip it before matching.
- **An expired session comes back as a 200 containing the login page**, not a
  401. Both `_canvas_get()` and `_canvas_get_html()` sniff for that, re-log in,
  and retry exactly once.

`GET /api/canvas/debug?raw=1` dumps the first table's markup even when parsing
succeeded, which is how to fix what it got *wrong* rather than what it missed.

Everything here is read-only: only GETs are issued after the login POST.

Credentials live in the `config` table in plaintext, like the other secrets in
this app. `/api/config` masks them and the page never receives them back.

## Park City School Specific

- **School Year**: Aug 18, 2025 - Jun 5, 2026
- **Bell Schedule**: Red Day (7:30 AM-11:53 AM) vs. White Day (7:30 AM-2:25 PM)
- **Holiday Dates**: built-in calendar for 2025-2026
- **Timezone**: Mountain Time (America/Denver)

## Development Notes

- `FLASK_SKIP_BOOT=1` skips DB init and the scheduler; `FLASK_BOOT_DEV=1` relaxes
  the secret-strength check. The test suite sets both.
- Templates are cached when `debug=False`. If a template edit seems to have no
  effect, restart the server rather than hunting the CSS.
- Canvas iCal summaries are `Title [Course]`; some feeds use `Title - Course`.
  `parse_canvas_assignments` handles both — do not narrow it to one.
- `_uid()` returns None outside a request context, so scheduler jobs fall back to
  the default student. Never call `session` from a worker thread.
- Tests run without a live database by stubbing `get_db()`: `pytest tests/ -q`.
