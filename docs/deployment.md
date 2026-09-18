# Deployment

Running Talaia on a real host, and wiring it into an existing Prometheus and Grafana.

## Deploying

Production runs the image CI built, not a local build, so what runs is what was tested.

### Once, on the host

```sh
sudo mkdir -p /opt/talaia && sudo chown "$USER" /opt/talaia
git clone <repo-url> /opt/talaia
cd /opt/talaia
cp .env.example .env
```

Fill in `.env`. The three that have no sensible default:

```sh
TALAIA_IMAGE=<registry-host>/<namespace>/talaia   # no tag here
TALAIA_TAG=latest
POSTGRES_PASSWORD=<generate one>
```

`TALAIA_DATABASE_URL` is **not** read from `.env` in production — `compose.prod.yaml` builds
it from `POSTGRES_USER`/`POSTGRES_PASSWORD`/`POSTGRES_DB` so the app and the database can
never disagree about the credentials.

Put the real monitor configuration at `/opt/talaia/config/monitors.local.yaml`. That name
is git-ignored, which is the point: `config/monitors.yaml` is **tracked** and carries
placeholder targets, so the `git reset --hard` in every upgrade would overwrite it. The
default `TALAIA_CONFIG_PATH` already points at the local file; set `TALAIA_CONFIG_DIR` in
`.env` if you would rather keep the configuration outside the checkout entirely.

Then:

```sh
docker login <registry-host>
docker compose -f compose.prod.yaml pull
docker compose -f compose.prod.yaml up -d
docker compose -f compose.prod.yaml logs -f app
```

Migrations run in the entrypoint before uvicorn binds, so the schema is never behind the
code. The first start creates the database and applies every migration.

### Create the first user

There is no sign-up page, and starting with no users logs a warning and serves a login
nobody can pass:

```sh
docker compose -f compose.prod.yaml exec app python -m talaia.auth add <username>
```

### Behind a reverse proxy

If the proxy runs on a **different host**, Talaia must publish its port — container-to-
container DNS does not cross hosts. `compose.prod.yaml` publishes 9999 for exactly that.

**Block `/metrics` at the proxy.** It is unauthenticated by design, for Prometheus on the
LAN, and it names every monitor, group and target you have. Publishing the domain without
this hands your infrastructure inventory to anyone who asks:

```caddyfile
talaia.example.org {
    @internal path /metrics /healthz /readyz
    respond @internal 403

    reverse_proxy <talaia-host>:9999
}
```

Set `TALAIA_BASE_URL` to the public `https://` URL: it is what notification links point
at, and it is what makes the session cookie Secure. Add the proxy's address to
`TALAIA_PROXY_IPS` so Talaia sees real client addresses instead of the proxy's.

### Deploying from CI

`main` and tag pipelines carry a manual `production` job. Pressing it fetches the new
revision on the Docker host, pulls the image, restarts the stack and waits for `/readyz` —
so a red job means a broken release, not merely a job that finished.

It uses the pipeline's own `CI_JOB_TOKEN` for both git and the registry, so the runner
needs no SSH key and no stored registry credentials.

Setting it up, once:

**1. Tag the runner.** The job wants a shell-executor runner on the Docker host tagged
`shell`. In GitLab: **Settings → CI/CD → Runners**, edit the runner, add the tag.

**2. Let the runner use Docker.**

```sh
sudo usermod -aG docker gitlab-runner
sudo systemctl restart gitlab-runner
```

**3. Share the deployment directory.** It is owned by you and written by the runner, so
give both a group and make new files inherit it:

```sh
sudo groupadd -f talaia
sudo usermod -aG talaia gitlab-runner
sudo usermod -aG talaia "$USER"
sudo chown -R "$USER":talaia /opt/talaia
sudo chmod -R g+rwX /opt/talaia
sudo find /opt/talaia -type d -exec chmod g+s {} +
sudo chmod 640 /opt/talaia/.env
```

**4. Let git work in a directory it does not own.** Without this, git refuses with
"detected dubious ownership" and the job fails on its first command:

```sh
sudo -u gitlab-runner git config --global --add safe.directory /opt/talaia
```

**5. Add the CI/CD variables.** **Settings → CI/CD → Variables**, both unprotected so tag
pipelines can see them:

| Key | Value |
|---|---|
| `TALAIA_DIR` | `/opt/talaia` |
| `TALAIA_URL` | the public URL, for the environment link |

Log out and back in for the group changes to apply to your own shell.

### Reloading the configuration from CI

The README describes a loop — edit `monitors.yaml`, commit, push, CI validates, deploy,
Talaia reconciles — but the last step needed someone to restart the container or press a
button. A token closes it:

```sh
TALAIA_API_TOKEN=$(openssl rand -hex 32)
```

With that set, `/api/*` accepts `Authorization: Bearer <token>` instead of a session cookie,
so the pipeline that deploys your configuration can apply it:

```sh
curl -fsS -X POST -H "Authorization: Bearer $TALAIA_API_TOKEN" \
  https://talaia.example.org/api/reload
```

An invalid file comes back `422` and the running configuration is untouched, so a bad
commit fails the pipeline instead of the monitoring.

The token opens the **JSON API only**, never the pages — a browser session is still the
only way to reach the dashboard. Unset, the API is reachable only with a login, and a token
presented against an unset setting is refused rather than accepted.

It also makes `curl` bearable for one-off queries, which previously meant a cookie jar.

