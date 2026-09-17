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

### One message on startup

```sh
TALAIA_NOTIFY_ON_STARTUP=true    # the default
```

A low-priority message — "👁 talaia is watching, 20 monitors" — every time the process
starts. It is not an alarm and nothing needs acting on. It is there because otherwise the
only way to find out that notifications still work after a deploy is to break a service on
purpose, and a path that is never exercised is a path that quietly rots.

Set it false if a restart-heavy afternoon becomes annoying.

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
docker compose exec app python -m talaia.auth sessions isaac
docker compose exec app python -m talaia.auth revoke isaac 3f9a1c2b0d7e
docker compose exec app python -m talaia.auth revoke isaac all
```

`sessions` lists every login a user has open — when it started, when it was last used, when
it expires — each with a short id. `revoke` ends one by that id, or all of them. That is the
"log me out everywhere" for a lost phone; `passwd` and `disable` also end every session but
take the account with them.

Start the application with no users and it logs a warning at startup saying exactly that,
then serves a login page nobody can get past. It does not create a default account, because
a default account is a published password.

### The Secure cookie flag follows `TALAIA_BASE_URL`

A `Secure` cookie is never sent over plain `http://`. Get the flag wrong and the symptom is
a login that accepts your password and bounces you straight back to the form with no error —
the login worked, the cookie was simply never returned.

So it is derived rather than set: when `TALAIA_BASE_URL` starts with `https://` the cookie
is Secure, otherwise it is not. Reaching Talaia at `http://10.0.0.x:9999` and at
`https://talaia.example.org` both just work, provided `TALAIA_BASE_URL` says which one you
mean.

`TALAIA_SESSION_COOKIE_SECURE` still exists as an explicit override for the case where the
public URL and the URL you actually use disagree. Leave it unset otherwise.

### Times are shown in one timezone of your choosing

Everything is stored in UTC and always will be. What it is *rendered* as is a setting:

```sh
TALAIA_TIMEZONE=Europe/Madrid
```

That applies to the dashboard, the latency chart axis, the incident tables, the phone
notifications and the CLI, and every rendered time names its zone — `CET` or `CEST` rather
than a bare clock you have to guess at. Unset, everything reads `UTC`.

An unrecognised name stops the application at startup rather than quietly rendering the
wrong thing forever.

### Repeated failed logins are slowed down

argon2 makes each password guess cost about 50 ms. That is a speed bump, not a wall: left
alone, someone on the LAN could still try a thousand passwords an hour. So failures are
counted per client address, and once the budget is spent attempts are refused outright:

```sh
TALAIA_LOGIN_MAX_ATTEMPTS=5          # failures before the first lockout
TALAIA_LOGIN_LOCKOUT_SECONDS=60      # doubles with each further failure
TALAIA_LOGIN_MAX_LOCKOUT_SECONDS=900 # ceiling, so a bad afternoon is not a bad week
```

A refused attempt returns `429` with a `Retry-After` header and a page saying how long to
wait. A successful login clears the count, so mistyping twice and then getting it right
leaves no trace.

The count is held in memory and lost on restart. That is deliberate — a table would turn
every login attempt into a database write, which is precisely the amplification an attacker
would want.

Two limits worth knowing. Guesses spread across many addresses are not slowed; defending
that needs something that can see the whole network. And the address is only the real client
if `TALAIA_PROXY_IPS` names your reverse proxy — otherwise every attempt shares one bucket
and a single attacker locks out everyone.

### Behind a reverse proxy on another host

uvicorn only trusts `X-Forwarded-For` and `X-Forwarded-Proto` from `127.0.0.1`. If the
proxy runs on a different machine, every request appears to come from the proxy's address
— in the access log, and in anything that will ever key on the client address. Name the
proxy:

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
