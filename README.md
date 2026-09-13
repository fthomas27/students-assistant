# Schola Registry

A focused academic dashboard: what you're graded on, what's due, and whether
you're rested enough to do it.

Three connectors feed it — **Canvas**, **PowerSchool**, and **WHOOP** — and four
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

None of the three are required — the app renders with whatever is configured,
and Sync & Feeds shows the state of each.

- **Canvas** — paste your calendar feed URL in the app, or set `CANVAS_ICAL_URL`.
  That gives titles and due dates. For grades, open **Sync & Feeds** and fill in
  the Canvas card: your Canvas URL plus the username and password you sign in
  with. If your district does issue personal access tokens, set
  `CANVAS_API_TOKEN` instead — it is preferred when present. `GET
  /api/canvas/debug` prints a step-by-step trace if sign-in misbehaves.
- **PowerSchool** — set `POWER_USERN`, `POWER_PASS`, and `PS_BASE_URL`. Scraped
  with a headless browser on weekdays at 07:12 and 15:12.
- **WHOOP** — set `WHOOP_CLIENT_ID` and `WHOOP_CLIENT_SECRET`, then connect from
  the Readiness page. Without it, the page renders sample data, clearly labelled.

See `CLAUDE.md` for the full environment variable list and architecture notes.

## Tests

```sh
pytest tests/ -q
```

The suite runs without a database by stubbing the connection.
