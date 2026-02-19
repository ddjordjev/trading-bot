# Token Consumption Estimate — 24-Hour Sprint (Feb 18-19, 2026)

## Raw Data: Stored Conversation Text

| Source | Size |
|--------|------|
| Session 0 (retro-poker transcript) | 1,273 KB |
| Session 1 (trading-bot transcript .txt + .jsonl) | 811 KB |
| Sessions 2–8 (9 additional jsonl transcripts) | 373 KB |
| Chat history + archives + chat-log | 59 KB |
| **Total raw stored text** | **~2.5 MB** |

Raw stored text converts to ~625,000 tokens. But that only captures the
final logged text — not the actual API consumption.

## Why Actual Token Usage Is Much Higher

Three multipliers inflate the raw number:

1. **Context accumulation** — every turn resends the full conversation so
   far. A 25-turn session costs roughly 25×13 tokens (triangle sum), not
   25×1, because each message includes all prior messages.

2. **System overhead per request** — workspace rules, tool definitions,
   file attachments, and git context add ~12,000–15,000 tokens to every
   single API call.

3. **Subagents** — sessions 4, 5, and 7 each launched 4 parallel
   exploration agents. Each subagent gets its own full context window with
   system prompt + task description + tool calls. That's 12+ separate API
   call chains on top of the main conversations.

## Estimated Total Token Consumption

| Category | Input Tokens | Output Tokens |
|----------|-------------|---------------|
| 9 main sessions (~20 turns avg, growing context) | 10–14M | 1.5–2.5M |
| 12+ subagent launches | 2–3M | 0.5–1M |
| Extended thinking (hidden reasoning) | — | 2–4M |
| **Total** | **~12–17M** | **~4–7.5M** |

**Grand total: roughly 16–25 million tokens** over 24 hours.

## Cost on a Paid API (Feb 2026 pricing)

| Model / Platform | Input $/M | Output $/M | Low Est. | High Est. |
|-----------------|-----------|------------|----------|-----------|
| Claude Sonnet 3.5 | $3 | $15 | $96 | $163 |
| Claude Opus-class | $15 | $75 | $480 | $818 |
| GPT-4o | $2.50 | $10 | $70 | $118 |
| **Realistic mix** (Opus main, Sonnet/Haiku subagents) | ~$8 | ~$35 | **$236** | **$398** |

### Best estimate: ~$150–300 on the API

Most sessions ran on a Sonnet/Opus mix with thinking enabled.

## What Was Produced

- 27 git commits
- ~100+ files created or modified
- 1,044 tests written from scratch
- Full React dashboard (7 pages) + FastAPI backend
- 10 intel data source integrations
- 8 trading strategies
- 3-service Docker architecture
- CI pipeline
- Multiple rounds of bug hunting and fixing
- Preflight checks, smoke tests, signal flow verification

## Sessions Covered

| # | Timestamp | Focus |
|---|-----------|-------|
| 0 | Feb 18 evening | Initial codebase (30+ files from scratch, retro-poker workspace) |
| 1 | Feb 18 night | Context recovery, 7 pending features implemented |
| 2 | Feb 18–19 overnight | CI hardening, 1044 tests, coverage 48%→80% |
| 3 | Feb 19 ~14:00 | Documentation audit |
| 4 | Feb 19 ~14:00 | Pre-launch code analysis, 5 bugs fixed |
| 5 | Feb 19 ~16:00 | Pre-launch bug fixes (9 bugs) |
| 6 | Feb 19 ~17:00 | Pre-launch bug fixes round 2 (10 bugs) |
| 7 | Feb 19 ~19:00 | Pre-launch execution (Docker, preflight) |
| 8 | Feb 19 ~22:00 | Fix 3 pre-launch blockers, clean smoke test |
