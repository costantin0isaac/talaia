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

Under active development, delivered in phases. **Phase 1 is in progress.**

| Phase | Contents | Status |
|---|---|---|
| 1 | Scaffolding, settings, logging, database, config schema, HTTP checker, scheduler, JSON API | in progress |
| 2 | ICMP and TCP checkers, Prometheus metrics, ntfy notifications, retention, `/api/reload` | planned |
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
