"""Read coding-agent usage from Datadog.

The second backend, and the one that doubles the addressable market: a company instrumented into
Datadog has already solved collection, which is the hard part and the thing we were never going
to win at. What they have not done is price it correctly or decide anything with it.

Datadog is the same job as Prometheus with four differences that each break a naive port, and
all four are handled here rather than discovered by a customer:

1. **Dots survive.** OTLP names keep their dots - `gen_ai.client.token.usage`, not
   `gen_ai_client_token_usage` - and so do tag keys. The vocabulary in `metrics_names.py` carries
   both spellings for exactly this reason.
2. **Histograms become distributions.** `gen_ai.client.token.usage` is a Histogram in the GenAI
   conventions, so Datadog stores `.sum`, `.count`, `.min`, `.max` and `.bucket` beside it. Only
   `.sum` is a token total; `.count` is a measurement count and reading it as tokens would put a
   number roughly a millionfold wrong on the page.
3. **Tags are lowercased.** Datadog normalises tag keys and values on ingestion, so Claude Code's
   `cacheRead` arrives as `cacheread`. `field_for_kind` folds case and separators away, which is
   why it exists rather than a plain dict lookup.
4. **Two timestamp units in one response.** The request takes `from`/`to` in epoch *seconds*;
   the `pointlist` comes back in *milliseconds*. Getting this backwards yields an empty report
   with no error, which is the worst kind of bug.

Auth needs two credentials - an API key and an Application key - and the Application key is the
one that carries scopes. It needs exactly two: `metrics_read` to list metrics and their tags, and
`timeseries_query` to read the numbers. Verified against a live org: with `metrics_read` alone,
discovery works and every usage query returns 403.

**Every credential mistake is a 403, and they need different sentences.** Verified live: a missing
scope, a wrong Application key, a wrong API key and valid keys sent to the wrong site all return
403 Forbidden. Only the missing scope has a distinguishing body. So on a bare 403 we ask
`/api/v1/validate`, which needs only the API key: if the key validates on this site the
Application key is the problem; if it validates on another site we can say which one.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from mycroftcompute.parsers.model import Issue, ParsedUsage, Severity
from mycroftcompute.sources.base import SourceError
from mycroftcompute.sources.merging import Family, Part, contested_message, merge
from mycroftcompute.sources.metrics_names import (
    CACHE_METRIC_STEMS,
    COST_STEMS,
    KIND_LABELS,
    MODEL_LABELS,
    REPO_LABELS,
    SEGMENT_LABELS,
    TOKEN_FAMILIES,
    USER_LABELS,
    VENDOR_LABELS,
    best_metric,
    field_for_kind,
)
from mycroftcompute.sources.records import (
    NO_CACHE_DIMENSION,
    NO_USER_LABEL,
    Accumulator,
    Key,
    dominant,
    multi_vendor_message,
    no_kind_label_message,
    vendors_in,
)

DAY_SECONDS = 86_400
MAX_DAYS = 400

#: Datadog's own retention for metrics at full resolution. Quoted back when a range is empty, so
#: the reader looks at their retention rather than at us.
RETENTION_NOTE = (
    "Datadog keeps metrics for 15 months, but a custom-metric plan or a drop rule can shorten "
    "that - check that these metrics are still being submitted."
)

#: Where the API lives for each Datadog site. Keys belong to one site: sent to another, they are
#: refused with a 403 indistinguishable from a bad key, which is why `_diagnose_forbidden` asks
#: every site before blaming the key.
SITES = {
    "us1": "https://api.datadoghq.com",
    "us3": "https://api.us3.datadoghq.com",
    "us5": "https://api.us5.datadoghq.com",
    "eu1": "https://api.datadoghq.eu",
    "ap1": "https://api.ap1.datadoghq.com",
    "gov": "https://api.ddog-gov.com",
}

HttpGet = Callable[[str, dict[str, str]], dict[str, Any]]

#: What the Application key must carry. Listing metrics and tags needs the first; reading the
#: numbers needs the second. Verified live - `metrics_read` alone passes discovery and then
#: fails every query.
REQUIRED_SCOPES = ("metrics_read", "timeseries_query")

#: The body Datadog returns for a missing scope, and only for a missing scope.
_SCOPE_DENIED = "Failed permission authorization checks"

#: What Datadog writes into a grouped-by tag that a series does not carry.
_NOT_APPLICABLE = "n/a"

#: Every label we might group by, in both spellings. Used when Datadog's tag metadata has not yet
#: caught up with newly arrived data: grouping a real query by all of them returns real values
#: for the tags that exist and "N/A" for the ones that do not.
_CANDIDATE_LABELS = tuple(
    dict.fromkeys(
        (*KIND_LABELS, *VENDOR_LABELS, *MODEL_LABELS, *USER_LABELS, *REPO_LABELS, *SEGMENT_LABELS)
    )
)

#: How far back the data-driven fallback looks. Short on purpose: it only runs when metadata is
#: lagging, which means the data is new, and a short window keeps the grouped query small on a
#: large org.
_FALLBACK_WINDOW_DAYS = 2


@dataclass(frozen=True, slots=True)
class DatadogEndpoint:
    """Which Datadog, and the two keys it wants.

    Both are required for a metric query. The API key identifies the org; the Application key
    identifies a user and carries the scopes, which is why a 403 here means "check the
    Application key's scopes" rather than "wrong password".
    """

    api_key: str
    app_key: str
    site: str = "us1"

    @property
    def base_url(self) -> str:
        if self.site.startswith(("http://", "https://")):
            return self.site.rstrip("/")
        resolved = SITES.get(self.site.lower())
        if resolved is None:
            raise SourceError(
                "unknown_site",
                f"'{self.site}' is not a Datadog site. Known sites: "
                + ", ".join(sorted(SITES))
                + ". The site is in the address you use to open Datadog: app.datadoghq.com is "
                "us1, us5.datadoghq.com is us5, app.datadoghq.eu is eu1.",
            )
        return resolved

    def headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "DD-API-KEY": self.api_key,
            "DD-APPLICATION-KEY": self.app_key,
        }


@dataclass(slots=True)
class DatadogDiscovered:
    """What this Datadog org actually has.

    `families` is a list because an org usually has more than one telemetry vocabulary live at
    once. See `sources/merging.py` for how they are combined, and why they are never summed.
    """

    families: list[Family] = field(default_factory=list)
    cost: Family | None = None

    cache_metrics: dict[str, Family] = field(default_factory=dict)
    all_names: tuple[str, ...] = ()

    @property
    def usable(self) -> bool:
        return bool(self.families) or self.cost is not None

    @property
    def token_metric(self) -> str | None:
        return self.families[0].metric if self.families else None

    @property
    def cost_metric(self) -> str | None:
        return self.cost.metric if self.cost else None

    @property
    def primary(self) -> Family | None:
        if not self.families:
            return None
        return max(self.families, key=lambda f: (f.knows_segments, f.knows_cache, f.knows_tokens))

    @property
    def kind_label(self) -> str | None:
        return self.primary.kind_label if self.primary else None

    @property
    def kinds(self) -> tuple[str, ...]:
        return tuple(sorted({k for f in self.families for k in f.kinds}))

    @property
    def model_label(self) -> str | None:
        return self.primary.model_label if self.primary else None

    @property
    def vendor_label(self) -> str | None:
        return self.primary.vendor_label if self.primary else None

    @property
    def user_label(self) -> str | None:
        return next((f.user_label for f in self.families if f.user_label), None)

    @property
    def repo_label(self) -> str | None:
        return next((f.repo_label for f in self.families if f.repo_label), None)

    @property
    def segment_labels(self) -> tuple[str, ...]:
        return next((f.segment_labels for f in self.families if f.segment_labels), ())

    @property
    def knows_tokens(self) -> bool:
        return any(f.knows_tokens for f in self.families)

    @property
    def knows_cache(self) -> bool:
        return any(f.knows_cache for f in self.families) or (
            "cached_input_tokens" in self.cache_metrics
        )


def _http_get(url: str, headers: dict[str, str]) -> dict[str, Any]:
    if not url.lower().startswith("https://"):
        raise SourceError("bad_url", f"Refusing to send Datadog keys over a non-HTTPS URL: {url}")
    request = urllib.request.Request(url, headers=headers)  # noqa: S310 - scheme checked above
    try:
        with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        if error.code == 403:
            try:
                body = error.read().decode("utf-8", errors="replace")
            except Exception:  # noqa: BLE001 - the body is a hint, never a requirement
                body = ""
            if _SCOPE_DENIED in body:
                raise SourceError("forbidden_scope", _scope_message()) from error
            # A bare 403 is any of: wrong API key, wrong Application key, wrong site. The
            # source diagnoses which, because it holds the keys and knows the other sites.
            raise SourceError(
                "forbidden",
                "Datadog refused these credentials. The API key, the Application key or the "
                "site is wrong.",
            ) from error
        if error.code == 401:
            raise SourceError(
                "unauthorized",
                "Datadog rejected the credentials. A metric query needs both an API key "
                "(DD-API-KEY) and an Application key (DD-APPLICATION-KEY) - one alone is not "
                "enough, and the two are issued from different pages.",
            ) from error
        if error.code == 404:
            raise SourceError("not_found", f"Datadog has no such resource: {url}") from error
        if error.code == 429:
            raise SourceError(
                "rate_limited",
                "Datadog rate-limited this query. The metrics API is limited per org per hour; "
                "waiting and pulling a shorter range is the fix.",
            ) from error
        raise SourceError("http_error", f"Datadog returned HTTP {error.code}.") from error
    except (urllib.error.URLError, TimeoutError) as error:
        raise SourceError("unreachable", f"Could not reach {url}: {error}") from error
    except json.JSONDecodeError as error:
        raise SourceError("bad_response", "Datadog did not return JSON.") from error

    if not isinstance(payload, dict):
        raise SourceError("bad_response", "Datadog returned JSON that is not an object.")
    if payload.get("status") == "error" or payload.get("error"):
        raise SourceError("query_failed", f"Datadog refused the query: {payload.get('error')}")
    return payload


def _scope_message() -> str:
    return (
        "The keys are right but the Application key is missing a scope. It needs both "
        + " and ".join(REQUIRED_SCOPES)
        + ": the first lists metrics, the second reads their values, and with only the first "
        "the connection check passes while every report fails. Scopes are set on the "
        "Application key in Organization Settings, not on the API key."
    )


def _pick(candidates: tuple[str, ...], available: set[str]) -> str | None:
    return next((c for c in candidates if c in available), None)


def _tag_pairs(series: dict[str, Any]) -> dict[str, str]:
    """Datadog reports a series' identity as `key:value` strings, twice over.

    `tag_set` is the structured form and `scope` is the same thing comma-joined. Prefer the
    former; fall back to the latter, because some query shapes only populate one.
    """
    raw = series.get("tag_set")
    if not isinstance(raw, list) or not raw:
        scope = series.get("scope")
        raw = str(scope).split(",") if isinstance(scope, str) and scope != "*" else []

    pairs: dict[str, str] = {}
    for item in raw:
        if not isinstance(item, str) or ":" not in item:
            continue
        # Split once: a tag value may legitimately contain a colon, a key may not.
        key, value = item.split(":", 1)
        value = value.strip()
        # Datadog fills a grouped-by tag the series does not carry with the literal "N/A".
        # Verified live: Claude Code omits `effort` on models that do not support it, and
        # reading the filler as a value would invent an effort level called "N/A".
        if value and value.lower() != _NOT_APPLICABLE:
            pairs[key.strip()] = value
    return pairs


class DatadogSource:
    """A `Source` backed by Datadog's metrics API."""

    def __init__(self, endpoint: DatadogEndpoint, http_get: HttpGet | None = None) -> None:
        self.endpoint = endpoint
        self._get = http_get or _http_get
        self._discovered: DatadogDiscovered | None = None

    def _fetch(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        url = f"{self.endpoint.base_url}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        try:
            return self._get(url, self.endpoint.headers())
        except SourceError as error:
            if error.code != "forbidden":
                raise
            raise self._diagnose_forbidden() from error

    def _validates(self, base_url: str) -> bool:
        """Whether the API key belongs to the site at this URL. Needs no Application key."""
        try:
            payload = self._get(
                f"{base_url}/api/v1/validate",
                {"Accept": "application/json", "DD-API-KEY": self.endpoint.api_key},
            )
        except SourceError:
            return False
        return payload.get("valid") is True

    def _diagnose_forbidden(self) -> SourceError:
        """Turn a bare 403 into the one sentence that fixes it.

        Verified live: a wrong API key, a wrong Application key and valid keys on the wrong site
        all return an identical 403. Validation separates them - and when the key belongs to a
        different site, it tells us which, so the reader gets the fix rather than a guess.
        """
        here = self.endpoint.base_url
        if self._validates(here):
            return SourceError(
                "bad_application_key",
                "The API key is valid for this site, so the Application key is the problem: it "
                "is wrong, revoked, or belongs to another organisation. Create one in "
                "Organization Settings > Application Keys with the scopes "
                + " and ".join(REQUIRED_SCOPES)
                + ".",
            )
        for site, base_url in SITES.items():
            if base_url == here:
                continue
            if self._validates(base_url):
                return SourceError(
                    "wrong_site",
                    f"These keys belong to the Datadog site '{site}', not the one we asked. "
                    f"Run again with --datadog {site}. Keys only work on their own site, and "
                    f"the wrong one refuses them exactly as it would a bad key.",
                )
        return SourceError(
            "bad_api_key",
            "The API key is not valid on any Datadog site, so it is wrong or has been revoked. "
            "It is the 32-character key from Organization Settings > API Keys - not the "
            "Application key, which is longer and comes from a different page.",
        )

    # ---------------------------------------------------------------- discovery ---

    def discover(self, *, since: date | None = None) -> DatadogDiscovered:
        """Ask Datadog which of these metrics it has, and which tags hang off them.

        `since` bounds the active-metrics list. Datadog needs a `from`, and asking about a metric
        that stopped reporting last year is not useful - we want what is live.
        """
        if self._discovered is not None:
            return self._discovered

        window_start = since or (datetime.now(tz=UTC).date() - timedelta(days=30))
        payload = self._fetch("/api/v1/metrics", {"from": _epoch(window_start)})
        names = [n for n in payload.get("metrics", []) if isinstance(n, str)]

        # The active-metrics list lags new data by minutes to hours, per metric. Verified live:
        # claude_code.token.usage was queryable while absent from the list, so a customer who
        # had just switched telemetry on was told "token counts missing" beside their own
        # token data. Each candidate we know by name is checked directly - the per-metric
        # metadata endpoint answers 404 or 200 immediately.
        for candidate in self._candidates():
            if candidate not in names and self._has_data(candidate, window_start):
                names.append(candidate)

        found = DatadogDiscovered(
            families=[
                self._describe(metric)
                for stems in TOKEN_FAMILIES
                if (metric := best_metric(stems, names)) is not None
            ],
            cache_metrics={
                field_name: self._describe(metric)
                for field_name, stems in CACHE_METRIC_STEMS.items()
                if (metric := best_metric(stems, names)) is not None
            },
            all_names=tuple(names),
        )
        if (cost := best_metric(COST_STEMS, names)) is not None:
            found.cost = self._describe(cost)

        self._discovered = found
        return found

    @staticmethod
    def _candidates() -> list[str]:
        """Every metric name worth checking directly, in Datadog's dotted spelling.

        A Histogram such as gen_ai.client.token.usage may arrive under its bare name or with a
        `.sum` suffix depending on how it was translated, so both are asked for.
        """
        stems = [s for family in TOKEN_FAMILIES for s in family]
        stems += list(COST_STEMS)
        stems += [s for group in CACHE_METRIC_STEMS.values() for s in group]
        dotted = [s for s in stems if "." in s]
        return list(dict.fromkeys([*dotted, *(f"{s}.sum" for s in dotted)]))

    def _has_data(self, metric: str, since: date) -> bool:
        """Whether this metric has any data points in the window - not merely a name.

        Two checks, because each is wrong alone. The metadata endpoint's 404 is a fast, reliable
        "no". Its 200 is not a "yes": verified live, standard names like gen_ai.client.cost
        return 200 with a null type in an org that has never sent one, and trusting that put a
        phantom metric on the connection check. So a 200 is confirmed against the data itself.
        """
        try:
            self._fetch(f"/api/v1/metrics/{urllib.parse.quote(metric, safe='')}")
        except SourceError as error:
            if error.code in {"not_found", "http_error"}:
                return False
            raise
        today = datetime.now(tz=UTC).date()
        return bool(self._query(f"sum:{metric}{{*}}", since, today))

    def _tags_from_data(self, metric: str) -> dict[str, set[str]]:
        """Tag keys and values read from the data itself, for when metadata is lagging.

        Grouping a real query by every label we know returns real values for the tags that
        exist and "N/A" for the rest, which `_tag_pairs` drops.
        """
        today = datetime.now(tz=UTC).date()
        start = today - timedelta(days=_FALLBACK_WINDOW_DAYS)
        query = f"sum:{metric}{{*}} by {{{','.join(_CANDIDATE_LABELS)}}}"
        pairs: dict[str, set[str]] = {}
        for series in self._query(query, start, today):
            for key, value in _tag_pairs(series).items():
                pairs.setdefault(key, set()).add(value)
        return pairs

    def _describe(self, metric: str) -> Family:
        """Which tags hang off one metric, and which token kinds it reports.

        Asked per metric rather than once per org, because the two vocabularies do not mix: the
        standard metric carries `gen_ai.request.model` while Claude Code's carries `model`, and an
        org with both has both spellings live at once. Datadog returns keys and values in the same
        call, so unlike Prometheus this costs nothing extra.
        """
        tags = self._tags_for(metric)
        if not any(key in tags for key in _CANDIDATE_LABELS):
            # Metadata has not caught up with the data yet. Ask the data.
            tags = self._tags_from_data(metric)
        keys = set(tags)
        family = Family(
            metric=metric,
            kind_label=_pick(KIND_LABELS, keys),
            vendor_label=_pick(VENDOR_LABELS, keys),
            model_label=_pick(MODEL_LABELS, keys),
            user_label=_pick(USER_LABELS, keys),
            repo_label=_pick(REPO_LABELS, keys),
            segment_labels=tuple(s for s in SEGMENT_LABELS if s in keys),
        )
        if family.kind_label:
            family.kinds = tuple(sorted(tags.get(family.kind_label, set())))
        return family

    def _tags_for(self, metric: str) -> dict[str, set[str]]:
        """Every tag key on a metric, with the values seen for each.

        Both indexed and ingested tags are read. A company that has not indexed its GenAI tags
        still has them on the datapoints, and refusing to look would report a perfectly
        instrumented backend as unusable.
        """
        payload = self._fetch(
            f"/api/v2/metrics/{urllib.parse.quote(metric, safe='')}/all-tags",
            {"window[seconds]": str(30 * DAY_SECONDS)},
        )
        attributes = payload.get("data", {}).get("attributes", {})
        pairs: dict[str, set[str]] = {}
        for group in ("tags", "ingested_tags"):
            for item in attributes.get(group, []) or []:
                if isinstance(item, str) and ":" in item:
                    key, value = item.split(":", 1)
                    if value:
                        pairs.setdefault(key.strip(), set()).add(value.strip())
        return pairs

    # -------------------------------------------------------------------- query ---

    def _query(self, query: str, start: date, end: date) -> list[dict[str, Any]]:
        payload = self._fetch(
            "/api/v1/query",
            # Seconds here; the pointlist comes back in milliseconds. Both are deliberate.
            {"from": _epoch(start), "to": _epoch(end + timedelta(days=1)), "query": query},
        )
        series = payload.get("series") or []
        return [s for s in series if isinstance(s, dict)]

    @staticmethod
    def _rollup(metric: str, grouping: list[str]) -> str:
        """A day-bucketed sum, grouped by whichever tags this org actually has.

        `.rollup(sum, 86400)` aligns buckets to the query window rather than to midnight UTC, so
        a range that starts mid-day would smear two days together. `fetch` always asks from
        midnight, which keeps the buckets and the record days the same thing.
        """
        by = f" by {{{','.join(grouping)}}}" if grouping else ""
        return f"sum:{metric}{{*}}{by}.rollup(sum, {DAY_SECONDS})"

    # ------------------------------------------------------------------ readers ---

    def _read_tokens(self, family: Family, start: date, end: date) -> Accumulator:
        acc = Accumulator()
        for series in self._query(self._rollup(family.metric, _grouping(family)), start, end):
            tags = _tag_pairs(series)
            field_name = field_for_kind(tags.get(family.kind_label or ""))
            if field_name is None:
                continue  # an unrecognised kind is dropped, never folded into another
            key = _key_of(tags, family)
            if system := tags.get(family.vendor_label or ""):
                acc.systems.add(system)
            for stamp, value in series.get("pointlist", []):
                if (day := _day(stamp)) is not None and (n := _count(value)) is not None:
                    acc.add_tokens(key.on(day), field_name, n)
        return acc

    def _read_into(
        self, acc: Accumulator, family: Family, field_name: str, start: date, end: date
    ) -> None:
        grouping = _grouping(family, with_kind=False)
        for series in self._query(self._rollup(family.metric, grouping), start, end):
            key = _key_of(_tag_pairs(series), family)
            for stamp, value in series.get("pointlist", []):
                if (day := _day(stamp)) is not None and (n := _count(value)) is not None:
                    acc.add_tokens(key.on(day), field_name, n)

    def _read_cost(self, acc: Accumulator, family: Family, start: date, end: date) -> None:
        grouping = _grouping(family, with_kind=False)
        for series in self._query(self._rollup(family.metric, grouping), start, end):
            key = _key_of(_tag_pairs(series), family)
            for stamp, value in series.get("pointlist", []):
                amount = _money(value)
                if (day := _day(stamp)) is not None and amount is not None:
                    acc.add_cost(key.on(day), amount)

    # -------------------------------------------------------------------- fetch ---

    def fetch(self, start: date, end: date) -> ParsedUsage:
        if end < start:
            raise SourceError("bad_range", "The end date is before the start date.")
        if (end - start).days > MAX_DAYS:
            raise SourceError(
                "range_too_large",
                f"Asked for {(end - start).days} days. {MAX_DAYS} is the limit in one pull.",
            )

        found = self.discover(since=start)
        if not found.usable:
            raise SourceError(
                "no_coding_agent_metrics",
                "This Datadog org has no coding-agent metrics in it. We looked for the "
                "OpenTelemetry GenAI metrics (gen_ai.client.token.usage), for Claude Code's own "
                "(CLAUDE_CODE_ENABLE_TELEMETRY=1, exported over OTLP), and for the agent "
                "plugins' names. Turn one of them on - org-wide through managed settings rather "
                "than developer by developer - and try again once a day of data has "
                "accumulated.",
            )

        issues: list[Issue] = []
        parts: list[Part] = []

        for family in found.families:
            if not family.knows_tokens:
                issues.append(
                    Issue(
                        code="no_token_kind_label",
                        severity=Severity.ERROR,
                        message=no_kind_label_message(family.metric),
                    )
                )
                continue
            parts.append(Part(family=family, accumulator=self._read_tokens(family, start, end)))

        merged = merge(parts)
        acc = merged.accumulator

        for field_name, family in found.cache_metrics.items():
            self._read_into(acc, family, field_name, start, end)
        if found.cost:
            self._read_cost(acc, found.cost, start, end)

        if not found.user_label:
            issues.append(Issue(code="no_user_label", message=NO_USER_LABEL))
        if not found.knows_cache:
            issues.append(Issue(code="no_cache_dimension", message=NO_CACHE_DIMENSION))
        if merged.contested:
            issues.append(Issue(code="contested_models", message=contested_message(merged)))
        if len(found.families) > 1:
            issues.append(
                Issue(
                    code="several_metric_families",
                    severity=Severity.INFO,
                    message=(
                        "This org carries more than one telemetry vocabulary: "
                        + ", ".join(f.metric for f in found.families)
                        + ". We read all of them, because the vendor-neutral metric covers every "
                        "provider while an agent's own metric is the only one carrying "
                        "query_source and effort - and those are what separate work somebody is "
                        "waiting on from the agent's own housekeeping."
                    ),
                )
            )

        knows_cache = found.knows_cache
        records = acc.records(knows_cache=knows_cache, knows_tokens=found.knows_tokens)

        if not records and found.families and not found.knows_tokens:
            raise SourceError(
                "no_token_kind_label", no_kind_label_message(found.families[0].metric)
            )
        if not records:
            raise SourceError(
                "no_data_in_range",
                f"Datadog has coding-agent metrics but none between {start} and {end}. "
                + RETENTION_NOTE,
            )

        vendors = vendors_in(records, acc.systems)
        if len(vendors) > 1:
            issues.append(
                Issue(
                    code="multi_vendor",
                    severity=Severity.INFO,
                    message=multi_vendor_message(vendors),
                )
            )

        present = {"day", "model"}
        if found.knows_tokens:
            present |= {"input_tokens", "output_tokens"}
        if knows_cache:
            present |= {"cached_input_tokens", "cache_write_tokens"}
        if found.cost_metric:
            present.add("cost")
        if found.user_label:
            present.add("user")
        if found.repo_label:
            present.add("repo")
        present |= set(found.segment_labels)

        return ParsedUsage(
            vendor=dominant(records, vendors),
            vendors=frozenset(vendors),
            records=records,
            fields_present=frozenset(present),
            rows_seen=len(records),
            rows_accepted=len(records),
            currency="USD" if found.cost_metric else None,
            issues=issues,
            source_kind="metrics",
        )


def _key_of(tags: dict[str, str], family: Family) -> Key:
    return Key(
        model=tags.get(family.model_label or "") or "(unspecified)",
        user=tags.get(family.user_label or "") or None,
        repo=tags.get(family.repo_label or "") or None,
        query_source=tags.get("query_source") or None,
        effort=tags.get("effort") or None,
    )


def _grouping(family: Family, *, with_kind: bool = True) -> list[str]:
    return [
        g
        for g in (
            family.kind_label if with_kind else None,
            family.model_label,
            family.vendor_label,
            family.user_label,
            family.repo_label,
            *family.segment_labels,
        )
        if g
    ]


def _epoch(day: date) -> str:
    """Epoch **seconds**, which is what the query endpoint takes."""
    return str(int(datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp()))


def _day(stamp: Any) -> date | None:
    """A pointlist timestamp, which is in **milliseconds**, and marks the START of its bucket.

    Verified live: a day's `.rollup(sum, 86400)` is stamped at 00:00 UTC of that same day. That is
    the opposite of Prometheus, whose daily sample is stamped at the end of the window
    it covers - so the Grafana connector shifts back a day and this one must not. Porting that fix
    here would date every figure a day early.

    Treating it as seconds silently yields dates in 1970, which the range filter then drops -
    an empty report with no error anywhere. The two units genuinely differ within one request.
    """
    try:
        return datetime.fromtimestamp(float(stamp) / 1000, tz=UTC).date()
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _count(value: Any) -> int | None:
    """Datadog writes `null` into the pointlist for an interval with no data."""
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if parsed != parsed or parsed < 0:
        return None
    return int(parsed)


def _money(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        parsed = Decimal(str(value))
    except (TypeError, ValueError, ArithmeticError):
        return None
    if not parsed.is_finite() or parsed < 0:
        return None
    return parsed
