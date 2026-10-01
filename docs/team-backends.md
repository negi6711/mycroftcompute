# Reading a team's Claude Code telemetry

For a team that sends, or could send, Claude Code's telemetry to **Grafana Cloud** (or any
Prometheus-compatible backend) or **Datadog**. mycroftcompute reads it with one read-only
credential and prints the same report it prints for your own transcripts, across the team.

| Read | Never read |
|---|---|
| Daily token counts by model and type: input, output, cache reads, cache writes | Prompts, responses or code |
| The labels Claude Code attaches: developer (`user.email`), request kind (`query_source`), `effort` | Files, logs or traces |
| The backend's own cost figure, to check the pricing against | Anything it could change |

Settings are read when a Claude Code session starts, so after changing them, start new sessions.

---

## Grafana Cloud

### 1. Get the telemetry write details

1. Sign in at **grafana.com** (the Cloud Portal, not your stack's own address).
2. On your stack, find the **OpenTelemetry** tile and click **Configure**.
3. It generates a token and shows an endpoint and a header. This is a **write** credential for
   developers' machines; mycroftcompute never needs it.

### 2. Point Claude Code at Grafana

Add this to the `env` block of Claude Code's settings: `~/.claude/settings.json` for one
developer, or Claude Code's managed settings for the whole team.

```json
{
  "env": {
    "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
    "OTEL_METRICS_EXPORTER": "otlp",
    "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
    "OTEL_EXPORTER_OTLP_ENDPOINT": "https://otlp-gateway-prod-<region>.grafana.net/otlp",
    "OTEL_EXPORTER_OTLP_HEADERS": "Authorization=Basic%20<from step 1>",
    "OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE": "cumulative"
  }
}
```

**Do not skip the `cumulative` line.** Claude Code sends delta counters by default, and Grafana
Cloud rejects them without any error you would notice: you would see a `target_info` series and
no usage at all.

**Leave `session.id` on** (do not set `OTEL_METRICS_INCLUDE_SESSION_ID=false`). Claude Code's
series carry no per-process label, so the session id is what keeps two sessions a developer has
open at once from writing into the same counter.

### 3. Check the data is arriving

Start a new Claude Code session, give it one prompt, wait a minute. In Grafana, open **Explore**,
choose your Prometheus data source, switch the query box to **Code**, and run:

```
count by (job, __name__) ({__name__=~"claude_code_.*"})
```

You should see `claude_code_token_usage_tokens_total` and `claude_code_cost_usage_USD_total`. The
`job` column says which app sent them: `claude-code` from the terminal, `claude-code-desktop` from
the desktop app. mycroftcompute reads them all. Grafana Cloud's free tier keeps 14 days.

### 4. Create a read-only credential and run it

1. On your stack, find the **Prometheus** tile and click **Details**. Note the **URL** (ending in
   `/api/prom`, not `/api/prom/push`) and the **Username / Instance ID**.
2. **Security**, then **Access Policies**, then **Create access policy**: realm **your one
   stack**, scopes **`metrics:read` only**. Then **Add token**.
3. Run:

```sh
export MYCROFT_METRICS_TOKEN=glc_...
mycroftcompute --grafana <URL> --user <instance ID> --discover
mycroftcompute --grafana <URL> --user <instance ID> --days 14 --db team.db
```

That token can read metrics from that one stack and nothing else.

---

## Datadog

### 1. Know your site

Your site is in the address you use to open Datadog: `app.datadoghq.com` is **us1**,
`app.datadoghq.eu` is **eu1**, `us3.datadoghq.com` is **us3**, and so on.

### 2. Point Claude Code at Datadog

```json
{
  "env": {
    "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
    "OTEL_METRICS_EXPORTER": "otlp",
    "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
    "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT": "https://otlp.datadoghq.com/v1/metrics",
    "OTEL_EXPORTER_OTLP_HEADERS": "dd-api-key=<an API key for telemetry>"
  }
}
```

That endpoint is for **us1**; other sites have their own.

**Do not add a `cumulative` temporality line** (Grafana needs one; Datadog is the opposite).
Datadog silently drops cumulative counters, with no error anywhere.

### 3. Check the data is arriving

Start a new session, give it one prompt, let it finish. In Datadog, open **Metrics**, then
**Explorer**, and graph `claude_code.token.usage` grouped by `type`. You should see `input`,
`output`, `cacheread` and `cachecreation`. Metric lists can take about five minutes to catch up.

### 4. Create read-only keys and run it

1. **API key:** Organization Settings, then **API Keys**. Create one named `mycroftcompute`,
   separate from the key telemetry uses.
2. **Application key:** Organization Settings, then **Application Keys**, then **New Key**. Open
   it, choose **Edit**, then **Scopes**, and select exactly **`metrics_read`** and
   **`timeseries_query`**.
3. Run:

```sh
export MYCROFT_METRICS_TOKEN=<API key>
export MYCROFT_DATADOG_APP_KEY=<Application key>
mycroftcompute --datadog us1 --discover
mycroftcompute --datadog us1 --days 14 --db team.db
```

Datadog shows developer identities as `name_domain.com`: its OTLP intake rewrites the `@`.

---

## Notes on the numbers

- **Counting.** Grafana stores running totals, and each Claude Code session starts new series.
  mycroftcompute counts each series' growth per day from zero, rather than PromQL's `increase()`,
  which misses every session's first export and so undercounts short sessions badly. A Grafana
  dashboard built on `increase()` will read lower than the report.
- **Cache-write lifetime.** Telemetry does not say whether a write used the five-minute or the
  one-hour cache, so writes are priced at the cheaper five-minute rate. If the backend's own cost
  figure is higher, the report shows what the one-hour rate would make it; Claude Code writes
  mostly at one hour, so the true figure is usually nearer that.
- **History.** `--db` keeps daily totals and findings in a local SQLite file so the next read can
  open with "since last time". Grafana Cloud's free tier only keeps 14 days, so keep the file.
