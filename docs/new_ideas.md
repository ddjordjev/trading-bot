# New Ideas — Backlog

> Ideas already transferred to `docs/summary.html` section 6 are removed from here.
> Only items not yet in the summary live below.

---

## Kafka for Internal Communication

Consider adding Apache Kafka as a message bus between bots and the central
dashboard/monitor if HTTP POST latency becomes a bottleneck. Current architecture
uses direct API calls from bots → bot-momentum's `/internal/report` endpoint
which holds state in memory.

**When to consider:** If dashboard updates feel laggy, if bot count grows beyond
3-5, or if we need reliable message ordering / replay.

**Trade-off:** Kafka adds operational complexity (another service, topic
management, consumer groups). The current approach is simpler and sufficient
for live dashboard data that only cares about the latest state, not history.

**Status:** Parked. Revisit if performance degrades.

---

## Automated UI Test Tool (Browser-Based Self-Diagnosis)

Build a test tool that spins up alongside Docker and crawls every page/tab of the
dashboard like a real user would. It should catch the kinds of issues a human
notices — pages that load but do nothing, tabs that are completely dead, broken
links (e.g. summary page links that don't load their targets), API endpoints
returning errors behind the scenes, empty tables that should have data, etc.

**Scope:**
- Navigate to every route in the dashboard (Dashboard, Scanner, Analytics,
  Monitoring, Modules, Settings, Summary)
- Click every tab/sub-tab within each page
- Verify that key elements actually render (positions table, charts, balance
  card, trade queue, etc.) — not just "page loaded 200 OK"
- Follow links (especially on the Summary page) and verify they resolve
- Check for JS console errors, failed network requests, empty/broken iframes
- Check API endpoints the frontend depends on (`/api/status`, `/api/positions`,
  `/api/news`, `/api/system-metrics`, `/api/grafana-url`, `/api/summary-html`,
  `/health`, etc.) — verify they return valid data, not error payloads
- Report a clear pass/fail per page with details on what's broken

**Self-diagnosable output:**
- The report should be detailed enough that an AI agent can read it, understand
  exactly what's wrong, fix the code, rebuild, and re-run the tests — without
  needing a human to describe the problem
- Include: page URL, expected behavior, actual behavior, screenshot (optional),
  network requests that failed, console errors captured
- Machine-readable format (JSON or structured markdown) so the agent can parse
  it programmatically

**Workflow:**
1. `docker compose up -d` (build the full stack)
2. Wait for all services healthy
3. Run the UI test tool against `http://localhost:9035`
4. Tool outputs a report
5. Agent reads report, fixes issues, rebuilds, re-runs — loop until clean

**Tech options:** Playwright, Puppeteer, or the built-in browser-use agent.
Prefer Playwright (Python, headless, good assertion library).

**Status:** Not started. Build when ready to do a full UI quality pass.

---

## Test Coverage Strategy — Surge & Coast

The `fail_under` floor stays at 80%. The workflow:

1. **Surge** — when coverage drops near or below 80%, push it back up to
   85-90%. Focus on critical areas (exchange, orders, risk, daily target)
   for deeper coverage. Less critical areas (intel clients, frontend glue)
   just need enough to clear the bar.
2. **Coast** — when adding new features, write basic tests for the new code
   but don't obsess over hitting 90% again. Let coverage naturally drift
   down as new lines are added.
3. **Repeat** — when it drops below 80% again, do another surge. Rinse and
   repeat.

The point: maintain a meaningful safety net without wasting time writing
filler tests every commit. The surges build a buffer; the coasts spend it.

**Status:** Active policy. Current coverage: ~91% (post-surge).
