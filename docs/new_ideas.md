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

---

## WebSocket for Bot ↔ Hub Communication

Replace HTTP POST/GET between trading bots and the hub with persistent
WebSocket connections.

**Current state:** Bots use `aiohttp.ClientSession` (HTTP keep-alive) to
POST reports and trade events to the hub every tick. Ack confirmations
piggyback on the report response. Works fine — sub-millisecond on Docker
network, keep-alive reuses TCP connections.

**Where WebSocket would NOT help much:**
- **Report cycle** (bot → hub every few seconds): 2-5KB JSON, HTTP
  keep-alive already eliminates TCP overhead. WebSocket saves maybe
  200-500 bytes of header framing. Noise on a local Docker network.
- **Trade push** (bot → hub on open/close): infrequent events, a few per
  hour. HTTP request-response is actually ideal — send, get confirmation,
  done.
- **Ack confirmations**: currently delayed up to one report cycle (~5s).
  WebSocket could push instantly, but 5s ack delay has zero operational
  impact — the pending buffer is just a safety net.

**Where WebSocket WOULD help:**
- **Hub-initiated commands**: "close all positions NOW", "new opportunity,
  act fast", "config change — reload". Currently impossible without
  polling. A persistent WS channel enables real-time push from hub to bots.
- **Event streaming**: live trade events, position updates, risk alerts
  pushed to the dashboard without polling.
- **Reduced connection churn**: if tick frequency increases (sub-second),
  WS eliminates per-request overhead entirely.

**Verdict:** No performance gain for current architecture. The win is
enabling new capabilities (hub-initiated commands, real-time event push)
rather than raw throughput. Worth building if we add hub → bot command
flow or if tick frequency goes sub-second.

**Status:** Parked. Revisit when hub-initiated commands become a need.

---

## Hub-Managed Balance Allocation

Currently each bot has its own `SESSION_BUDGET` env var — a self-imposed cap
with no inter-bot coordination. If two bots each claim $3000 from a $5000
wallet, they'll over-allocate and eventually fail on insufficient margin.

**The fix:** Hub queries the exchange wallet once, then divides capital across
enabled bots based on their profiles (equal split, weighted by strategy risk,
or explicit per-bot allocation in config). Each bot receives its ceiling from
the hub at startup and on every report cycle. If a bot is disabled, its share
is redistributed.

**Behavior today (honor system):**
- No `SESSION_BUDGET` → bot uses full wallet balance (dangerous with multiple bots)
- `SESSION_BUDGET=1000` → bot caps itself at $1000 notional, others unaware
- `SESSION_BUDGET > wallet` → bot thinks it has money it doesn't, margin errors

**Safe rule for now:** `sum(all bot SESSION_BUDGET) <= exchange wallet balance`.
Keep budgets conservative. The hub-managed version is the real solution.

**Status:** Not started. Build before going live with multiple bots on real money.

---

## Trading Philosophy — Volatility as Opportunity

1. **Volatility is not RISK — it's an OPPORTUNITY.**
   - Never throttle or avoid trades solely because volatility is high.
   - High volatility = bigger moves = more profit potential.
   - Risk management limits *losses*, not *exposure to movement*.
   - Strategies should lean INTO volatility spikes, not shy away.
