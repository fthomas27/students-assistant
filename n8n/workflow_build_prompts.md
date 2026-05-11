# n8n Workflow Build Prompts — Jarvis Student Assistant

Paste each prompt below into n8n's **"Describe your workflow"** AI builder
(the chat icon on a new workflow canvas). Build one workflow at a time.

Before using any prompt, set these values in your n8n environment:
- `APP_BASE_URL` — e.g. `https://your-app.com`
- `APP_API_KEY` — your Flask app API key
- `STUDENT_WHATSAPP` — e.g. `+1XXXXXXXXXX`
- `STUDENT_EMAIL` — student's email address

---

## Workflow 1 — Morning Briefing

```
Build me an n8n workflow called "Jarvis — Morning Briefing" that runs every weekday at 7:00 AM America/Denver time.

Workflow steps:

1. Schedule Trigger node: Cron expression "0 7 * * 1-5", timezone America/Denver.

2. HTTP Request node called "Get Day Info": GET request to {{ $env.APP_BASE_URL }}/api/day-info with header X-API-Key: {{ $env.APP_API_KEY }}. This returns JSON with fields: day_type (string, "Red" or "White"), is_school_day (boolean), schedule (object).

3. IF node called "Check School Day": Check if {{ $json.is_school_day }} equals true. If false, route to the "No School" branch. If true, continue to fetch briefing.

4. HTTP Request node called "Get Briefing": GET {{ $env.APP_BASE_URL }}/api/briefing with header X-API-Key: {{ $env.APP_API_KEY }}. Returns JSON with field "content" (markdown string).

5. IF node called "Briefing Empty?": Check if {{ $json.content }} is empty or null. If empty, make a POST request to {{ $env.APP_BASE_URL }}/api/briefing/refresh with header X-API-Key: {{ $env.APP_API_KEY }}, wait 5 seconds, then re-fetch GET /api/briefing.

6. AI Agent node called "Jarvis Formats Briefing": Use the Anthropic Claude model (claude-sonnet-4-6). Set the system prompt to the contents of the Jarvis orchestrator system prompt (from jarvis_orchestrator_prompt.md). User message: "You have received the morning briefing data below. Format it for TWO outputs: (1) an HTML email and (2) a plain-text WhatsApp message following your channel formatting rules. Return a JSON object with two keys: email_html (string) and whatsapp_text (string). Briefing content: {{ $('Get Briefing').item.json.content }}. School day type today: {{ $('Get Day Info').item.json.day_type }}."

7. Send Email node called "Email Briefing": 
   - From: jarvis@your-domain.com
   - To: {{ $env.STUDENT_EMAIL }}
   - Subject: "Jarvis — Morning Briefing — {{ $now.setZone('America/Denver').toFormat('ccc, LLL d') }}"
   - Email Format: HTML
   - HTML Body: {{ $json.email_html }}

8. WhatsApp Business node called "WhatsApp Briefing":
   - To: {{ $env.STUDENT_WHATSAPP }}
   - Message: {{ $json.whatsapp_text }}

9. On the "No School" branch from step 3: Add a No-Op node or simply end the workflow without sending anything.

Add error handling: if any HTTP Request node fails (status not 2xx), route to a Slack or email alert node that sends "Jarvis morning briefing failed — check app logs."
```

---

## Workflow 2 — Evening Debrief

```
Build me an n8n workflow called "Jarvis — Evening Debrief" that runs every day at 6:30 PM America/Denver time.

Workflow steps:

1. Schedule Trigger node: Cron "30 18 * * *", timezone America/Denver.

2. HTTP Request node called "Generate Debrief": GET {{ $env.APP_BASE_URL }}/api/debrief/generate with header X-API-Key: {{ $env.APP_API_KEY }}. Returns JSON with field "content" (markdown string).

3. HTTP Request node called "Get Today Stats": GET {{ $env.APP_BASE_URL }}/api/stats with header X-API-Key: {{ $env.APP_API_KEY }}. Returns JSON with completion counts and hours logged.

4. IF node called "Debrief Empty?": Check if debrief content is empty. If empty, skip to step 6 without sending.

5. AI Agent node called "Jarvis Formats Debrief": Use Anthropic Claude (claude-sonnet-4-6). System prompt: paste the Jarvis orchestrator system prompt. User message: "You have the evening debrief and today's productivity stats. Format them for TWO outputs: (1) a full HTML email debrief and (2) a compressed plain-text WhatsApp message (max 500 characters). Return JSON with keys: email_html (string) and whatsapp_text (string). Debrief: {{ $('Generate Debrief').item.json.content }}. Stats: {{ JSON.stringify($('Get Today Stats').item.json) }}."

6. Send Email node called "Email Debrief":
   - To: {{ $env.STUDENT_EMAIL }}
   - Subject: "Jarvis — Evening Debrief — {{ $now.setZone('America/Denver').toFormat('ccc, LLL d') }}"
   - Email Format: HTML
   - HTML Body: {{ $json.email_html }}

7. WhatsApp Business node called "WhatsApp Debrief":
   - To: {{ $env.STUDENT_WHATSAPP }}
   - Message: {{ $json.whatsapp_text }}

Add a final Set node that outputs { "workflow": "evening_debrief", "status": "sent", "timestamp": "{{ $now.toISO() }}" } for logging.
```

