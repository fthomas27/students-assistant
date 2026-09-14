# Schola Registry

A focused academic dashboard: what you're graded on, what's due, and whether
you're rested enough to do it.

Two connectors feed it — **Canvas** and **WHOOP** — and four
pages render it: Academic Overview, Assignments & iCal, WHOOP & Readiness, and
Sync & Feeds.

## Running it locally

```sh
pip install -r requirements.txt

export DATABASE_URL="postgresql://user@localhost/schola"
export SECRET_KEY="something-long-and-random"
export FLASK_BOOT_DEV=1          # relaxes the secret-strength check for dev

python -c "import app; app.init_db()"
./start.sh
```

Then open http://127.0.0.1:8000 and sign in with `APP_PASSWORD` (default
`finn2025`).

## Connecting the data sources

Neither is required — the app renders with whatever is configured,
and Sync & Feeds shows the state of each.

- **Canvas** — paste your calendar feed URL in the app, or set `CANVAS_ICAL_URL`.
  That gives titles and due dates. For grades, open **Sync & Feeds** and fill in
  the Canvas card: your Canvas URL plus the username and password you sign in
  with. If your district does issue personal access tokens, set
  `CANVAS_API_TOKEN` instead — it is preferred when present. `GET
  /api/canvas/debug` prints a step-by-step trace if sign-in misbehaves.
- **WHOOP** — set `WHOOP_CLIENT_ID` and `WHOOP_CLIENT_SECRET`, then connect from
  the Readiness page. Without it, the page renders sample data, clearly labelled.

See `CLAUDE.md` for the full environment variable list and architecture notes.

## Letting an agent read it

Set `AGENT_API_KEY` to a long random string, then:

```sh
curl -H "Authorization: Bearer $AGENT_API_KEY" https://your-app/api/agent/snapshot
```

That returns grades, assignments, calendar, readiness and connector health in
one call. `GET /api/agent` lists the narrower endpoints and describes itself.

The surface is read-only and refuses anything but GET. With `AGENT_API_KEY`
unset it is switched off completely. The key reaches a student's grades and
health data, so treat it like the login password.

## Tests

```sh
pytest tests/ -q
```

The suite runs without a database by stubbing the connection.
