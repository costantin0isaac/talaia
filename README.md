# Talaia

A small, self-hosted uptime monitor for a homelab.

Talaia periodically checks a set of targets — HTTP endpoints, hosts reachable by ICMP, open
TCP ports — records the results, tracks incidents, exposes Prometheus metrics, serves a
read-only dashboard, and pushes a notification when something goes down or comes back.

The name is Catalan for *watchtower*: a lookout post that watches the horizon and raises
the alarm.

Think of it as a deliberately smaller, more opinionated Uptime Kuma, with one crucial
difference.

## Monitors are configured in git, not in a web UI

This is the decision the rest of the design follows from. Monitor configuration lives in
`config/monitors.yaml`. The database stores **state and history only** — never
configuration typed in by a human.

```
edit monitors.yaml → commit → push → CI validates the schema → deploy → app reconciles
```

The benefits are the ordinary benefits of infrastructure-as-code: the configuration is
reviewable, diffable, revertible, and impossible to lose to a database mishap. A typo like
`intervall:` fails the pipeline instead of silently applying a default.

On startup the application reconciles the file against the database:

| In the file | In the database | Result |
|---|---|---|
| yes | no | inserted, `active = true` |
| yes, changed | yes | updated in place — same id, history and open incident retained |
| no | yes, active | soft-deleted (`active = false`); row and history are kept forever |
| reappears | yes, inactive | the original row is reactivated, old history intact |

Reconciliation runs in a single transaction, and an invalid file is rejected without
disturbing the configuration already running. **A broken config must never take down
monitoring that is currently working.**

`name` is the identity key, so renaming a monitor in the YAML is equivalent to deleting one
monitor and creating another. The old row is soft-deleted with its history; the new one
starts empty.

## Project status

Under active development, delivered in phases. **Phases 1 to 3 are complete; Phase 4 is
in progress.**

| Phase | Contents | Status |
|---|---|---|
| 1 | Scaffolding, settings, logging, database, config schema, HTTP checker, scheduler, JSON API | done |
| 2 | ICMP and TCP checkers, Prometheus metrics, ntfy notifications, retention, `/api/reload` | done |
| 3 | Web dashboard and monitor detail pages | done |
| 4 | Session authentication, TLS expiry checks, Grafana dashboard, alerting rules | in progress |

Deliberately out of scope: multi-tenancy, remote probes, high availability, databases other
than PostgreSQL, SSO, editing monitors through the UI, and headless-browser checks.

## Configuration

### Monitors

```yaml
# config/monitors.yaml
defaults:
  interval: 60              # seconds; per-monitor override allowed
  timeout: 10               # seconds; must be strictly less than the interval
  failure_threshold: 3      # consecutive failures before the state becomes DOWN
  recovery_threshold: 2     # consecutive successes before the state becomes UP

monitors:
  - name: open-webui              # required, unique, [a-z0-9-]+, max 64 characters
    type: http                    # http | icmp | tcp
    target: http://10.0.0.10:3001
    group: services               # optional; groups the dashboard and labels the metrics
    description: Local LLM front-end
    interval: 60
    enabled: true                 # optional; false keeps the monitor but stops checking it
    http:                         # type-specific block, only valid for type: http
      method: GET
      expected_status: [200]
      expected_body: null         # optional substring that must appear in the body
      follow_redirects: false
      verify_tls: true
      headers: {}

  - name: proxmox-node
    type: icmp
    target: 10.0.0.2

  - name: gitlab-ssh
    type: tcp
    target: 10.0.0.20:22

  - name: talaia-certificate
    type: tls
    target: talaia.example.org      # port defaults to 443
    interval: 3600
    tls:
      warn_days: 14                 # fail once fewer than this many days remain
```

Unknown keys are a hard error, not a warning.

**No secret ever belongs in this file.** It is committed and the repository is treated as
public. The committed `config/monitors.yaml` contains placeholder `10.0.0.x` targets only;
it doubles as the fixture that the CI `validate` stage parses. Real monitor configuration
lives on the deployment host, outside git, and is bind-mounted over that path.