---

## Workflow 3 — Deadline Alert

```
Build me an n8n workflow called "Jarvis — Deadline Alerts" that runs every hour between 8 AM and 9 PM America/Denver time.

Workflow steps:

1. Schedule Trigger node: Cron "0 8-21 * * *", timezone America/Denver.

2. HTTP Request node called "Get Assignments": GET {{ $env.APP_BASE_URL }}/api/assignments with header X-API-Key: {{ $env.APP_API_KEY }}. Returns JSON array of assignments, each with fields: title (string), course (string), due_date (ISO string), uid (string), completed (boolean).

3. Code node called "Filter Due Soon": Write JavaScript to filter the assignments array for items where completed is false AND due_date is within the next 24 hours from now. Output the filtered array as {{ $json.due_soon }}. Also output a count: {{ $json.count }}.

4. IF node called "Any Due Soon?": Check if {{ $json.count }} > 0. If 0, end the workflow without sending anything.

5. AI Agent node called "Jarvis Writes Alert": Anthropic Claude (claude-sonnet-4-6). System prompt: paste the Jarvis orchestrator system prompt. User message: "The following assignments are due within 24 hours and are not yet completed. Write a WhatsApp alert (plain text only, max 300 characters, use bullet points with •, end with — Jarvis). Assignments: {{ JSON.stringify($('Filter Due Soon').item.json.due_soon) }}."

6. WhatsApp Business node called "Send Deadline Alert":
   - To: {{ $env.STUDENT_WHATSAPP }}
   - Message: {{ $json.output }}

Add deduplication: before step 5, add a Code node that checks a static data store (n8n's built-in $getWorkflowStaticData) for a "last_alert_sent" timestamp. If an alert was sent less than 6 hours ago for the same assignment UIDs, skip sending and end the workflow.
```

---

## Workflow 4 — Critical Task Nudge

```
Build me an n8n workflow called "Jarvis — Critical Task Nudge" that runs at 3:00 PM and 5:00 PM America/Denver time on weekdays.

Workflow steps:

1. Schedule Trigger node: Cron "0 15,17 * * 1-5", timezone America/Denver.

2. HTTP Request node called "Get Tasks": GET {{ $env.APP_BASE_URL }}/api/tasks with header X-API-Key: {{ $env.APP_API_KEY }}. Returns JSON array of tasks with fields: id, title, urgency, due_date, completed.

3. Code node called "Filter Critical": Filter tasks where urgency equals "critical" AND completed is false. Output filtered array and count.

4. IF node called "Any Critical?": Check if count > 0. If 0, end without sending.

5. AI Agent node called "Jarvis Writes Nudge": Anthropic Claude (claude-sonnet-4-6). System prompt: paste Jarvis orchestrator system prompt. User message: "Write a short WhatsApp nudge for the student about the following critical tasks that need attention. Plain text only, max 250 characters, use • bullets, end with — Jarvis. Tasks: {{ JSON.stringify($('Filter Critical').item.json) }}."

6. WhatsApp Business node called "Send Nudge":
   - To: {{ $env.STUDENT_WHATSAPP }}
   - Message: {{ $json.output }}
```

---

## Workflow 5 — Weekly Summary

