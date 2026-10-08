# Deployment

Running Talaia on a server, and wiring it into an existing Prometheus and Grafana.

Talaia is a container and a PostgreSQL database, described by one compose file. Pick one of
two ways to get it.

## Option A — run the published image

Nothing to build. Download the deploy bundle from the
[latest release](https://github.com/costantin0isaac/talaia/releases/latest) it holds the
compose file, the example settings and monitoring files:

```sh
mkdir -p ~/talaia && cd ~/talaia
curl -fsSL -o bundle.tar.gz \
  https://github.com/costantin0isaac/talaia/releases/download/v1.3.1/talaia-v1.3.1-deploy.tar.gz
tar -xzf bundle.tar.gz --strip-components=1
```

The image runs on x86 servers and on ARM boards alike:

```sh
docker pull ghcr.io/costantin0isaac/talaia:1.3.1
```


## Option B — build from source

```sh
git clone https://github.com/costantin0isaac/talaia.git && cd talaia
docker build -t talaia:local .
```

Then set `TALAIA_IMAGE=talaia` and `TALAIA_TAG=local` in `.env` below.

## Configure

```sh
cp .env.example .env
```

Three settings have no sensible default:

```sh
TALAIA_IMAGE=ghcr.io/costantin0isaac/talaia
TALAIA_TAG=1.3.1                              # or latest
POSTGRES_PASSWORD=<generate one>
```

And point Talaia at the monitor file:

```sh
TALAIA_CONFIG_PATH=/app/config/monitors.yaml
```

Every other setting, and the monitor file format, is in [Configuration](configuration.md).

## Run it

```sh
docker compose -f compose.prod.yaml pull     # skip if built locally
docker compose -f compose.prod.yaml up -d
docker compose -f compose.prod.yaml logs -f app
```

Talaia runs on port 9999.
Users must be created in the CLI:

```sh
docker compose -f compose.prod.yaml exec app python -m talaia.auth add <username>
```

## Upgrading

Change the tag and restart:

```sh
sed -i 's/^TALAIA_TAG=.*/TALAIA_TAG=1.4.0/' .env
docker compose -f compose.prod.yaml pull
docker compose -f compose.prod.yaml up -d
```

Rolling back is the same command with the old tag, but read the
[changelog](../CHANGELOG.md) first, migrations apply automatically on start and are not
undone by running an older image.


## Applying configuration changes

Edit `config/monitors.yaml`, then restart the container, or reload without one:

```sh
docker compose -f compose.prod.yaml restart app
```

To reload in place, set a token:

```sh
TALAIA_API_TOKEN=$(openssl rand -hex 32)
```

`/api/*` then accepts `Authorization: Bearer <token>` instead of a session cookie:

```sh
curl -fsS -X POST -H "Authorization: Bearer $TALAIA_API_TOKEN" \
  https://talaia.example.org/api/reload
```

An invalid file comes back `422` and the running configuration is untouched.

## Backups

`incidents` and `daily_uptime` are the permanent record; everything else is configuration or
raw results that get pruned. The stack runs a `backup` sidecar that dumps the database on a
schedule:

```sh
TALAIA_BACKUP_DIR=./backups
TALAIA_BACKUP_KEEP_DAYS=14
TALAIA_BACKUP_EVERY_SECONDS=86400
```

Restoring:

```sh
docker compose -f compose.prod.yaml stop app
gunzip -c backups/talaia-20260101T030000Z.sql.gz \
  | docker compose -f compose.prod.yaml exec -T db psql -U talaia -d talaia
docker compose -f compose.prod.yaml start app
```

## Prometheus and Grafana

Talaia sits alongside Prometheus rather than replacing it:

- **Talaia notifies about state changes.** "X is down", "X recovered after 6m".
- **Grafana/Alertmanager notifies about trends.** "uptime below 99% this week".

Talaia keeps a short window of raw results (`TALAIA_RETENTION_DAYS`, default 30) plus
permanent daily rollups; Prometheus owns long-term storage.

### Scraping

```yaml
scrape_configs:
  - job_name: talaia          # the alerting rules match on this job name
    static_configs:
      - targets: ["talaia-host:9999"]

rule_files:
  - talaia-rules.yml
```

`/metrics` is unauthenticated by default. Blocking it at the proxy is enough until someone
adds a second proxy or mistypes the rule; a token makes the endpoint safe on its own:

```sh
TALAIA_METRICS_TOKEN=$(openssl rand -hex 32)
```

Prometheus then needs it too:

```yaml
    authorization:
      credentials: "<the same token>"
```

Leave it unset and nothing changes. `/healthz` and `/readyz` stay open either way — an
orchestrator cannot carry a secret and they reveal nothing.

### What is exported

| Metric | Type | Labels |
|---|---|---|
| `talaia_check_up` | gauge | `monitor`, `type`, `group` |
| `talaia_check_duration_seconds` | gauge | `monitor`, `type`, `group` |
| `talaia_checks_total` | counter | `monitor`, `result` |
| `talaia_monitor_consecutive_failures` | gauge | `monitor` |
| `talaia_monitors_total` | gauge | `status` |
| `talaia_certificate_days_remaining` | gauge | `monitor`, `type`, `group` |
| `talaia_build_info` | gauge | `version`, `commit` |

`talaia_check_up` is **absent** while a monitor is `unknown` or `paused`, rather than
reporting a misleading zero — alerting rules should use `talaia_check_up == 0`, not
`absent()`. Per-monitor values are read from the database at scrape time, so they cannot
drift from the real state.

Labels are deliberately limited to `monitor`, `type`, `group`, `result` and `status`. A URL,
an IP address or a status code must never become a label — that is how a metrics database
gets destroyed by cardinality.

### Dashboard and alerting rules

Import `grafana/talaia-dashboard.json` through **Dashboards → New → Import → Upload JSON**.
It asks for a Prometheus datasource rather than hard-coding one. Three sections — Health,
Performance, Reliability — in the order the questions get asked.

Read the first two panels before believing any of the others. **Talaia** is
`up{job="talaia"}`: if it reads DOWN, every other panel is stale. **Checking** is the
completed-check rate, which proves the scheduler is alive. Both depend on the scrape job
being named exactly `talaia`, as do the rules.

Check the rules before reloading Prometheus, the same way CI does:

```sh
promtool check rules prometheus/talaia-rules.yml
```

There is deliberately **no "monitor is down" alert** — Talaia already sends that itself, and
two messages for one event teaches you to ignore both. The rules cover what Talaia
structurally cannot report: that it is down, that its scheduler has stalled, that a monitor
never ran, accumulated short outages, flapping, latency degradation short of failure, and a
certificate expiring before Talaia's own warning would fire.

`TalaiaSchedulerStalled` is the one worth understanding. If the scheduler wedges while the
server keeps serving, `/healthz` and `/readyz` stay green and every monitor holds its last
known state — from outside, indistinguishable from everything being fine.