### Environment

All runtime configuration is environment variables prefixed `TALAIA_`, parsed and validated
once at startup by `src/talaia/settings.py`. See [`.env.example`](.env.example) for the full
list. Copy it to `.env` for local development; `.env` is git-ignored and must stay that way.

`TALAIA_DATABASE_URL` is the only required variable, and it must use the asyncpg driver —
the application refuses to start on a `postgresql://` URL rather than failing confusingly
at the first query.

## Running it

```sh
cp .env.example .env          # only needed for local tooling; compose sets its own values
docker compose -f compose.dev.yaml up --build
```

The API is then on <http://localhost:9999>:

| Endpoint | Purpose |
|---|---|
| `GET /` | dashboard |
| `GET /monitors/{name}` | monitor detail page |
| `GET /healthz` | liveness: the process is serving |
| `GET /readyz` | readiness: the database answers and the scheduler is started |
| `GET /api/monitors` | every active monitor with its current state |
| `GET /api/monitors/{name}` | one monitor, with recent results and incidents |
| `GET /api/monitors/{name}/results?hours=24` | that monitor's raw results, newest first |
| `GET /api/incidents?limit=50&open=false` | incident history across every monitor |
| `GET /api/summary` | counts by status, open incidents, uptime over 24h |
| `POST /api/reload` | re-read and reconcile `monitors.yaml` |
| `GET /metrics` | Prometheus |
| `GET /docs` | generated OpenAPI documentation |

There are deliberately **no** `POST`/`PUT`/`DELETE` endpoints for monitors. `/api/reload`
re-reads the file; it does not accept one.

Migrations are applied by the container entrypoint before the server binds, so a deploy
never leaves the schema behind the code.

Configuration is read from `config/monitors.yaml`, which the dev compose file mounts
read-only. After editing it, either restart or:

```sh
curl -X POST http://localhost:9999/api/reload
```

Reload reconciles the file in one transaction and then resyncs the scheduler, so monitors
whose configuration did not change keep their task and their position within their interval.
An invalid file is rejected with `422` and the running configuration is left alone — a
broken edit cannot take down monitoring that is currently working.

`/readyz` is the endpoint to point a container healthcheck or an orchestrator at; `/healthz`
only says the process is up, which stays true while the database is unreachable.

**One worker only.** Each uvicorn worker would run its own scheduler and duplicate every
check. This is enforced in `__main__.py` and noted in the Dockerfile.

## Database and migrations

PostgreSQL only — there is no SQLite fallback. The schema is managed by Alembic from the
first commit, and migrations are applied on container start, before the server binds, so a
deploy never leaves the schema behind the code.

```sh
uv run alembic upgrade head        # apply migrations
uv run alembic downgrade -1        # step back one
uv run alembic check               # fail if a model has no matching migration
uv run alembic revision --autogenerate -m "add something"
```

Always read a generated migration before committing it. Autogenerate reliably misses
things: it silently dropped the descending order from the `check_results` index that the
monitor detail page depends on, and that index had to be written by hand.

## Retention

Raw check results are high-volume and short-lived; incidents and daily rollups are
permanent.

Once an hour a background task refreshes the `daily_uptime` rows for yesterday and today,
then deletes `check_results` older than `TALAIA_RETENTION_DAYS` (default 30). Rollups are
written **before** pruning, so a short retention window can never delete the results a
rollup was about to summarise.

Deletion happens in batches of 10,000, each in its own transaction, so a first prune over a
large backlog does not hold a lock for minutes.

| Table | Lifetime |
|---|---|
| `check_results` | `TALAIA_RETENTION_DAYS`, default 30 |
| `daily_uptime` | permanent |
| `incidents` | permanent |
| `monitors` | permanent, soft-deleted when removed from the YAML |

Days are UTC, matching Prometheus and the log timestamps.

## Notifications

### What Talaia notifies about, and what Grafana notifies about

These overlap in people's heads but not in practice, and it is worth being explicit:

- **Talaia notifies about state changes.** "web is DOWN", "web recovered after 6m". Event
  driven, immediate, and carrying the incident's own context.
