# Private fantasy desk

A small read-only frontend for the existing lineup, waiver and trade engines.
It reads the latest completed live roster week; it does not display old saved
recommendations as new advice. The frontend never receives DB or ESPN credentials.

## Run

Install with `uv sync --extra dev --extra web`. Set `DATABASE_URL` to the runtime
pooler URL, `DEMO_LEAGUE_ID` to the provider league ID, and `DEMO_SEASON` to the
season. Then:

```sh
uv run gunicorn 'espn_ff_assistant.web.app:create_app(prewarm=True)' --bind 127.0.0.1:8080 --workers 1 --threads 4 --timeout 120
```

**Host only behind an authenticating proxy.** The deployed Devin App uses Devin
organization sign-in; the WSGI app deliberately does not invent its own password
system. Keep the server bound to loopback so direct network clients cannot bypass
the proxy. Do not expose the local port or deploy it to an unprotected public host.
Devin App visits wake the managing session, but archiving/ending that session
takes it offline. There is no Vercel project or billable service provisioned here.

Start a replacement worker on a different loopback port, then wait for
`curl --fail http://127.0.0.1:8080/ready` to succeed before routing the authenticated
proxy to it. The startup warmup calculates all three tabs. `/ready` returns 503
until every tab has a cached result and 200 when ready; it contains no league data.
This prevents an interview demo from paying the cold calculation cost on a tab
click. Keep the old worker serving until the replacement is ready.

Each section runs a deduplicated background calculation. Reads run in a
database-enforced read-only, repeatable-read transaction; no call can persist
recommendations, send SMS, call a paid model or submit to ESPN. Calculations cache
for five minutes from calculation start and refresh on the next visit. A failed refresh preserves the
previous result with a warning. No league data is written to disk or source control.

Trade discovery uses the existing capped two-per-side search, reusing baseline
and roster-fill calculations within each partner's search without reducing the
candidate set. Startup warmup, not the first tab visit, pays its cold cost.
It checks ESPN's exact trade deadline, game locks,
ownership and configured roster/position limits. Results are model estimates,
not a substitute for ESPN eligibility checks. Rival fit is ordinal, not a
probability; sample sizes and fallback sources appear in each explanation.

The header always shows the source week and original snapshot timestamp. Source
snapshots older than eight hours get a warning. Reloading the page does not sync
ESPN or change the worker's schedule. The page reads the worker's saved state.

No raw provider payloads, manager identities, DB URLs or user IDs are returned.
All responses are marked `private, no-store`; scripts/styles are local and CSP
disallows third-party scripts, connections and forms. There is no service worker.
