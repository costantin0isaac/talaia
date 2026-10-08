# Changelog

## [1.3.1] - 2026-10-08

No changes to Talaia codebase.

### Added

- A published container image at `ghcr.io/costantin0isaac/talaia`, built for
  `linux/amd64` and `linux/arm64`.
- A deploy bundle attached to the release: the compose file, the example settings
  and monitor list, the backup script, the Prometheus alerting rules and the Grafana
  dashboard.


## [1.3.0] - 2026-09-18

### Added

- A 30-day window on the latency chart, alongside 1h, 24h and 7d.

### Changed

- The web UI works on a phone. The monitor and incident tables become stacked cards,
  the masthead wraps, statistics go two-wide, and the chart scales by aspect ratio
  instead of being letterboxed into a fixed height.
- The Grafana dashboard is reorganised into Health, Performance and Reliability
  sections, with Talaia's own liveness and check rate on it — a dead Talaia otherwise
  leaves every panel stale and looking healthy. Availability is a table sorted
  worst-first rather than a bar gauge.
- Uptime windows are labelled in days rather than hours: 7d and 30d, not 168h.
- Figures in the summary and uptime boxes are centred and aligned.
- The production deploy job ships the image its own pipeline built, rather than
  whatever tag the server's `.env` happened to name. Deploying a release no longer
  depends on remembering to edit a file on the host.

### Fixed

- Chart axis labels are no longer clipped or hidden behind the unit and timezone
  captions.

## [1.2.0] - 2026-09-17

### Added

- Active sessions can be listed and revoked.
- Repeated failed logins from one address are refused.
- An optional bearer token for `/metrics`.
- A configurable display timezone.
- Mean latency over 7 and 30 days on the monitor detail page.
- A single notification when Talaia starts.
- Scheduled database backups.
- An API token so CI can trigger a configuration reload.
- A manual production deploy job.
- Renovate configuration for dependency updates.

### Changed

- Dashboard performance: the uptime strip query is bounded, polling pauses in hidden
  tabs, and the status filter is remembered.
- Private configuration is kept out of built images, CI images are pinned, and images
  are pushed only from `main` and tags.

### Fixed

- Missing daily rollups are backfilled after an outage. Previously only yesterday and
  today were rolled up, so an outage spanning more than a day lost those days
  permanently.
- Redirect and cookie edge cases around login.
- Metrics count persisted checks only, and an outage produces one traceback rather
  than one per failed check.

## [1.1.0] - 2026-09-15

### Changed

- Branding and interface polish: logo, favicon, colour palette, and a simplified
  README.

## [1.0.0] - 2026-09-15

First release.

### Added

- Monitors are declared in `monitors.yaml` and reconciled atomically at startup and on
  reload. The file is the source of truth; there is no editing through the UI.
- HTTP, ICMP, TCP and TLS certificate expiry checks.
- A scheduler and a per-monitor state machine with configurable failure and recovery
  thresholds.
- PostgreSQL storage with Alembic migrations, hourly rollups and retention pruning.
- A JSON API and a session-authenticated web dashboard.
- Prometheus metrics at `/metrics`, with a Grafana dashboard and alerting rules.
- Notifications over ntfy on state change.
- A container image and a Docker Compose deployment.