- **Grafana and Alertmanager notify about trends.** "uptime below 99% this week", "p95
  latency elevated for 15 minutes". Window based, expressed in PromQL over `/metrics`.

A state change is a fact Talaia already owns; a trend is a question asked of the metrics.
Neither tool is a good substitute for the other, so run both.

### ntfy

Set three variables and notifications turn themselves on:

```sh
TALAIA_NTFY_URL=https://ntfy.example.org
TALAIA_NTFY_TOPIC=talaia-alerts
TALAIA_NTFY_TOKEN=tk_...          # optional, for a topic with access control
```

Leave `TALAIA_NTFY_TOPIC` empty and the application logs that notifications are disabled,
once, at startup, and runs normally. There is no other way to disable them.

`TALAIA_BASE_URL` is used for the `click` link on each message, so tapping a notification
opens that monitor's page.

Two messages per incident:

| Event | Title | Body | Priority |
|---|---|---|---|
| Down | 🔴 `<name>` is DOWN | target, error, timestamp | high |
| Up | 🟢 `<name>` recovered | downtime duration, timestamp | default |

### Why an outage produces exactly two messages

Notifications are tied to state *transitions*, not to failed checks, so a service that is
down for six hours produces one "down" message and one "recovered" message — not one per
failed check, and not a repeating reminder. There is no cooldown to configure because there
is nothing to suppress.

Delivery is fire-and-forget on a background task, retried twice with a short backoff, and a
failure is logged and dropped. **A notification can never delay or fail a check.** The cost
of that choice is that an unreachable ntfy server means a lost message rather than a delayed
one, which is the right trade for a monitor: the checking must not stop because the
announcing broke.

Before sending, Talaia stamps `notified_down_at` / `notified_up_at` on the incident row in a
single conditional `UPDATE`. Whoever wins the stamp sends the message, so the same event is
never announced twice even if the announcement is attempted again after a restart.

## The dashboard

Server-rendered Jinja2, progressively enhanced with HTMX. Two pages: the dashboard at `/`,
and a detail page per monitor at `/monitors/{name}`.

Each dashboard row polls **its own** partial every 15 seconds and swaps itself in place:

```html
hx-get="/partials/monitors/web/row" hx-trigger="every 15s" hx-swap="outerHTML"
```

The page itself never reloads. A full-page refresh would throw away your scroll position and
re-run every query on the page to update one number; per-row swapping costs one small query
per visible monitor and leaves the rest of the document alone. The summary bar refreshes the
same way.

### No CDN, no build step

`htmx.min.js` is **vendored** into `src/talaia/web/static/`, not loaded from a CDN, and the
latency chart is inline SVG whose coordinates are computed server-side in
`src/talaia/web/view.py`. There is no npm, no bundler and no external request.

This is not purity for its own sake. Talaia monitors the very network it is served from, so
the dashboard has to work exactly when the internet does not — which is precisely when you
are looking at it.

### Where the logic lives

The templates only interpolate values. Everything they render — the status strip, the
grouping, the chart geometry, the incident durations — is built by pure functions in
`view.py`, which is what makes it testable without a browser or a database.

Uptime windows come from different places on purpose: 24h is computed from raw
`check_results`, while 7d and 30d are summed from the `daily_uptime` rollups, which outlive
retention pruning.

## Signing in

Every page and every `/api/` endpoint requires a session. `/healthz`, `/readyz` and
`/metrics` do not — they are scraped by machines that will never hold a cookie.

There is no sign-up page and no user administration in the UI, because the alternative is
an unauthenticated endpoint that creates accounts. Users come from the command line:

```sh
docker compose exec app python -m talaia.auth add isaac
docker compose exec app python -m talaia.auth list
docker compose exec app python -m talaia.auth passwd isaac
docker compose exec app python -m talaia.auth disable isaac
```

Start the application with no users and it logs a warning at startup saying exactly that,
then serves a login page nobody can get past. It does not create a default account, because
a default account is a published password.

### The cookie flag that will bite you

