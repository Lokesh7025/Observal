<!-- SPDX-FileCopyrightText: 2026 Apoorv Garg <apoorvgarg.21@gmail.com> -->
<!-- SPDX-FileCopyrightText: 2026 Hari Srinivasan <harisrini21@gmail.com> -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# Troubleshooting

Common failure modes and their fixes. If none of these match, open a [GitHub Discussion](https://github.com/Observal/Observal/discussions) with the output of `observal auth status` and relevant logs from `docker compose logs`.

## Install and CLI

### `"Connection failed. Is the server running?"`

The CLI cannot reach the API. Check:

```bash
docker compose -f docker/docker-compose.yml ps     # API status
curl http://localhost/health                       # API health
observal config show                               # is server_url right?
```

If `server_url` is wrong:

```bash
observal config set server_url http://localhost
observal auth login
```

### `"System already initialized"` when logging in

The server already has users, so bootstrap is disabled. Use `observal auth login` with an email + password or an API key, not a fresh bootstrap flow.

## Docker and networking

### `port is already allocated`

Another process is on one of Observal's default ports. Remap host ports:

```bash
POSTGRES_HOST_PORT=5433 REDIS_HOST_PORT=6380 \
  docker compose -f docker/docker-compose.yml up --build -d
```

Full list in [Ports and volumes](ports-and-volumes.md).

### Service stuck in `starting`

The API depends on Postgres, the telemetry store, and Redis being healthy. Check each:

```bash
docker compose -f docker/docker-compose.yml ps
docker compose -f docker/docker-compose.yml logs observal-db
docker compose -f docker/docker-compose.yml logs observal-telemetry
docker compose -f docker/docker-compose.yml logs observal-redis
```

Common causes:

* Telemetry store refusing to start because another process holds `observal.duckdb.lock` (only one `observal-telemetry` may run)
* `TELEMETRY_TOKEN` mismatch between the API/worker and the telemetry container (API logs `401` from the store)

### Services restart in a loop

Check logs (`docker compose logs -f <service>`). Three frequent causes:

* Memory limit too tight. Bump limits in `docker-compose.yml`
* Corrupt volume. Wipe and restore from backup
* Config error introduced during an upgrade. Roll back

## Auth

### Admin forgot password

```bash
observal auth reset-password --email admin@demo.example
```

Then read the reset code from the server log:

```bash
docker logs observal-api 2>&1 | grep "PASSWORD RESET CODE"
```

Enter the code when the CLI prompts.

### OAuth login fails with `redirect_uri_mismatch`

The IdP doesn't have the right redirect URI registered. Add:

```
{FRONTEND_URL}/api/v1/auth/oauth/callback
```

with `FRONTEND_URL` set to your real external URL (scheme and host must match exactly).

### All users logged out after restart

Likely the `apidata` volume was recreated, so the JWT signing keys are new. Restore the `apidata` volume from backup, or accept that all sessions are invalid and everyone has to log in again.

## Telemetry

### Nothing in the dashboard

Run through, in order:

```bash
# 1. Are sessions arriving at all?
observal ops telemetry status

# 2. Are session hooks installed for the harness?
observal doctor --output json

# 3. Is the API reachable from the harness environment?
curl http://localhost/health
```

If hooks are missing, run `observal doctor patch --harness <harness>`. If sessions still are not arriving, check `~/.observal/telemetry_buffer.db`; growth indicates pending session delivery rather than silent loss.

### Telemetry store not receiving data

Check the `TELEMETRY_URL` the API is using and that the token matches:

```bash
docker compose -f docker/docker-compose.yml exec observal-api printenv TELEMETRY_URL
docker compose -f docker/docker-compose.yml exec observal-api \
  sh -c 'curl -fsS -H "Authorization: Bearer $TELEMETRY_TOKEN" "$TELEMETRY_URL/v1/stats"'
```

The source Compose default is `http://observal-telemetry:8125`. Server-package installs mount the token from `/run/secrets/telemetry_token`; confirm it is readable without printing it:

```bash
docker compose exec observal-api test -r /run/secrets/telemetry_token
docker compose exec observal-telemetry test -r /run/secrets/telemetry_token
```

Verify the store itself (row counts, file size, writer state):

```bash
curl -fsS http://127.0.0.1:8125/v1/health
curl -fsS -H "Authorization: Bearer $TELEMETRY_TOKEN" http://127.0.0.1:8125/v1/stats
```

A `503 telemetry_unavailable` from the API means the store is down or unreachable; ingest clients retry from their local outbox, so nothing is lost while you fix it. A `504 telemetry_timeout` means a read exceeded `TELEMETRY_QUERY_TIMEOUT_MS`; a `429 telemetry_busy` means the read pool is saturated. See [Telemetry service](telemetry-service.md).

## Web UI

### Blank white page

Frontend is still building. Check:

```bash
docker compose -f docker/docker-compose.yml logs -f observal-web
```

For local dev (running Next.js outside Docker), verify `NEXT_PUBLIC_API_URL` in `web/.env.local` matches your backend.

### Login redirects back to login immediately

Browser cookies aren't being set. Usually one of:

* `FRONTEND_URL` doesn't match the URL you're hitting.
* `CORS_ALLOWED_ORIGINS` doesn't include your frontend origin.
* You're on HTTP behind a proxy that's setting `secure` cookies. Terminate TLS at your proxy and keep `FRONTEND_URL=https://...`.

## Where to get more help

* Logs: `docker compose -f docker/docker-compose.yml logs -f`
* Health: `curl http://localhost/health`
* Status: `observal auth status`
* Community: [GitHub Discussions](https://github.com/Observal/Observal/discussions)
* Bugs: [GitHub Issues](https://github.com/Observal/Observal/issues). Please use Discussions for questions, Issues only for confirmed bugs
