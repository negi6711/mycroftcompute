"""Read coding-agent usage from a Prometheus-compatible metrics backend.

Grafana Cloud stores OTLP metrics in Mimir, which speaks the Prometheus HTTP API, so one
connector serves Grafana Cloud, Grafana OSS, self-hosted Prometheus and anything else
Prometheus-compatible. Only the auth differs.

**Nothing is assumed; everything is discovered.** Which metric family a backend has, which suffix
convention it uses, which labels are present, and even which token *kinds* exist are all asked
for rather than hardcoded. Backends genuinely differ - the OTLP-to-Prometheus mapping may or may
not append a unit, Mimir can be configured to strip suffixes, and a company may be instrumented
through the OTel GenAI standard, through Claude Code's own metrics, or through Grafana's plugins.

**Missing labels degrade loudly.** Companies strip per-user labels because cardinality is what
metrics backends charge for. When identity is absent we say per-developer analysis is unavailable
and why - we never return a team-wide total that a reader would take for a per-developer one.

Everything past "here is a labelled series of daily totals" lives in `records.py`, shared with
the Datadog connector, so the two backends cannot drift into disagreeing about the same company.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from base64 import b64encode
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

#: Refuse to pull more than this in one go, so one mistyped date cannot ask a backend for a decade.
MAX_DAYS = 400

#: How far before each day to look for a series' previous value. Past any free-tier retention,
#: so a series that existed before the day is never mistaken for a new one within what is kept.
LOOKBACK = "31d"

HttpGet = Callable[[str, dict[str, str]], dict[str, Any]]

#: Re-exported under the old name because the tests reach for it directly.
_best_metric = best_metric


@dataclass(frozen=True, slots=True)
class PrometheusEndpoint:
    """Where to query, and how to authenticate.

    Grafana Cloud uses basic auth with the metrics instance ID as username and a Cloud Access
    Policy token (scope `metrics:read`) as password. Local and OSS Prometheus usually need neither.
    """

    base_url: str
    username: str | None = None
    token: str | None = None

    def headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.username and self.token:
            raw = f"{self.username}:{self.token}".encode()
            headers["Authorization"] = "Basic " + b64encode(raw).decode()
        elif self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers


@dataclass(slots=True)
class Discovered:
    """What this backend actually has, found rather than assumed.

    `families` is a list rather than one metric because a company usually has more than one, and
    reading only the first loses whichever half matters. See `sources/merging.py` for how they
    are combined - and for why they are never summed.
    """

    families: list[Family] = field(default_factory=list)
    cost: Family | None = None

    #: Backends that expose cache accounting as separate metrics rather than token kinds.
    cache_metrics: dict[str, Family] = field(default_factory=dict)

    all_names: tuple[str, ...] = ()

    @property
    def usable(self) -> bool:
        return bool(self.families) or self.cost is not None

    @property
    def token_metric(self) -> str | None:
        """The primary family's metric, for the code and tests that want one name."""
        return self.families[0].metric if self.families else None

    @property
    def cost_metric(self) -> str | None:
        return self.cost.metric if self.cost else None

    @property
    def primary(self) -> Family | None:
        """The family whose labels describe the read as a whole.

        The richest one wins, because that is the family a reader is being shown segments from.
        """
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
        """Whether the cache split is reachable at all - by token kind or by separate metric."""
        return any(f.knows_cache for f in self.families) or (
            "cached_input_tokens" in self.cache_metrics
        )


def _http_get(url: str, headers: dict[str, str]) -> dict[str, Any]:
    if not url.lower().startswith(("http://", "https://")):
        raise SourceError("bad_url", f"Refusing to fetch a non-HTTP URL: {url}")
    request = urllib.request.Request(url, headers=headers)  # noqa: S310 - scheme checked above
    try:
        with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        if error.code in (401, 403):
            raise SourceError(
                "unauthorized",
                "The backend rejected the credentials. On Grafana Cloud three things cause "
                "this, and the third is the one people miss. (1) The password must be a Cloud "
                "Access Policy token with the metrics:read scope - an ordinary Grafana API key "
                "or a service account token will not work for querying metrics. (2) The "
                "username must be the numeric metrics instance ID, not an email or the stack "
                "name. (3) The host must be your own stack's region: a perfectly valid token "
                "sent to another region's endpoint is rejected exactly like a bad one. Your "
                "region is in the endpoint URL shown on the Prometheus tile in the Grafana "
                "Cloud portal.",
            ) from error
        raise SourceError("http_error", f"The backend returned HTTP {error.code}.") from error
    except (urllib.error.URLError, TimeoutError) as error:
        raise SourceError("unreachable", f"Could not reach {url}: {error}") from error
    except json.JSONDecodeError as error:
        raise SourceError("bad_response", "The backend did not return JSON.") from error

    if not isinstance(payload, dict):
        raise SourceError("bad_response", "The backend returned JSON that is not an object.")
    if payload.get("status") != "success":
        raise SourceError(
            "query_failed", f"The backend refused the query: {payload.get('error', 'no reason')}"
        )
    return payload


