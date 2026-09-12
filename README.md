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

Under active development, delivered in phases. **Phase 2 is in progress.**

| Phase | Contents | Status |
|---|---|---|
| 1 | Scaffolding, settings, logging, database, config schema, HTTP checker, scheduler, JSON API | done |
| 2 | ICMP and TCP checkers, Prometheus metrics, ntfy notifications, retention, `/api/reload` | in progress |
| 3 | Web dashboard and monitor detail pages | planned |
| 4 | Session authentication, TLS expiry checks, Grafana dashboard, alerting rules | planned |

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
| `GET /healthz` | liveness |
| `GET /api/monitors` | every active monitor with its current state |
| `GET /api/summary` | counts by status, open incidents, uptime over 24h |
| `GET /docs` | generated OpenAPI documentation |

Migrations are applied by the container entrypoint before the server binds, so a deploy
never leaves the schema behind the code.

Configuration is read from `config/monitors.yaml`, which the dev compose file mounts
read-only. Editing it takes effect on the next restart; `POST /api/reload` arrives in
Phase 2.

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
