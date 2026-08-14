# New API About Monitor

This project is a standalone quota monitor for a New API/Sub2API deployment. It keeps snapshots in SQLite, serves the New API-style history page, estimates remaining capacity, and can send Bark alerts when the usage/percentage regression gets a statistically significant slope change.

## Quick start with Docker

```sh
cp .env.example .env
# Edit .env. Do not commit it.
docker compose pull
docker compose up -d
```

The page is available at `http://127.0.0.1:8320/`. Persistent history is stored in `./data/history.db`.

The required runtime values are `SUB2API_BASE_URL` and `SUB2API_ADMIN_KEY`. The four `*_URL` variables are optional overrides when the Sub2API routes are not standard. `BARK_ENABLED=true` enables a startup connectivity notification and later slope-change notifications; `BARK_URL` is the complete Bark endpoint.

For a reverse-proxied About page, set `FRAME_ANCESTORS` to the space-separated allowed origins, for example `https://newapi.example.com https://monitor.example.com`.

## Binary

The GitHub Action builds one-file binaries for Linux, Windows, and macOS. A binary serves the bundled page assets beside the executable and stores its SQLite data in a `data` directory beside it. Configure the same environment variables before starting it:

```sh
./newapi-about-monitor
```

The binary build uses PyInstaller and does not require Python on the target machine.

## GitHub Actions and releases

- Every tag matching `v*` builds the Docker image and pushes it to `ghcr.io/<owner>/newapi-about-monitor`.
- The same tag creates a GitHub Release containing the platform binaries and `SHA256SUMS`.
- `workflow_dispatch` runs the checks and builds without publishing a release.

The workflow only builds artifacts. Runtime credentials belong in the target host's environment or secret manager, never in GitHub source files. GitHub Actions uses `GITHUB_TOKEN` only to publish the image and release assets.

## Sampling policy

The collector normally records every 5 minutes (`NORMAL_INTERVAL_SECONDS=300`). It switches to 1-minute sampling when the cumulative main-account usage change, normalized to an equivalent five-minute interval, reaches either:

- `FAST_USAGE_AMOUNT_THRESHOLD` amount units; or
- `FAST_USAGE_REQUEST_THRESHOLD` requests.

Fast mode remains active for `FAST_HOLD_SECONDS` and can be extended by another large change. The decision uses cumulative usage deltas and the actual previous interval, so switching to 1-minute sampling does not by itself create a false usage spike.

## Slope and exhaustion model

The page's usage/percentage chart uses the main account only:

1. Repeated observations at the same rounded percentage are grouped and their amounts are median-smoothed over a three-point window.
2. Ordinary least squares fits `amount = intercept + slope * used_percent`; the slope is the estimated amount per percentage point.
3. A recursive two-sided change-point search requires at least five observations on each side, a relative slope change of at least 10%, and an adjusted two-sided normal-test p-value no greater than 0.05. Up to four changes are retained.
4. The selected slope is the segment after the last significant change. The chart marks every detected change and the Bark alert is emitted only for a newly observed marker.
5. Estimated total amount comes from the selected amount/percentage regression. Expected exhaustion time uses the remaining amount divided by the median positive amount-per-second rate across recent multi-sample spans. It no longer extrapolates from the rounded integer percentage jumps.

Existing markers are baselined when the container starts, so enabling Bark does not replay the whole historical database. A new marker is sent once; transient delivery failures are retried while the marker remains current.

## HTTP endpoints

- `GET /healthz` - container health check.
- `GET /api/status` - latest cached sources and sampling policy.
- `GET /api/history?hours=168` - snapshots and detected quota cycles.
- `POST /api/refresh` - force one collection immediately.

The server also serves `app/html/index.html` and its bundled VChart/font assets.
