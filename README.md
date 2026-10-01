# mycroftcompute

**Where your Claude Code money went, priced with the prompt cache counted, and which changes would
actually pay.**

[ccusage](https://github.com/ccusage/ccusage) tells you what Claude Code cost you. mycroftcompute
tells you what's worth changing, and what that change is worth at most.

```sh
uvx mycroftcompute            # read your own Claude Code history (~/.claude/projects)
uvx mycroftcompute --sample   # or see the report for an invented 24-developer team
```

Python 3.12+, no dependencies. Nothing leaves your machine. `pipx install mycroftcompute` works
too.

## What a finding looks like

From `--sample`:

```
FINDING 3 - 23.7% of your bill went on writing the prompt cache
------------------------------------------------------------------------------------

  What your metrics backend shows
  - 608,889,600 tokens were written to the prompt cache, costing $3,069.00 at the
    five-minute write rate - 23.7% of the $12,928.43 priced here. At the one-hour
    rate the same writes would cost $4,910.40.
  - 15,713,280,000 tokens were read back from cache, about 25.8 reads for every
    token written.
  - Read from cache instead of written, the same tokens would have cost $245.52.

  At most $2,823.48 over this period
    This is the gap between what these tokens cost and what they would cost under
    the cheaper option. It is a ceiling, not a saving - see below.

  What your metrics backend cannot tell you
  - How many of these writes were a warm prefix written again - after a new session,
    a compaction, a model switch, a change to the tools or system prompt, or a pause
    longer than the cache lives - and how many were new context that had to be
    written once. Only the first kind can be avoided ...
```

Every finding has the same three parts: what the data shows, the most a change could be worth,
and what the data cannot tell you.

## Why the cache changes the answer

Claude Code already serves most of its input from the prompt cache, typically above 90%. So
"turn caching on" is not the saving. What costs money is **writing** the cache: each write is
billed above list price (1.25x for five minutes, 2x for an hour), and a model switch throws the
warm cache away.

That is why a cheaper model is not always cheaper. On a typical coding-agent step (about 141,000
cached prompt tokens and 1,000 fresh ones):

| Decision | Input cost per step |
|---|---|
| Stay on Sonnet 4.6, cache warm | $0.045 |
| First request after moving to Haiku 4.5 | $0.177 - **3.9x worse** |
| Every request after that, Haiku's cache warm | $0.015 - 3x better |
| Alternating between the two | $0.426 - **9.4x worse** |

The rule that falls out: choose the model when a session starts, then stay. Every router and
dashboard compares list prices; mycroftcompute counts the cache before it suggests a model change.

## What it finds

| Finding | Fires when |
|---|---|
| **Cache writes** | Writing the cache is 20% or more of the bill. Ceiling: those tokens priced as reads instead. |
| **Model switch, net of cache** | A cheaper model would cost less once its own cache is warm. |
| **Housekeeping on an expensive model** | The agent's own background calls (`query_source = auxiliary`) run on a flagship model (team data: the label comes from telemetry). |
| **Uncached input** | Less than half of input comes from cache, which is unusual for Claude Code. |
| **Developer concentration** | A few people account for most of the spend (team data, 4+ developers). |
| **Continuous usage** | Spend doesn't drop at weekends, so something automated is driving it (team data). |

Run it again later with `--db history.db` and the report opens with **since last time**: what
appeared, what stopped firing, and how each ceiling moved, comparing equal windows.

## For a team: Grafana or Datadog

If your team already sends Claude Code's telemetry to Grafana Cloud (or any Prometheus) or
Datadog, mycroftcompute reads it with one read-only credential:

```sh
export MYCROFT_METRICS_TOKEN=glc_...            # Cloud Access Policy token, metrics:read
mycroftcompute --grafana https://prometheus-prod-NN-....grafana.net/api/prom --user 123456 --discover
mycroftcompute --grafana https://prometheus-prod-NN-....grafana.net/api/prom --user 123456 --days 14

export MYCROFT_METRICS_TOKEN=...                # Datadog API key
export MYCROFT_DATADOG_APP_KEY=...              # Application key: metrics_read, timeseries_query
mycroftcompute --datadog us1 --days 14
```

`--discover` checks the connection and lists what the telemetry can and cannot support before
reading any usage. Setup, including the two settings that silently lose every number, is in
[docs/team-backends.md](docs/team-backends.md). For a two-minute check you can run inside Grafana
or Datadog without installing anything, see [docs/cache-check.md](docs/cache-check.md).

Both backends were verified against live Claude Code telemetry in September 2026.

## How the numbers are made

- **List prices**, checked by hand against each vendor's own pricing page, for 19 models from
  Anthropic, OpenAI and Google, with cache read and write multipliers and per-model cache minimums.
  Prices were last checked on **24 September 2026**; when the table is more than 31 days old the
  report says so. Negotiated or batch rates are lower, so every figure is correspondingly high.
- **Ceilings, not savings.** Each figure is the most a change could be worth over the period.
  Ceilings overlap, so they are never added up.
- **Your own transcripts** record each cache write's lifetime, so local reports price five-minute
  and one-hour writes exactly. **Metrics backends** don't, so team reports price every write at
  the cheaper five-minute rate and say so.
- **Read-only.** It never reads prompts, responses or code from a backend, and never changes
  anything. Credentials come from the environment and are only used to query.

## Status

A personal project. It started as the engine of a product for platform teams, which I stopped in
October 2026; the analysis was worth keeping. The price table will age between updates, and the
report tells you when it has.

## Development

```sh
uv sync
uv run pytest
uv run ruff check src tests && uv run mypy
```

## License

MIT