### Backups

`incidents` and `daily_uptime` are the permanent record — everything else is either
configuration that lives in git or raw results that get pruned. They sit in one Docker
volume, so the stack runs a `backup` sidecar that dumps the database on a schedule:

```sh
TALAIA_BACKUP_DIR=./backups          # on the host
TALAIA_BACKUP_KEEP_DAYS=14
TALAIA_BACKUP_EVERY_SECONDS=86400
```

It shares the `postgres` image so `pg_dump` always matches the server version — a
mismatched client is the usual way a dump turns out not to restore. Each dump is written to
a `.partial` name and moved into place when complete, so an interrupted run is never
mistaken for a good backup by whatever copies these off the host.

**These are not an off-site backup.** They are on the same disk as the database they
protect, which covers the case you are actually likely to hit — a bad migration, a wrong
`DELETE`, a corrupted volume — and none of the cases where the machine is gone. Point
whatever already backs up that host at the directory.

Restoring:

```sh
docker compose -f compose.prod.yaml stop app
gunzip -c backups/talaia-20260101T030000Z.sql.gz \
  | docker compose -f compose.prod.yaml exec -T db psql -U talaia -d talaia
docker compose -f compose.prod.yaml start app
```

Stop `app` first: the scheduler writes continuously, and restoring underneath it gives you
a database that is half one thing and half another.

### Upgrading

```sh
cd /opt/talaia
git fetch && git reset --hard origin/main
docker compose -f compose.prod.yaml pull
docker compose -f compose.prod.yaml up -d
```

`git reset --hard`, never `git pull`: a hard reset is what guarantees the server matches the
repository. Never edit files on the server by hand — the next deploy discards them.

To pin a release instead of tracking `main`, set `TALAIA_TAG=v1.0.0` in `.env`. Every commit
also produces a `:$CI_COMMIT_SHORT_SHA` image, which is what you roll back to.

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
  - job_name: talaia          # the alerting rules match on this job name
    static_configs:
      - targets: ["talaia-host:9999"]

rule_files:
  - talaia-rules.yml
```

### Locking down `/metrics`

The endpoint is unauthenticated by default, and it names every monitor, group and type you
have. Blocking it at the reverse proxy is enough — until the day someone adds a second
proxy, or mistypes the rule. Setting a token makes the endpoint safe on its own:

```sh
TALAIA_METRICS_TOKEN=$(openssl rand -hex 32)
```

Prometheus then needs it too:

```yaml
scrape_configs:
  - job_name: talaia
    authorization:
      credentials: "<the same token>"
    static_configs:
      - targets: ["talaia-host:9999"]
```

Leave it unset and nothing changes. `/healthz` and `/readyz` stay open either way — an
orchestrator cannot carry a secret and they reveal nothing.

| Metric | Type | Labels |
|---|---|---|
| `talaia_check_up` | gauge | `monitor`, `type`, `group` |
| `talaia_check_duration_seconds` | gauge | `monitor`, `type`, `group` |
| `talaia_checks_total` | counter | `monitor`, `result` |
| `talaia_monitor_consecutive_failures` | gauge | `monitor` |
| `talaia_monitors_total` | gauge | `status` |
| `talaia_certificate_days_remaining` | gauge | `monitor`, `type`, `group` |
| `talaia_build_info` | gauge | `version`, `commit` |

`talaia_certificate_days_remaining` is exported only by `tls` monitors, and is negative
once the certificate has expired.

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

### Dashboard and alerting rules

Two files are committed, ready to use:

| File | What it is |
|---|---|
| `grafana/talaia-dashboard.json` | Grafana dashboard, import as-is |
| `prometheus/talaia-rules.yml` | example alerting rules |

Import the dashboard through **Dashboards → New → Import → Upload JSON**. It asks for a
Prometheus datasource rather than hard-coding one, and has `group` and `monitor` variables
for filtering. Nothing else needs configuring.

Check the rules before reloading Prometheus, the same way CI does:

```sh
promtool check rules prometheus/talaia-rules.yml
```

### What the rules deliberately do not alert on

There is **no "monitor is down" alert**, and that is the point of the split.

Talaia already sends that notification itself, immediately, with the incident attached.
A Prometheus rule saying the same thing would give you two messages for one event, arriving
minutes apart, and would teach you to ignore both.

What the rules cover instead is everything Talaia structurally cannot tell you:

| Alert | Why Talaia cannot report it |
|---|---|
| `TalaiaDown` | It is the thing that is down |
| `TalaiaSchedulerStalled` | Serving scrapes, checking nothing — no state ever changes, so nothing ever notifies |
| `TalaiaMonitorStuckUnknown` | A monitor that never ran has no transition to announce |
| `TalaiaUptimeBelowTarget` | Accumulated short outages that each recovered on their own |
| `TalaiaFlapping` | Each transition was reported correctly; the pattern is the problem |
| `TalaiaLatencyElevated` | Degradation short of failure is still a passing check |
| `TalaiaCertificateExpiringSoon` | Fires before Talaia's own `warn_days`, as a quiet warning first |

`TalaiaSchedulerStalled` is the one worth understanding. If the scheduler wedges while
uvicorn keeps serving, `/healthz` stays green, `/readyz` stays green, every monitor holds
its last known state, and Talaia goes permanently, silently quiet. From the outside that is
indistinguishable from everything being fine. `sum(rate(talaia_checks_total[10m])) == 0`
is what catches it.

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
