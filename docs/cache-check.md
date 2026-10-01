# The cache-write check

A query you run in your own Grafana or Datadog in two minutes, with nothing to install. It
answers one question: **what share of our Claude Code bill went on writing the prompt cache?**
The full picture, including what that share could be worth, is in the `mycroftcompute` report.

Why a cost share and not a token share: writes are a few percent of tokens but a third or more of
the bill, because every write is billed above list price and every read far below it. On the
author's own stack: 3% of tokens, 37-48% of the cost.

**Verified live on 2026-09-29,** against the exact figure computed from the same data with
mycroftcompute's price table:

| Backend | Check says | Exact | |
|---|---|---|---|
| Grafana Cloud, 5-minute rate | 36.73% | 36.75% | two models, 14 days |
| Grafana Cloud, 1-hour rate | 48.15% | 48.17% | |
| Datadog | 72.13% | 72.13% | one model, one day |

---

## Grafana (Grafana Cloud, or any Prometheus)

1. Grafana, then **Explore**, then your Prometheus data source. Switch the query box to **Code**.
2. Set **Options, Type: Instant** (one number rather than a line).
3. Paste, and run:

```promql
100 *
sum(
  sum by (model) (last_over_time(claude_code_cost_usage_USD_total[14d]))
  *
  (
    1.25 * sum by (model) (last_over_time(claude_code_token_usage_tokens_total{type="cacheCreation"}[14d]))
    /
    (
        sum by (model) (last_over_time(claude_code_token_usage_tokens_total{type="input"}[14d]))
      + 1.25 * sum by (model) (last_over_time(claude_code_token_usage_tokens_total{type="cacheCreation"}[14d]))
      + 5 * sum by (model) (last_over_time(claude_code_token_usage_tokens_total{type="output"}[14d]))
      + (
            0.05 * sum by (model) (last_over_time(claude_code_token_usage_tokens_total{type="cacheRead", model=~".*claude-opus-5-5.*"}[14d]))
         or 0.025 * sum by (model) (last_over_time(claude_code_token_usage_tokens_total{type="cacheRead", model=~".*claude-fable-5-1.*"}[14d]))
         or 0.1 * sum by (model) (last_over_time(claude_code_token_usage_tokens_total{type="cacheRead"}[14d]))
        )
    )
  )
)
/
sum(
  sum by (model) (last_over_time(claude_code_cost_usage_USD_total[14d]))
  and on (model)
  sum by (model) (last_over_time(claude_code_token_usage_tokens_total{type="cacheCreation"}[14d]))
)
```

The number is a percentage, at the five-minute write rate. **For the one-hour rate, change the
two `1.25` to `2`.** Claude Code writes mostly at the one-hour rate, so the true share is between
the two numbers, and usually nearer the second.

`No data`: the metric names differ on this backend. Run the check query from [team-backends.md](team-backends.md), step 3
(`count by (job, __name__) ({__name__=~"claude_code_.*"})`) and swap in the names it shows.

## Datadog

1. **Dashboards, New Dashboard**, then add a **Query Value** widget.
2. Add four queries, each `sum:` with **as count**, and **Take the sum** over the time frame:

   | | Metric | Filter |
   |---|---|---|
   | a | `claude_code.token.usage` | `type:cachecreation` |
   | b | `claude_code.token.usage` | `type:input` |
   | c | `claude_code.token.usage` | `type:cacheread` |
   | d | `claude_code.token.usage` | `type:output` |

3. Hide the four queries and add the formula:

   ```
   100 * 1.25 * a / (b + 1.25 * a + 0.1 * c + 5 * d)
   ```

4. Set the time frame to **Past 2 Weeks**.

**One setting to match your main model**, because reads are priced differently: most usage on
Claude Opus 5.5, change `0.1` to `0.05`; on Claude Fable 5.1, to `0.025`. It moved the verified
result by 4.5 points. For the one-hour rate, change both `1.25` to `2`.

---

## How the arithmetic works

Anthropic's rates keep the same ratios on every current model: output costs 5x input, a cache
write 1.25x (five minutes) or 2x (one hour), and a cache read 0.1x (0.05x on Opus 5.5, 0.025x on
Fable 5.1). So each model's write share needs no prices at all:

```
write share = 1.25 W / (I + 1.25 W + r R + 5 O)
```

Across models, the Grafana check weights each model's share by the cost Claude Code itself
reported for it, so the result is the share of the dollars. The Datadog check uses one read rate
for all models, which is close enough when one model carries most of the spend.

## Limits, said plainly

- **Anthropic models only.** Those are the ratios above. Claude Code routed to other vendors'
  models is not covered.
- **Grafana counts every session active in the last 14 days,** including the part of a long
  session that started earlier. For a share, that bias is small; for dollar totals, use the
  report.
- **The five-minute figure is a floor,** exactly as in the report.
- **It says how big the cache-write share is, not how much of it is avoidable.** That needs the
  report, and even then it is a ceiling.

## What the number means

| Share | Reading |
|---|---|
| Under 20% | Healthy for Claude Code. Look elsewhere: model mix, housekeeping on an expensive model, developer concentration. |
| 20-40% | Where the report's cache-writes finding starts to fire. For comparison: the sample team is 24%, the author's own usage 37% at the five-minute rate. Worth knowing which rewrites cause it. |
| Over 40% | Unusually high. Usually sessions restarted or cleared often, MCP servers or tools changing mid-session, model switches inside a session, or long pauses. The report shows the most fewer rewrites could be worth. |
