<p align="center">
  <img src="docs/images/readme-banner.png" alt="Talaia" width="900">
</p>

<p align="center">
  <em>Talaia is Catalan for <strong>watchtower</strong>: a lookout post that watches the
  horizon and raises the alarm.</em>
</p>

---

A small, self-hosted uptime monitor for your devices and services.
Schedule target checks, record results, track incidents, expose metrics to prometheus, push notifications.


## Monitor configuration is a YAML file

Configure your monitor targets in a single YAML file.
This offers some benefits if working with IaC and CI pipelines:
Configuration is reviewable, diffable and revertible, set up CI pipelines to fail on typos or other errors.


## What it does

| | |
|---|---|
| **Four check types** | HTTP, ICMP, TCP, and TLS certificate expiry |
| **Incidents** | Consecutive-failure and recovery thresholds |
| **Push notifications** | Uses ntfy on state change (failure, recovery) |
| **Prometheus metrics** | `/metrics` for scraping. Grafana dashboard in this repo |
| **A dashboard** | Server-rendered, HTMX-polled per row |
| **Session auth** | Session tokens, users created from CLI only|
| **Retention that keeps history** | Raw results are pruned; daily rollups and incidents are permanent |


## Getting it running

```sh
git clone <this-repo> talaia && cd talaia
cp .env.example .env
docker compose -f compose.dev.yaml up --build
```

That starts Talaia and a PostgreSQL container.

Create a user — there is no sign-up page:

```sh
docker compose -f compose.dev.yaml exec app python -m talaia.auth add <username>
```

Then open <http://localhost:9999> and sign in.

### Configure some monitors

`config/monitors.yaml` ships with placeholder targets. Point it at things you actually run:

```yaml
defaults:
  interval: 60              # seconds between checks
  timeout: 10               # must be strictly less than the interval
  failure_threshold: 3      # consecutive failures before the state becomes DOWN
  recovery_threshold: 2     # consecutive successes before it becomes UP

monitors:
  - name: open-webui
    type: http
    target: http://10.0.0.10:3001
    group: services
    http:
      expected_status: [200]

  - name: proxmox-node
    type: icmp
    target: 10.0.0.2
    group: infra

  - name: gitlab-ssh
    type: tcp
    target: 10.0.0.20:22

  - name: my-certificate
    type: tls
    target: example.org       # port defaults to 443
    interval: 3600
    tls:
      warn_days: 14           # fail once fewer than this many days remain
```

Unknown keys are a hard error, not a warning. Validate before committing:

```sh
uv run python -m talaia.config config/monitors.yaml
```

### Endpoints

| Endpoint | Purpose |
|---|---|
| `/` | dashboard |
| `/monitors/{name}` | monitor detail: uptime, latency chart, incidents |
| `/api/monitors`, `/api/incidents`, `/api/summary` | JSON API |
| `POST /api/reload` | re-read and reconcile `monitors.yaml` |
| `/healthz`, `/readyz` | liveness and readiness |
| `/metrics` | Prometheus |
| `/docs` | generated OpenAPI documentation |

There are deliberately **no** `POST`/`PUT`/`DELETE` endpoints for monitors.

## Documentation

[Configuration](docs/configuration.md) 
[Deployment](docs/deployment.md)


## Built with

Python, FastAPI, async SQLAlchemy, PostgreSQL, Alembic, Jinja2, HTMX, Prometheus,
and ntfy. Managed with `uv`, tested with `pytest` against a real PostgreSQL via
testcontainers, and deployed from GitLab CI.

## Licence

MIT. See [LICENSE](LICENSE).
