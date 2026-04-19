# Students Assistant - Jarvis AI

An intelligent student assistant application powered by Claude AI that helps high school students manage assignments, tasks, schedules, and fitness routines with the persona of Jarvis from Iron Man.

## Overview

This Flask-based web application provides a comprehensive student management system featuring:

- **Daily Briefings** - Morning plans synthesizing assignments, calendar events, and tasks
- **Evening Debriefs** - End-of-day summaries of accomplishments and upcoming priorities
- **Intelligent Chat** - Conversational AI assistant with context awareness
- **Workout Planning** - AI-generated strength training programs with rotation-based focus areas
- **Task Management** - Smart task creation, prioritization, and tracking
- **Schedule Optimization** - Automated daily schedule generation using available time windows
- **Calendar Integration** - Syncs with Canvas (assignments), personal calendars, and school events

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
  - Upcoming assignments from Canvas
  - Pending tasks and project work
  - Park City High School bell schedule (Red/White day rotation)
  - Student availability during/after school hours

### 3. Workout System
- **Rotation-Based Focus Cycles**: Back → Biceps & Triceps → Core/Cardio → Legs → Shoulders
- **Equipment Awareness**: Home gym (≤35 lb dumbbells) vs. full gym
- **Adaptive Programming**: Considers recent history and injury concerns
- **Workout Logging**: Categorizes custom workouts with difficulty assessment
- **Regeneration**: Creates alternative workouts for same focus area

### 4. Task Management
- **Manual Task Creation**: User-created pending tasks with urgency levels
- **Smart Suggestions**: Claude analyzes upcoming assignments and events to suggest new tasks
- **Recurring Tasks**: Daily processing at midnight maintains recurring task instances
- **Filtering**: Only suggests tasks not completed and due within 14 days

### 5. Schedule Planning
- **Free Window Detection**: Analyzes calendar to find available time slots
- **Smart Prioritization**: MUST-include assignments > critical tasks > medium tasks > projects
- **JSON-Based Scheduling**: Returns structured schedule items with exact time blocks

## Database Schema

Key tables include:
- `config` - User settings (name, timezone, morning briefing time)
- `tasks` - Pending user tasks with urgency and due dates
- `completions` - Logged task/assignment completions with time tracking
- `projects` - Active projects with status tracking
- `project_tasks` - Tasks within projects
- `project_notes` - Collaborative notes for projects
- `briefing_cache` - Cached morning briefing content
- `debrief_cache` - Cached evening debrief content
- `workout_logs` - Completed workout records
- `workout_state` - Current position in rotation cycle
- `timer_state` - Timer state for work sessions
- `daily_plans` - Generated daily schedules

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
- `CANVAS_ICAL_URL` - Canvas/LMS assignment calendar
- `SPORTS_ICAL_URL` - Sports/activities calendar
- `RED_DAY_ICAL_URL` - Park City Schools Red Day schedule
- `WHITE_DAY_ICAL_URL` - Park City Schools White Day schedule

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
- `POST /api/workout/generate` - Create new workout plan
- `POST /api/workout/log/<int:log_id>` - Log completed workout
- `POST /api/workout/regenerate` - Alternative workout for same focus
- `POST /api/tasks` - Create/manage tasks
- `GET /api/task-suggestions` - AI task suggestions
- `GET /api/plan-my-day` - Generate daily schedule

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
