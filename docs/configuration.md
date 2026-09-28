# Configuration

Everything that shapes what Talaia watches and how it behaves.

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

### Environment

All runtime configuration is environment variables prefixed `TALAIA_`, parsed and validated
once at startup by `src/talaia/settings.py`. See [`.env.example`](.env.example) for the full
list. Copy it to `.env` for local development.

`TALAIA_DATABASE_URL` is the only required variable, and it must use the asyncpg driver —
the application refuses to start on a `postgresql://` URL rather than failing confusingly
at the first query.

## Notifications

### What Talaia notifies about

- **Talaia notifies about state changes.** "web is DOWN", "web recovered after 6m".
Talaia incidents and notifications are simple by design.

Talaia exposes metrics to prometheus, a Grafana example dashboard is provided in this repo.
You can achieve more complex notifications using Grafana and Alertmanager using PromQL:
- **Grafana and Alertmanager notify about trends.** "uptime below 99% this week", "p95
  latency elevated for 15 minutes".


### ntfy

Set three variables to turn on notifications:

```sh
TALAIA_NTFY_URL=https://ntfy.example.org
TALAIA_NTFY_TOPIC=talaia-alerts
TALAIA_NTFY_TOKEN=tk_...          # optional, for a topic with access control
```

Leave `TALAIA_NTFY_TOPIC` empty and the application logs that notifications are disabled,
once, at startup, and runs normally.

`TALAIA_BASE_URL` is used for the `click` link on each message, so tapping a notification
opens that monitor's page.

Two messages per incident:

| Event | Title | Body | Priority |
|---|---|---|---|
| Down | 🔴 `<name>` is DOWN | target, error, timestamp | high |
| Up | 🟢 `<name>` recovered | downtime duration, timestamp | default |

### One message on startup

```sh
TALAIA_NOTIFY_ON_STARTUP=true    # the default
```

A low-priority message — "👁 talaia is watching, 20 monitors" — every time the process
starts. It is not an alarm and nothing needs acting on. It is there to find out that notifications still work after a deploy.

### Outage notifications

Notifications are tied to state *transitions*, not to failed checks, so a service that is
down for six hours produces one "down" message and one "recovered" message.

Before sending, Talaia stamps `notified_down_at` / `notified_up_at` on the incident row in a
single conditional `UPDATE`. The same event is never announced twice even if the announcement is attempted again after a restart.

## Signing in

Every page and every `/api/` endpoint requires a session. `/healthz`, `/readyz` and
`/metrics` do not — they are scraped by machines that will never hold a cookie.

There is no sign-up page and no user administration in the UI, users come from the command line:

```sh
docker compose exec app python -m talaia.auth add user
docker compose exec app python -m talaia.auth list
docker compose exec app python -m talaia.auth passwd user
docker compose exec app python -m talaia.auth disable user
docker compose exec app python -m talaia.auth sessions user
docker compose exec app python -m talaia.auth revoke user 3f9a1c2b0d7e
docker compose exec app python -m talaia.auth revoke user all
```

`sessions` lists every login a user has open — when it started, when it was last used, when
it expires — each with a short id. `revoke` ends one by that id, or all of them. 
`passwd` and `disable` also end every session.

Starting the application with no users produces a log warning at startup.


### Times are shown in one timezone of your choosing

Everything is stored in UTC. What it is *rendered* as is a setting:

```sh
TALAIA_TIMEZONE=Europe/Madrid
```

That applies to the dashboard, the latency chart axis, the incident tables, the phone
notifications and the CLI, and every rendered time names its zone. Unset, everything reads `UTC`.


### Repeated failed logins are slowed down

```sh
TALAIA_LOGIN_MAX_ATTEMPTS=5          # failures before the first lockout
TALAIA_LOGIN_LOCKOUT_SECONDS=60      # doubles with each further failure
TALAIA_LOGIN_MAX_LOCKOUT_SECONDS=900 # ceiling
```

A refused attempt returns `429` with a `Retry-After` header and a page saying how long to
wait. A successful login clears the count.
The count is held in memory and lost on restart.

### Behind a reverse proxy on another host

uvicorn only trusts `X-Forwarded-For` and `X-Forwarded-Proto` from `127.0.0.1`.
If the proxy runs on a different machine, every request appears to come from the proxy's address.
Name the proxy:

```sh
TALAIA_PROXY_IPS=10.0.0.6          # comma-separated for several, or * for any
```

### How it is built

| Concern | Choice |
|---|---|
| Password storage | argon2id, library defaults, minimum 12 characters |
| Session token | 256 random bits, `SHA-256` hashed before storage |
| Cookie | `HttpOnly`, `SameSite=Lax`, `Secure` (configurable) |
| Expiry | `TALAIA_SESSION_TTL_HOURS`, default 30 days, absolute |

Only the *hash* of a session token is stored, so a copy of the `sessions` table cannot be
replayed as a set of live logins. 

The expiry and the user's `active` flag are both part of the session lookup query, so a
disabled account loses its open sessions immediately and no cleanup step can be forgotten.
Changing a password deletes that user's sessions outright. Expired rows are swept by the
hourly retention task.

Failed logins do not say whether the username or the password was wrong, and an unknown
username is still checked against a dummy hash so the two paths take the same time.

### Using the API from a script

The API takes the same cookie, so `curl` needs a cookie jar:

```sh
curl -c jar -d 'username=user&password=...' http://localhost:9999/login
curl -b jar -X POST http://localhost:9999/api/reload
```

## Certificate expiry

A `tls` monitor completes a handshake and reports how much validity the certificate has
left. It **fails** once fewer than `warn_days` remain, default 14.

```yaml
- name: talaia-certificate
  type: tls
  target: talaia.example.org   # or host:port; the port defaults to 443
  interval: 3600               # check interval in seconds
  tls:
    warn_days: 14
    server_name: null          # SNI override, when it differs from the host above
```

Remaining validity is also exported for Grafana, and is negative once the certificate has
expired:

```
talaia_certificate_days_remaining{monitor="talaia-certificate",type="tls",group="infra"} 45
```


### Certificates are verified, not just read

The handshake uses a normal verifying context, exactly as a browser would. A self-signed
certificate, an incomplete chain or a hostname mismatch is therefore a **failure**, not a
reading.

The cost is that this checker cannot watch a self-signed certificate. Use a `tcp` monitor
for the port and let the certificate go unchecked, or issue it from a CA the container
trusts.
