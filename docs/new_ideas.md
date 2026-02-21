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