def _pick(candidates: tuple[str, ...], available: set[str]) -> str | None:
    return next((c for c in candidates if c in available), None)


class GrafanaSource:
    """A `Source` backed by any Prometheus-compatible endpoint."""

    def __init__(self, endpoint: PrometheusEndpoint, http_get: HttpGet | None = None) -> None:
        self.endpoint = endpoint
        self._get = http_get or _http_get
        self._discovered: Discovered | None = None

    @property
    def _base(self) -> str:
        return self.endpoint.base_url.rstrip("/")

    def _fetch(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        url = f"{self._base}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        return self._get(url, self.endpoint.headers())

    # ---------------------------------------------------------------- discovery ---

    def discover(self) -> Discovered:
        """Ask the backend what it has rather than assuming a naming convention."""
        if self._discovered is not None:
            return self._discovered

        names = [
            n
            for n in self._fetch("/api/v1/label/__name__/values").get("data", [])
            if isinstance(n, str)
        ]

        found = Discovered(
            families=[
                self._describe(metric)
                for stems in TOKEN_FAMILIES
                if (metric := best_metric(stems, names)) is not None
            ],
            cache_metrics={
                field_name: self._describe(metric, kinds=False)
                for field_name, stems in CACHE_METRIC_STEMS.items()
                if (metric := best_metric(stems, names)) is not None
            },
            all_names=tuple(names),
        )
        if (cost := best_metric(COST_STEMS, names)) is not None:
            found.cost = self._describe(cost, kinds=False)

        self._discovered = found
        return found

    def _describe(self, metric: str, *, kinds: bool = True) -> Family:
        """Which labels hang off one metric, and which token kinds it actually reports.

        Labels are asked per metric rather than once per backend, because the two vocabularies do
        not mix: the standard metric carries `gen_ai.request.model` while Claude Code's carries
        `model`, and a company with both has both spellings live at once.
        """
        labels = {
            label
            for label in self._fetch("/api/v1/labels", {"match[]": metric}).get("data", [])
            if isinstance(label, str)
        }
        family = Family(
            metric=metric,
            kind_label=_pick(KIND_LABELS, labels),
            vendor_label=_pick(VENDOR_LABELS, labels),
            model_label=_pick(MODEL_LABELS, labels),
            user_label=_pick(USER_LABELS, labels),
            repo_label=_pick(REPO_LABELS, labels),
            segment_labels=tuple(s for s in SEGMENT_LABELS if s in labels),
        )
        if kinds and family.kind_label:
            # The values matter as much as the label. The GenAI standard documents only input and
            # output; whether a cache kind exists decides the headline finding.
            family.kinds = tuple(
                v
                for v in self._fetch(
                    f"/api/v1/label/{family.kind_label}/values", {"match[]": metric}
                ).get("data", [])
                if isinstance(v, str)
            )
        return family

    # -------------------------------------------------------------------- query ---

    def _range(self, query: str, start: date, end: date) -> list[dict[str, Any]]:
        # A daily sample evaluated at midnight T reports the window [T-1d, T] - that is, the
        # *previous* day's usage. So the samples we want sit one day after the days we
        # asked for, and `_window_day` shifts each one back when bucketing. Verified against a
        # live Grafana Cloud stack, where the old range dated today's usage as tomorrow.
        payload = self._fetch(
            "/api/v1/query_range",
            {
                "query": query,
                "start": _epoch(start + timedelta(days=1)),
                "end": _epoch(end + timedelta(days=1)),
                "step": str(DAY_SECONDS),
            },
        )
        result = payload.get("data", {}).get("result", [])
        return [r for r in result if isinstance(r, dict)]

    # ------------------------------------------------------------------ readers ---

    def _read_tokens(self, family: Family, start: date, end: date) -> Accumulator:
        """One family's token series, split by kind."""
        acc = Accumulator()
        grouping = _grouping(family)
        if not grouping:
            return acc
        query = f"sum by ({', '.join(grouping)}) ({_daily_growth(family.metric)})"
        for series in self._range(query, start, end):
            labels = series.get("metric", {})
            field_name = field_for_kind(_label(labels, family.kind_label))
            if field_name is None:
                continue  # an unrecognised kind is dropped, never folded into another
            key = _key_of(labels, family)
            if system := _label(labels, family.vendor_label):
                acc.systems.add(system)
            for stamp, value in series.get("values", []):
                if (day := _window_day(stamp)) is not None and (n := _count(value)) is not None:
                    acc.add_tokens(key.on(day), field_name, n)
        return acc

    def _read_into(
        self, acc: Accumulator, family: Family, field_name: str, start: date, end: date
    ) -> None:
        """A metric that is already one kind of token, so it carries no kind label."""
        grouping = _grouping(family, with_kind=False)
        if not grouping:
            return
        query = f"sum by ({', '.join(grouping)}) ({_daily_growth(family.metric)})"
        for series in self._range(query, start, end):
            key = _key_of(series.get("metric", {}), family)
            for stamp, value in series.get("values", []):
                if (day := _window_day(stamp)) is not None and (n := _count(value)) is not None:
                    acc.add_tokens(key.on(day), field_name, n)

    def _read_cost(self, acc: Accumulator, family: Family, start: date, end: date) -> None:
        grouping = _grouping(family, with_kind=False)
        if not grouping:
            return
        query = f"sum by ({', '.join(grouping)}) ({_daily_growth(family.metric)})"
        for series in self._range(query, start, end):
            key = _key_of(series.get("metric", {}), family)
            for stamp, value in series.get("values", []):
                amount = _money(value)
                if (day := _window_day(stamp)) is not None and amount is not None:
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

        found = self.discover()
        if not found.usable:
            raise SourceError(
                "no_coding_agent_metrics",
                "This backend has no coding-agent metrics in it. We looked for the "
                "OpenTelemetry GenAI metrics (gen_ai.client.token.usage), for Claude Code's own "
                "(CLAUDE_CODE_ENABLE_TELEMETRY=1, exported over OTLP), and for Grafana's agent "
                "plugins. Turn one of them on - org-wide through managed settings rather than "
                "developer by developer - and try again once a day of data has accumulated.",
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

        # Cache and cost live in their own metrics, with their own labels. They attach to the
        # read as a whole rather than to one family, because they are not split by token kind.
        for field_name, family in found.cache_metrics.items():
            self._read_into(acc, family, field_name, start, end)
        if found.cost:
            self._read_cost(acc, found.cost, start, end)

        if not found.user_label:
            issues.append(Issue(code="no_user_label", message=NO_USER_LABEL))
        if not found.knows_cache:
            issues.append(Issue(code="no_cache_dimension", message=NO_CACHE_DIMENSION))
        if merged.contested:
            issues.append(
                Issue(code="contested_models", message=contested_message(merged))
            )
        if len(found.families) > 1:
            issues.append(
                Issue(
                    code="several_metric_families",
                    severity=Severity.INFO,
                    message=(
                        "This backend carries more than one telemetry vocabulary: "
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
            # Blaming retention here would send the reader looking in the wrong place. The data
            # is present; the dimension we need to price it is not.
            raise SourceError(
                "no_token_kind_label", no_kind_label_message(found.families[0].metric)
            )
        if not records:
            raise SourceError(
                "no_data_in_range",
                f"The backend has coding-agent metrics but none between {start} and {end}. "
                f"Metrics backends expire data - Grafana Cloud's free tier keeps 14 days.",
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


def _daily_growth(metric: str) -> str:
    """How much each series grew during the day, counting a new series from zero.

    Not `increase(m[1d])`, which was wrong both ways on a live stack. It cannot see the first
    sample of a series, and Claude Code starts new series with every session (its `session_id`
    label), so a session's first export interval vanished: a one-prompt session read as zero
    tokens, and the desktop app's four days read 1.8% low. It also extrapolates to the edges of
    the window, which put one day 11% high.

    Instead: each series' last value in the day, minus its last value before the day, or minus
    zero when it had none. A drop means the counter restarted, and then the day's value is all
    new. Summing comes after the subtraction, so series never offset one another. Checked on the
    live stack against every series' final value: exact to the token.
    """
    today = f"last_over_time({metric}[1d])"
    before = f"last_over_time({metric}[{LOOKBACK}] offset 1d)"
    return f"((({today} - ({before} or {today} * 0)) >= 0) or {today})"


def _key_of(labels: dict[str, Any], family: Family) -> Key:
    return Key(
        model=_label(labels, family.model_label) or "(unspecified)",
        user=_label(labels, family.user_label),
        repo=_label(labels, family.repo_label),
        # A label absent from the series simply yields None, so no gating is needed here.
        query_source=_label(labels, "query_source"),
        effort=_label(labels, "effort"),
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


def _label(labels: dict[str, Any], name: str | None) -> str | None:
    if name is None:
        return None
    value = labels.get(name)
    return str(value) if value not in (None, "") else None


def _epoch(day: date) -> str:
    return str(int(datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp()))


def _window_day(stamp: Any) -> date | None:
    """The day a sample's one-day window actually covers.

    A sample stamped midnight on the 25th carries the 24th's usage. Reading the stamp as the
    day would shift every figure forward by one and, for the most recent sample, date today's
    spend in the future - which is what a live stack showed us.
    """
    try:
        moment = datetime.fromtimestamp(float(stamp), tz=UTC)
    except (TypeError, ValueError, OSError, OverflowError):
        return None
    return (moment - timedelta(days=1)).date()


def _count(value: Any) -> int | None:
    """Prometheus returns sample values as strings, and NaN for gaps."""
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if parsed != parsed or parsed < 0:  # NaN, or a negative that cannot be a counter increase
        return None
    return int(parsed)


def _money(value: Any) -> Decimal | None:
    """A cost sample. Deliberately not `_count`, which truncates every cent to zero."""
    try:
        parsed = Decimal(str(value))
    except (TypeError, ValueError, ArithmeticError):
        return None
    if not parsed.is_finite() or parsed < 0:
        return None
    return parsed