`TALAIA_SESSION_COOKIE_SECURE` defaults to **true**, which means the browser will not send
the session cookie over plain `http://`. Reaching Talaia at `http://10.0.0.x:9999` with the
default leaves you at a login form that accepts your password and then bounces you straight
back to it, with no error — the login worked, the cookie was simply never returned.

Set it to `false` for plain-HTTP LAN access, or put Talaia behind HTTPS and leave it alone.
`compose.dev.yaml` already sets it false.

### How it is built

| Concern | Choice |
|---|---|
| Password storage | argon2id, library defaults, minimum 12 characters |
| Session token | 256 random bits, `SHA-256` hashed before storage |
| Cookie | `HttpOnly`, `SameSite=Lax`, `Secure` (configurable) |
| Expiry | `TALAIA_SESSION_TTL_HOURS`, default 30 days, absolute |

Only the *hash* of a session token is stored, so a copy of the `sessions` table cannot be
replayed as a set of live logins. The token itself is hashed with SHA-256 rather than argon2
because it already carries 256 bits of entropy — there is no weak secret to slow an attacker
down over, and argon2 on every authenticated request would be pure latency.

The expiry and the user's `active` flag are both part of the session lookup query, so a
disabled account loses its open sessions immediately and no cleanup step can be forgotten.
Changing a password deletes that user's sessions outright. Expired rows are swept by the
hourly retention task.

Failed logins do not say whether the username or the password was wrong, and an unknown
username is still checked against a dummy hash so the two paths take the same time.

### Using the API from a script

The API takes the same cookie, so `curl` needs a cookie jar:

```sh
curl -c jar -d 'username=isaac&password=...' http://localhost:9999/login
curl -b jar -X POST http://localhost:9999/api/reload
```

## Certificate expiry

A `tls` monitor completes a handshake and reports how much validity the certificate has
left. It **fails** once fewer than `warn_days` remain, default 14 — which is the point:
failing is what opens an incident and sends the notification, while there is still time to
renew.

```yaml
- name: talaia-certificate
  type: tls
  target: talaia.example.org   # or host:port; the port defaults to 443
  interval: 3600               # certificates do not change minute to minute
  tls:
    warn_days: 14
    server_name: null          # SNI override, when it differs from the host above
```

The error text says what you need to act on: `certificate expires in 5d`, or
`certificate expired 3d ago` once it is too late.

Remaining validity is also exported for Grafana, and is negative once the certificate has
expired:

```
talaia_certificate_days_remaining{monitor="talaia-certificate",type="tls",group="infra"} 45
```

Monitors that check no certificate export no series at all, rather than exporting zero —
zero days remaining is a real and very different condition.

### Certificates are verified, not just read

The handshake uses a normal verifying context, exactly as a browser would. A self-signed
certificate, an incomplete chain or a hostname mismatch is therefore a **failure**, not a
reading.

The alternative — disabling verification so the expiry date can be read off any certificate
— would leave the monitor green through precisely the faults that take a site down. A
checker called "is my TLS healthy" should not be the one component that accepts a
certificate nothing else will.

The cost is that this checker cannot watch a self-signed certificate. Use a `tcp` monitor
for the port and let the certificate go unchecked, or issue it from a CA the container
trusts.

## Troubleshooting

### Every ICMP check fails with a permission error

This is the most common stumbling block, and it is not a bug in Talaia.

ICMP checks use **unprivileged** datagram sockets, so the container does not need
`CAP_NET_RAW`. In exchange, the *host* kernel must allow the container's group ID range to
open ping sockets:

```yaml
services:
  app:
    sysctls:
      net.ipv4.ping_group_range: "0 2147483647"
```

Both compose files set this. Without it, every `type: icmp` monitor reports
`ping socket permission denied` while HTTP and TCP monitors work normally.

To confirm the host allows it:

```sh
docker compose -f compose.dev.yaml exec app sh -c 'cat /proc/sys/net/ipv4/ping_group_range'
```

### A TLS monitor fails with `certificate rejected`