```
Build me an n8n workflow called "Jarvis — Weekly Summary" that runs every Sunday at 7:00 PM America/Denver time.

Workflow steps:

1. Schedule Trigger node: Cron "0 19 * * 0", timezone America/Denver.

2. HTTP Request node called "Get Stats": GET {{ $env.APP_BASE_URL }}/api/stats with header X-API-Key: {{ $env.APP_API_KEY }}.

3. HTTP Request node called "Get Upcoming Assignments": GET {{ $env.APP_BASE_URL }}/api/assignments with header X-API-Key: {{ $env.APP_API_KEY }}.

4. HTTP Request node called "Get Active Projects": GET {{ $env.APP_BASE_URL }}/api/projects with header X-API-Key: {{ $env.APP_API_KEY }}.

Run steps 2, 3, and 4 in parallel (connect all three from the trigger node).

5. Merge node: Wait for all three HTTP requests to complete, then pass all data downstream.

6. AI Agent node called "Jarvis Writes Weekly Summary": Anthropic Claude (claude-sonnet-4-6). System prompt: paste Jarvis orchestrator system prompt. User message: "It is Sunday evening. Synthesize the following data into a weekly summary. Produce TWO outputs: (1) a full HTML email with Jarvis commentary on the week's productivity, trends, and the week ahead — be thorough and sophisticated; (2) a compressed plain-text WhatsApp message (max 500 characters) with the key highlights. Return JSON with keys email_html and whatsapp_text. Stats: {{ JSON.stringify($('Get Stats').item.json) }}. Upcoming assignments: {{ JSON.stringify($('Get Upcoming Assignments').item.json) }}. Active projects: {{ JSON.stringify($('Get Active Projects').item.json) }}."

7. Send Email node called "Email Weekly Summary":
   - To: {{ $env.STUDENT_EMAIL }}
   - Subject: "Jarvis — Weekly Summary — Week of {{ $now.setZone('America/Denver').startOf('week').toFormat('LLL d') }}"
   - Email Format: HTML
   - HTML Body: {{ $json.email_html }}

8. WhatsApp Business node called "WhatsApp Weekly Summary":
   - To: {{ $env.STUDENT_WHATSAPP }}
   - Message: {{ $json.whatsapp_text }}
```

---

## Workflow 6 — App Webhook Receiver

```
Build me an n8n workflow called "Jarvis — App Webhook Receiver" that listens for incoming POST requests from the student assistant Flask app.

Workflow steps:

1. Webhook Trigger node called "App Event Webhook":
   - HTTP Method: POST
   - Path: /jarvis-event
   - Authentication: Header Auth using header name X-Webhook-Secret and value {{ $env.WEBHOOK_SECRET }}
   - Response Mode: Respond immediately with { "received": true }

2. Switch node called "Route by Event Type": Route on {{ $json.body.event }} with these cases:
   - "task_created" → branch A
   - "briefing_ready" → branch B
   - "plan_generated" → branch C
   - "assignment_completed" → branch D (silent, no notification)
   - Default → branch E (unknown, log and end)

3. Branch A — task_created:
   a. IF node: Check if {{ $json.body.data.urgency }} equals "critical". If not critical, end without sending.
   b. IF node: Check current hour is between 7 and 22 (quiet hours gate). If outside range, end.
   c. WhatsApp Business node: Send "⚠️ New critical task added: {{ $json.body.data.title }}. — Jarvis" to {{ $env.STUDENT_WHATSAPP }}.

4. Branch B — briefing_ready:
   a. HTTP Request node: GET {{ $env.APP_BASE_URL }}/api/briefing with header X-API-Key: {{ $env.APP_API_KEY }}.
   b. AI Agent node: Format the briefing content as a short WhatsApp message (same instructions as Workflow 1 step 6, WhatsApp only this time).
   c. WhatsApp Business node: Send the formatted message to {{ $env.STUDENT_WHATSAPP }}.

5. Branch C — plan_generated:
   a. WhatsApp Business node: Send "📅 Your schedule for today is ready. Check Jarvis for your plan. — Jarvis" to {{ $env.STUDENT_WHATSAPP }}.

6. Branch D — assignment_completed: No-Op node. End silently.

7. Branch E — unknown event: Set node that logs { "event": "{{ $json.body.event }}", "status": "unhandled", "timestamp": "{{ $now.toISO() }}" }. End.

Add error handling on the Webhook Trigger: if authentication fails, return HTTP 401 with body { "error": "unauthorized" }.
```

---

## Notes for All Workflows

### Credentials to create in n8n first
1. **Anthropic API** — add your Anthropic API key under Credentials → New → Anthropic
2. **SMTP / Email** — configure your email provider (Gmail, SendGrid, etc.) under Credentials → New → Email (SMTP)
3. **WhatsApp Business** — connect via Meta Business API or Twilio under Credentials → New → WhatsApp Business
4. **HTTP Header Auth** — create a credential with header name `X-API-Key` and your app's API key value

### Environment variables to set in n8n
Go to **Settings → Variables** and add:
```
APP_BASE_URL       your app URL (no trailing slash)
APP_API_KEY        your Flask app API key
STUDENT_EMAIL      student email address
STUDENT_WHATSAPP   WhatsApp number with country code (+1...)
WEBHOOK_SECRET     shared secret for app → n8n webhooks
```

### After building each workflow
- Activate the workflow toggle (top right)
- Test manually using the "Test workflow" button before enabling the schedule
- Check execution logs under **Executions** after the first scheduled run
