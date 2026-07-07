# Deploying Jarvis to Railway

This app is a multi-tenant SaaS: students sign up, bring their own Anthropic
API key (so you pay nothing for their AI usage), pick or create their school,
and pay a monthly Stripe subscription. This runbook takes you from an empty
Railway project to a live, sellable deployment.

## 1. Create the Railway project

1. New Project → **Deploy from GitHub repo** → pick this repo. Railway reads
   `railway.json` and builds from the `Dockerfile`.
2. Add the **PostgreSQL** plugin. Railway injects a `DATABASE_URL` reference —
   attach it to the service (Variables → add reference to the plugin's
   `DATABASE_URL`).
3. Service settings: confirm **replicas = 1** (required — the in-process
   APScheduler double-fires with multiple replicas) and healthcheck path
   `/healthz`. Both come from `railway.json`.

## 2. Generate and set secrets

Generate two keys locally and **store copies in a password manager** — losing
`CONFIG_ENCRYPTION_KEY` permanently bricks every stored API key and OAuth token:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(48))"          # SECRET_KEY
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"  # CONFIG_ENCRYPTION_KEY
```

### Required env vars

| Variable | Purpose |
|---|---|
| `DATABASE_URL` | Postgres (reference the plugin) |
| `SECRET_KEY` | Flask session signing |
| `CONFIG_ENCRYPTION_KEY` | Fernet key encrypting per-user secrets at rest |
| `STRIPE_SECRET_KEY` | Stripe API (live mode) |
| `STRIPE_WEBHOOK_SECRET` | Stripe webhook signature (from step 4) |
| `ADMIN_USER` / `ADMIN_PASSWORD` | Break-glass admin login (do NOT leave the default) |

### Optional / app-level env vars

| Variable | Purpose |
|---|---|
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` / `GOOGLE_REDIRECT_URI` | One Google OAuth app; each user connects their own account |
| `WHOOP_CLIENT_ID` / `WHOOP_CLIENT_SECRET` / `WHOOP_REDIRECT_URI` | One WHOOP OAuth app; each user connects their own account |
| `MEM0_API_KEY` | Long-term memory (owner-paid, cheap at hobby scale) |
| `FINNHUB_API_KEY` / `NOAA_API_TOKEN` / `GUARDIAN_API_KEY` | Shared free-tier data feeds |
| `NTFY_SERVER` | Push server (default `https://ntfy.sh`); topics are per-user |
| `PARENT_USER` / `PARENT_PASSWORD` | Enables the single-family parent portal (leave unset for a pure SaaS) |

**Do NOT set** `ANTHROPIC_API_KEY` in production — every user supplies their
own. (If you do set it, it only migrates once to the default student and is
otherwise ignored.) The old `APP_PASSWORD`, `AVERAGE_USER`, `NTFY_TOPIC`,
`SECURITY_CODE`, `POWER_USERN/PASS`, and all `*_ICAL_URL` / `CANVAS_*` vars are
no longer used — schools and per-user settings replace them.

## 3. First deploy

Push to your default branch. On boot, `init_db()` runs every migration
(creating tables, adding `user_id` columns + NOT NULL/FK constraints, seeding
Park City High School). Watch the logs for `Database initialized successfully`,
then hit `https://<your-app>/healthz` — it should return
`{"status":"ok","db":true,"scheduler":true}`.

## 4. Configure Stripe (live mode)

1. In the Stripe dashboard, create a **Product** with a recurring monthly
   **Price**.
2. Log into `/admin` on your deployment (with `ADMIN_PASSWORD`), open the
   pricing panel, and paste the Price ID.
3. Add a webhook endpoint at `https://<your-app>/api/webhooks/stripe`
   subscribed to: `checkout.session.completed`,
   `customer.subscription.created`, `customer.subscription.updated`,
   `customer.subscription.deleted`, `invoice.paid`, `invoice.payment_failed`.
4. Copy the webhook's **signing secret** into `STRIPE_WEBHOOK_SECRET` and
   redeploy.

## 5. Custom domain & OAuth redirects

1. Add your domain in Railway → Service → Settings → Networking, then point a
   CNAME at the Railway target.
2. Update `GOOGLE_REDIRECT_URI` / `WHOOP_REDIRECT_URI` to
   `https://<domain>/google-auth/callback` and `.../whoop-auth/callback`, and
   add those exact URLs to the authorized-redirect lists in the Google and
   WHOOP developer consoles.

## 6. Launch checklist

- [ ] `/healthz` green
- [ ] Admin login works; default `ADMIN_PASSWORD` changed
- [ ] Existing student (Finn) can log in; his data, school, and API key
      migrated (check Settings → AI shows a key on file)
- [ ] Mint a beta access code in `/admin` and run a full signup: enter a real
      Anthropic key (the form validates it live), pick/create a school, pay
      with a live card, land in the app, then cancel from `/billing`
- [ ] Confirm a second test user cannot see the first user's tasks/chat

## Access model

By default signup requires an **access code** (mint them in `/admin`). To open
fully self-serve signup, flip `open_signup` in the admin pricing panel. Comped
accounts (`is_comped`) skip payment entirely — use them for friends and beta
users.