Verification is intentionally on. The message names the reason — a self-signed certificate,
an untrusted issuer or a hostname that does not match. Check the target resolves to the host
you think, and use `tls.server_name` if the certificate is issued for a different name than
the one you are connecting to.

### A monitor is stuck in `unknown`

`unknown` means no threshold has been crossed yet. With the default
`failure_threshold: 3` and a 60-second interval, a monitor that is down from startup takes
three minutes to report `down`. That is deliberate: it is what stops a single blip from
raising an alarm.

## Prometheus and Grafana

Talaia exposes `/metrics` and expects to sit alongside Prometheus rather than replace it.
The division of responsibility is worth stating, because the overlap confuses people:

- **Talaia notifies about state changes.** "X is down", "X recovered after 6m".
  Event-driven, immediate, carrying incident context.
- **Grafana/Alertmanager notifies about trends.** "uptime below 99% this week", "p95
  latency elevated for 15 minutes". Window-based, expressed in PromQL.

They do not overlap, because they alert on different classes of thing. Talaia keeps only a
short window of raw results (`TALAIA_RETENTION_DAYS`, default 30) plus permanent daily
rollups; Prometheus owns long-term metric storage.

### Scraping

`GET /metrics` is plain-text Prometheus exposition, unauthenticated, on the same port as
the application. It is meant for the LAN and should never be published through a reverse
proxy.

```yaml
scrape_configs:
  - job_name: talaia
    static_configs:
      - targets: ["talaia-host:9999"]
```

| Metric | Type | Labels |
|---|---|---|
| `talaia_check_up` | gauge | `monitor`, `type`, `group` |
| `talaia_check_duration_seconds` | gauge | `monitor`, `type`, `group` |
| `talaia_checks_total` | counter | `monitor`, `result` |
| `talaia_monitor_consecutive_failures` | gauge | `monitor` |
| `talaia_monitors_total` | gauge | `status` |
| `talaia_build_info` | gauge | `version`, `commit` |

`talaia_check_up` is **absent** while a monitor is `unknown` or `paused`, rather than
reporting a misleading zero. Alerting rules should use `talaia_check_up == 0`, not
`absent()`.

Per-monitor values are read from the database when Prometheus scrapes, so they cannot
drift from the real state. `talaia_checks_total` is the exception: it is an in-memory,
process-lifetime counter, so it resets when Talaia restarts. That is normal and Prometheus
handles it.

Labels are deliberately limited to `monitor`, `type`, `group`, `result` and `status`. A
URL, an IP address, an error message or a status code must never become a label — that is
how a metrics database gets destroyed by cardinality.

## Development

Requires [uv](https://docs.astral.sh/uv/) and Docker.

```sh
uv sync --all-groups          # create .venv and install everything, including dev tools
cp .env.example .env          # then edit it

uv run ruff check .           # lint
uv run ruff format .          # format
uv run mypy                   # type check, strict
uv run pytest                 # full suite
uv run pytest -m "not integration"   # fast inner loop, no Docker needed
```

The interpreter is Python 3.14, managed by uv — the system Python is not used. Integration
tests start a real PostgreSQL container through testcontainers, so the Docker daemon must be
reachable.

**Known gap in the test suite:** the ICMP checker's tests cover how icmplib's results and
errors are turned into check outcomes, but never send a real ping. Whether a ping succeeds
depends on kernel and container settings that cannot be relied on in CI, and faking it
would prove nothing. The HTTP and TCP checkers are tested against a mocked transport and a
real local socket respectively.

Run the same three commands CI runs before pushing, and the pipeline will rarely surprise
you.

## Contributing and conventions

- Everything is in English: code, comments, docstrings, commit messages, variable names.
- Commit messages follow [Conventional Commits](https://www.conventionalcommits.org/)
  (`feat:`, `fix:`, `chore:`, `docs:`, `test:`, `ci:`).
- The repository is treated as public from the first commit. Secrets are scanned by
  `gitleaks` in the pipeline. A secret that reaches git history must be rotated, whatever
  is done to the history afterwards.

## Licence

[MIT](LICENSE).
