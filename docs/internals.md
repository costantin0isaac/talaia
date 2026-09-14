# How it works

The decisions behind the behaviour, and what to check when something looks wrong.

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
