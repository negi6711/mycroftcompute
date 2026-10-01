"""Reading coding-agent usage from a Prometheus-compatible backend.

Responses here are built from the documented schema rather than recorded from an account:
`claude_code.token.usage` carries a `type` attribute with values `input`, `output`, `cacheRead`
and `cacheCreation`, plus `model` and the standard attributes including `user.id`. The
OTLP-to-Prometheus mapping turns dots into underscores and adds `_total` to counters.

The two behaviours worth most of these tests are discovery - because backends disagree about
suffixes - and degrading loudly when a label the analysis needs is absent.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from mycroftcompute.analysis.findings import analyse
from mycroftcompute.report.text import render
from mycroftcompute.sources.base import SourceError
from mycroftcompute.sources.grafana import (
    GrafanaSource,
    PrometheusEndpoint,
    _best_metric,
)
from mycroftcompute.sources.metrics_names import TOKEN_STEMS, field_for_kind
from mycroftcompute.vendors import Vendor

#: Prometheus stamps a daily sample at the END of the window it covers, so the
#: sample carrying 2026-09-07's usage is stamped midnight on the 8th. Getting this backwards
#: dates every day's spend one day late - which is what a live Grafana Cloud stack revealed.
DAY1 = 1_788_825_600  # midnight 2026-09-08Z: the sample for 2026-09-07
DAY2 = DAY1 + 86_400  # the sample for 2026-09-08
DAY = date(2026, 9, 7)

NAMES_WITH_SUFFIXES = [
    "claude_code_token_usage_tokens_total",
    "claude_code_cost_usage_USD_total",
    "claude_code_session_count_total",
    "up",
]


def _series(labels: dict[str, str], points: list[tuple[int, str]]) -> dict[str, Any]:
    return {"metric": labels, "values": [[t, v] for t, v in points]}


def _backend(
    names: list[str] | None = None,
    labels: list[str] | None = None,
    token_series: list[dict[str, Any]] | None = None,
    cost_series: list[dict[str, Any]] | None = None,
    cache_series: dict[str, list[dict[str, Any]]] | None = None,
) -> Any:
    """A fake Prometheus that answers the four calls the connector makes.

    The fourth is the values of the token-kind label. It is answered from the fixture series
    rather than from a constant, because that is how a real backend answers it: the standard
    GenAI metric only ever reports `input` and `output`, and whether a cache kind exists is
    exactly what the connector needs to find out rather than assume.
    """
    calls: list[str] = []

    def get(url: str, headers: dict[str, str]) -> dict[str, Any]:
        calls.append(url)
        if "__name__/values" in url:
            return {"status": "success", "data": names if names is not None else NAMES_WITH_SUFFIXES}
        if "/api/v1/labels" in url:
            return {
                "status": "success",
                "data": labels if labels is not None else ["model", "type", "user_id", "repository"],
            }
        if "/api/v1/label/" in url:
            label = url.split("/api/v1/label/")[1].split("/values")[0]
            seen = {s["metric"][label] for s in (token_series or []) if label in s["metric"]}
            return {"status": "success", "data": sorted(seen)}
        for metric, series in (cache_series or {}).items():
            if metric in url:
                return {"status": "success", "data": {"result": series}}
        if "cost" in url:
            return {"status": "success", "data": {"result": cost_series or []}}
        return {"status": "success", "data": {"result": token_series or []}}

    get.calls = calls  # type: ignore[attr-defined]
    return get


def _source(**kwargs: Any) -> GrafanaSource:
    return GrafanaSource(PrometheusEndpoint("https://example.invalid"), _backend(**kwargs))


class TestDiscovery:
    """Metric names are found, not assumed. A backend may or may not append the unit, and Mimir
    can be configured to strip suffixes entirely."""

    def test_it_finds_metrics_whatever_suffix_convention_is_used(self) -> None:
        for names in (
            ["claude_code_token_usage_tokens_total", "claude_code_cost_usage_USD_total"],
            ["claude_code_token_usage_total", "claude_code_cost_usage_total"],
            ["claude_code_token_usage", "claude_code_cost_usage"],
        ):
            found = _source(names=names).discover()
            assert found.usable, names
            assert found.token_metric and found.token_metric.startswith("claude_code_token_usage")
            assert found.cost_metric and found.cost_metric.startswith("claude_code_cost_usage")

    def test_a_histogram_is_read_by_its_sum_not_its_bucket(self) -> None:
        # gen_ai.client.token.usage is a Histogram in the GenAI conventions, so Prometheus holds
        # _bucket, _count and _sum beside each other. _bucket is the longest name and the wrong
        # one; _count is a measurement count, and instrumentation records one per token type, so
        # it is not even a request count.
        names = [
            "gen_ai_client_token_usage_tokens_bucket",
            "gen_ai_client_token_usage_tokens_count",
            "gen_ai_client_token_usage_tokens_sum",
        ]
        assert _best_metric(("gen_ai_client_token_usage",), names) == (
            "gen_ai_client_token_usage_tokens_sum"
        )

    def test_a_backend_with_only_buckets_reports_nothing_rather_than_guessing(self) -> None:
        assert _best_metric(("gen_ai_client_token_usage",),
                            ["gen_ai_client_token_usage_tokens_bucket"]) is None

    def test_the_most_specific_name_wins(self) -> None:
        # A backend exposing both the bare and suffixed name should be read through the suffixed
        # one, which is the counter.
        assert (
            _best_metric(
                ("claude_code_token_usage",),
                ["claude_code_token_usage", "claude_code_token_usage_tokens_total"],
            )
            == "claude_code_token_usage_tokens_total"
        )

    def test_a_dotted_backend_and_an_underscored_one_both_resolve(self) -> None:
        # Datadog keeps the dots; Prometheus flattens them. One vocabulary covers both.
        assert _best_metric(TOKEN_STEMS, ["gen_ai.client.token.usage.sum"]) == (
            "gen_ai.client.token.usage.sum"
        )
        assert _best_metric(TOKEN_STEMS, ["gen_ai_client_token_usage_tokens_total"]) == (
            "gen_ai_client_token_usage_tokens_total"
        )

    def test_it_finds_the_identity_and_repo_labels(self) -> None:
        found = _source().discover()
        assert found.user_label == "user_id"
        assert found.repo_label == "repository"

    def test_it_prefers_email_over_id_when_both_exist(self) -> None:
        found = _source(labels=["model", "type", "user_id", "user_email"]).discover()
        assert found.user_label == "user_email"

    def test_token_kinds_are_matched_past_case_and_separators(self) -> None:
        # Both vocabularies, plus Datadog's lowercasing, land in the same field.
        for spelling in ("cacheRead", "cache_read", "cacheread", "CACHE_READ"):
            assert field_for_kind(spelling) == "cached_input_tokens"
        assert field_for_kind("somethingNew") is None
        assert field_for_kind(None) is None

    def test_it_reads_grafanas_own_plugin_labels(self) -> None:
        found = _source(labels=["model", "type", "agento11y_tag_user", "agento11y_tag_repo"]).discover()
        assert found.user_label == "agento11y_tag_user"
        assert found.repo_label == "agento11y_tag_repo"

    def test_discovery_is_cached(self) -> None:
        source = _source()
        source.discover()
        before = len(source._get.calls)  # type: ignore[attr-defined]
        source.discover()
        assert len(source._get.calls) == before  # type: ignore[attr-defined]


TOKENS = [
    _series(
        {"model": "claude-sonnet-5", "type": "input", "user_id": "ana", "repository": "api"},
        [(DAY1, "1200"), (DAY2, "900")],
    ),
    _series(
        {"model": "claude-sonnet-5", "type": "cacheRead", "user_id": "ana", "repository": "api"},
        [(DAY1, "340000"), (DAY2, "410000")],
    ),
    _series(
        {"model": "claude-sonnet-5", "type": "cacheCreation", "user_id": "ana", "repository": "api"},
            [(DAY1, "8000"), (DAY2, "6000")],
    ),
    _series(
        {"model": "claude-sonnet-5", "type": "output", "user_id": "ana", "repository": "api"},
        [(DAY1, "4500"), (DAY2, "3900")],
    ),
]


class TestReadingUsage:
    def test_the_four_token_types_land_in_the_right_fields(self) -> None:
        parsed = _source(token_series=TOKENS).fetch(date(2026, 9, 7), date(2026, 9, 8))
        first = next(r for r in parsed.records if r.day == date(2026, 9, 7))
        assert first.input_tokens == 1_200
        assert first.cached_input_tokens == 340_000
        assert first.cache_write_tokens == 8_000
        assert first.output_tokens == 4_500

    def test_the_cache_split_survives_otel_which_is_the_whole_point(self) -> None:
        # If cacheRead did not come through, the headline finding could not run from a metrics
        # backend at all and Tier 1 would need a different source.
        parsed = _source(token_series=TOKENS).fetch(date(2026, 9, 7), date(2026, 9, 8))
        assert parsed.knows_cache_split
        assert all(r.cached_input_tokens for r in parsed.records)

    def test_identity_and_repository_are_carried(self) -> None:
        parsed = _source(token_series=TOKENS).fetch(date(2026, 9, 7), date(2026, 9, 8))
        assert parsed.records[0].user == "ana"
        assert parsed.records[0].repo == "api"

    def test_a_sample_is_dated_by_the_window_it_covers_not_its_stamp(self) -> None:
        # Verified against a live Grafana Cloud stack. A daily sample at midnight on the 8th
        # reports the 7th's usage; reading the stamp as the day shifted every figure forward
        # by one and dated today's spend in the future.
        parsed = _source(token_series=TOKENS).fetch(DAY, date(2026, 9, 8))
        assert min(r.day for r in parsed.records) == date(2026, 9, 7)
        assert max(r.day for r in parsed.records) == date(2026, 9, 8)

    def test_the_query_window_is_shifted_to_match(self) -> None:
        source = _source(token_series=TOKENS)
        source.fetch(DAY, DAY)
        ranged = next(u for u in source._get.calls if "query_range" in u)  # type: ignore[attr-defined]
        # Asking for the 7th means asking Prometheus for the sample stamped the 8th.
        assert "start=1788825600" in ranged

    def test_a_new_series_is_counted_from_zero(self) -> None:
        # Live: increase() dropped every session's first export, so a one-prompt session read
        # as zero tokens. Each query must take the day's last value minus the previous one, with
        # zero standing in when the series is new, and sum only after subtracting.
        cost = [_series({"model": "claude-sonnet-5"}, [(DAY1, "1.0")])]
        source = _source(token_series=TOKENS, cost_series=cost)
        source.fetch(DAY, DAY)
        queries = [
            parse_qs(urlsplit(u).query)["query"][0]
            for u in source._get.calls  # type: ignore[attr-defined]
            if "query_range" in u
        ]
        assert len(queries) == 2
        for query in queries:
            assert "increase(" not in query
            metric = query.split("last_over_time(")[1].split("[")[0]
            today = f"last_over_time({metric}[1d])"
            before = f"last_over_time({metric}[31d] offset 1d)"
            assert f"(({today} - ({before} or {today} * 0)) >= 0) or {today}" in query
            assert query.startswith("sum by (")

    def test_days_are_kept_apart(self) -> None:
        parsed = _source(token_series=TOKENS).fetch(date(2026, 9, 7), date(2026, 9, 8))
        assert sorted({r.day for r in parsed.records}) == [date(2026, 9, 7), date(2026, 9, 8)]

    def test_cost_is_read_when_the_backend_has_it(self) -> None:
        cost = [
            _series({"model": "claude-sonnet-5", "user_id": "ana", "repository": "api"},
                    [(DAY1, "1.2345")])
        ]
        parsed = _source(token_series=TOKENS, cost_series=cost).fetch(
            date(2026, 9, 7), date(2026, 9, 8)
        )
        assert parsed.knows_cost
        assert next(r for r in parsed.records if r.day == date(2026, 9, 7)).cost == Decimal("1.2345")

    def test_it_feeds_the_existing_analysis_unchanged(self) -> None:
        # The whole reason for the Source abstraction: findings never learn where data came from.
        parsed = _source(token_series=TOKENS).fetch(date(2026, 9, 7), date(2026, 9, 8))
        assert analyse(parsed) is not None

    def test_an_unknown_token_type_is_dropped_not_folded_into_another(self) -> None:
        odd = [*TOKENS, _series(
            {"model": "claude-sonnet-5", "type": "somethingNew", "user_id": "ana"},
            [(DAY1, "999999")],
        )]
        parsed = _source(token_series=odd).fetch(date(2026, 9, 7), date(2026, 9, 8))
        first = next(r for r in parsed.records if r.day == date(2026, 9, 7))
        assert first.input_tokens == 1_200  # unchanged by the unknown type

    def test_gaps_and_nan_samples_are_skipped(self) -> None:
        gappy = [_series({"model": "m", "type": "input", "user_id": "ana"},
                         [(DAY1, "NaN"), (DAY2, "500")])]
        parsed = _source(token_series=gappy).fetch(date(2026, 9, 7), date(2026, 9, 8))
        assert [r.day for r in parsed.records] == [date(2026, 9, 8)]


# The vendor-neutral vocabulary. Three vendors in one backend, which is the view none of their
# own consoles can produce - and the reason this connector exists at all.
OTEL_NAMES = [
    "gen_ai_client_token_usage_tokens_total",
    "claude_code_token_usage_tokens_total",
    "up",
]
OTEL_LABELS = [
    "gen_ai_request_model",
    "gen_ai_token_type",
    "gen_ai_system",
    "user_email",
    "repository",
]


def _otel(model: str, system: str, kind: str, value: str) -> dict[str, Any]:
    return _series(
        {
            "gen_ai_request_model": model,
            "gen_ai_system": system,
            "gen_ai_token_type": kind,
            "user_email": "ana@example.com",
            "repository": "api",
        },
        [(DAY1, value)],
    )


THREE_VENDORS = [
    _otel("claude-sonnet-5", "anthropic", "input", "100000"),
    _otel("claude-sonnet-5", "anthropic", "output", "10000"),
    _otel("gpt-5-6-terra", "openai", "input", "40000"),
    _otel("gpt-5-6-terra", "openai", "output", "4000"),
    _otel("gemini-3-1-pro", "gcp.gemini", "input", "20000"),
    _otel("gemini-3-1-pro", "gcp.gemini", "output", "2000"),
]


def _otel_source(**kwargs: Any) -> GrafanaSource:
    kwargs.setdefault("names", OTEL_NAMES)
    kwargs.setdefault("labels", OTEL_LABELS)
    return _source(**kwargs)


class TestTheStandardVocabulary:
    """`gen_ai.*` is what makes this multi-vendor. Claude Code's own metric is richer but speaks
    for one vendor, so the standard is tried first and the agent metric is the fallback."""

    def test_the_standard_metric_wins_over_the_agent_specific_one(self) -> None:
        found = _otel_source().discover()
        assert found.token_metric == "gen_ai_client_token_usage_tokens_total"

    def test_the_agent_metric_is_still_read_when_the_standard_is_absent(self) -> None:
        found = _source(names=NAMES_WITH_SUFFIXES).discover()
        assert found.token_metric == "claude_code_token_usage_tokens_total"

    def test_it_finds_the_standard_label_spellings(self) -> None:
        found = _otel_source().discover()
        assert found.kind_label == "gen_ai_token_type"
        assert found.model_label == "gen_ai_request_model"
        assert found.vendor_label == "gen_ai_system"
        assert found.user_label == "user_email"

    def test_three_vendors_come_back_from_one_query(self) -> None:
        parsed = _otel_source(token_series=THREE_VENDORS).fetch(DAY, DAY)
        assert parsed.all_vendors == frozenset({Vendor.ANTHROPIC, Vendor.OPENAI, Vendor.GOOGLE})
        assert parsed.is_multi_vendor

    def test_the_single_vendor_field_names_whoever_served_the_most(self) -> None:
        # Legacy callers read `vendor`. A page headed "Google" when Google served 15% of the
        # tokens reads as a bug, whatever `vendors` says underneath.
        parsed = _otel_source(token_series=THREE_VENDORS).fetch(DAY, DAY)
        assert parsed.vendor is Vendor.ANTHROPIC

    def test_the_model_decides_the_vendor_not_the_serving_platform(self) -> None:
        # Bedrock and Vertex serve other vendors' models. The price table is keyed by model, so
        # a Claude model billed through Bedrock is Anthropic money.
        parsed = _otel_source(
            token_series=[
                _otel("claude-sonnet-5", "aws.bedrock", "input", "1000"),
                _otel("claude-sonnet-5", "aws.bedrock", "output", "100"),
            ]
        ).fetch(DAY, DAY)
        assert parsed.all_vendors == frozenset({Vendor.ANTHROPIC})

    def test_the_system_label_is_the_fallback_for_a_model_we_cannot_price(self) -> None:
        parsed = _otel_source(
            token_series=[
                _otel("some-unreleased-model", "openai", "input", "1000"),
                _otel("some-unreleased-model", "openai", "output", "100"),
            ]
        ).fetch(DAY, DAY)
        assert parsed.all_vendors == frozenset({Vendor.OPENAI})

    def test_the_report_names_every_vendor_it_read(self) -> None:
        parsed = _otel_source(token_series=THREE_VENDORS).fetch(DAY, DAY)
        page = render(parsed, analyse(parsed), today=date(2026, 9, 22))
        assert "Anthropic, Google and OpenAI usage" in page

    def test_spend_is_split_across_vendors_in_one_bill(self) -> None:
        parsed = _otel_source(token_series=THREE_VENDORS).fetch(DAY, DAY)
        costed = analyse(parsed).costed
        assert costed is not None
        assert set(costed.by_model) == {"claude-sonnet-5", "gpt-5-6-terra", "gemini-3-1-pro"}


class TestTheStandardHasNoCacheType:
    """`gen_ai.token.type` is documented with two values, `input` and `output`. The cache split
    arrives as separate attributes where it arrives at all - and where it does not, the headline
    finding has to say so rather than report a zero hit rate."""

    def test_a_missing_cache_dimension_is_declared_not_reported_as_zero(self) -> None:
        parsed = _otel_source(token_series=THREE_VENDORS).fetch(DAY, DAY)
        assert not parsed.knows_cache_split
        assert all(r.cached_input_tokens is None for r in parsed.records)
        issue = next(i for i in parsed.issues if i.code == "no_cache_dimension")
        assert "a missing dimension and a cold cache are different things" in issue.message

    def test_the_cache_arrives_from_its_own_metric_when_the_backend_has_one(self) -> None:
        names = [*OTEL_NAMES, "gen_ai_usage_cache_read_input_tokens_total"]
        reads = [
            _series(
                {
                    "gen_ai_request_model": "claude-sonnet-5",
                    "gen_ai_system": "anthropic",
                    "user_email": "ana@example.com",
                    "repository": "api",
                },
                [(DAY1, "900000")],
            )
        ]
        parsed = _otel_source(
            names=names,
            token_series=THREE_VENDORS,
            cache_series={"gen_ai_usage_cache_read_input_tokens": reads},
        ).fetch(DAY, DAY)
        assert parsed.knows_cache_split
        anthropic = next(r for r in parsed.records if r.model == "claude-sonnet-5")
        assert anthropic.cached_input_tokens == 900_000

    def test_a_metric_with_no_kind_label_is_refused_rather_than_split_on_a_guess(self) -> None:
        # Input and output differ five to ten times in price. A single undifferentiated total
        # split on an assumed ratio is a confident wrong bill.
        with pytest.raises(SourceError, match="separating input from output"):
            _otel_source(
                labels=["gen_ai_request_model", "gen_ai_system"], token_series=THREE_VENDORS
            ).fetch(DAY, DAY)


AGENT_LABELS = ["model", "type", "user_id", "query_source", "effort"]


class TestTheSegmentsOnlyAgentMetricsCarry:
    """`query_source` and `effort` are the reason to keep reading an agent's own metric after the
    vendor-neutral one. Neither exists in the GenAI conventions, and they are what separate work
    somebody is waiting on from the agent talking to itself."""

    @staticmethod
    def _agent(kind: str, query_source: str, effort: str, value: str) -> dict[str, Any]:
        return _series(
            {
                "model": "claude-opus-5",
                "type": kind,
                "user_id": "ana",
                "query_source": query_source,
                "effort": effort,
            },
            [(DAY1, value)],
        )

    def test_discovery_finds_them(self) -> None:
        found = _source(labels=AGENT_LABELS).discover()
        assert found.segment_labels == ("query_source", "effort")

    def test_they_reach_the_records_and_split_the_bill(self) -> None:
        series = [
            self._agent("input", "main", "high", "1000000"),
            self._agent("output", "main", "high", "100000"),
            self._agent("input", "auxiliary", "low", "400000"),
            self._agent("output", "auxiliary", "low", "40000"),
        ]
        parsed = _source(labels=AGENT_LABELS, token_series=series).fetch(DAY, DAY)
        assert {r.query_source for r in parsed.records} == {"main", "auxiliary"}
        assert {r.effort for r in parsed.records} == {"high", "low"}

        costed = analyse(parsed).costed
        assert costed is not None
        assert costed.knows_segments
        assert set(costed.by_query_source) == {"main", "auxiliary"}

    def test_a_backend_without_them_leaves_them_unset_rather_than_guessed(self) -> None:
        parsed = _otel_source(token_series=THREE_VENDORS).fetch(DAY, DAY)
        assert all(r.query_source is None for r in parsed.records)
        assert not analyse(parsed).costed.knows_segments  # type: ignore[union-attr]


class TestDegradingLoudly:
    """A missing label must never become a quietly wrong number."""

    def test_no_identity_label_is_declared_rather_than_guessed(self) -> None:
        tokens = [_series({"model": "m", "type": "input"}, [(DAY1, "1000")])]
        parsed = _source(labels=["model", "type"], token_series=tokens).fetch(
            date(2026, 9, 7), date(2026, 9, 8)
        )
        assert all(r.user is None for r in parsed.records)
        assert "user" not in parsed.fields_present
        issue = next(i for i in parsed.issues if i.code == "no_user_label")
        assert "cardinality" in issue.message
        assert "per-developer figures cannot be produced" in issue.message

    def test_a_backend_with_no_coding_agent_metrics_says_how_to_fix_it(self) -> None:
        with pytest.raises(SourceError, match="CLAUDE_CODE_ENABLE_TELEMETRY"):
            _source(names=["up", "go_goroutines"]).fetch(date(2026, 9, 7), date(2026, 9, 8))

    def test_an_empty_range_blames_retention_not_the_user(self) -> None:
        with pytest.raises(SourceError, match="free tier keeps 14 days"):
            _source(token_series=[]).fetch(date(2026, 9, 7), date(2026, 9, 8))

    def test_rejected_credentials_name_all_three_causes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Verified against a real Grafana Cloud stack: a valid token sent to the wrong region's
        # endpoint returns exactly the same 401 as a bad token. Naming only the token would send
        # a reader to check the one thing that was already right.
        import urllib.error

        from mycroftcompute.sources.grafana import _http_get

        def unauthorized(request: Any, timeout: int = 0) -> Any:
            raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", {}, None)  # type: ignore[arg-type]

        monkeypatch.setattr("urllib.request.urlopen", unauthorized)
        with pytest.raises(SourceError) as caught:
            _http_get("https://example.invalid/api/v1/labels", {})

        message = " ".join(caught.value.message.split())
        assert "metrics:read scope" in message
        assert "numeric metrics instance ID" in message
        assert "another region's endpoint is rejected exactly like a bad one" in message


class TestGuards:
    def test_a_backwards_range_is_refused(self) -> None:
        with pytest.raises(SourceError, match="end date is before"):
            _source().fetch(date(2026, 9, 8), date(2026, 9, 7))

    def test_an_absurd_range_is_refused_before_the_backend_sees_it(self) -> None:
        with pytest.raises(SourceError, match="is the limit in one pull"):
            _source().fetch(date(2020, 1, 1), date(2026, 9, 7))

    def test_grafana_cloud_auth_is_basic_with_the_instance_id(self) -> None:
        endpoint = PrometheusEndpoint("https://prometheus-x.grafana.net", "123456", "glc_abc")
        assert endpoint.headers()["Authorization"].startswith("Basic ")

    def test_a_bare_token_falls_back_to_bearer(self) -> None:
        assert PrometheusEndpoint("https://x", token="abc").headers()["Authorization"] == "Bearer abc"

    def test_local_prometheus_needs_no_auth(self) -> None:
        assert "Authorization" not in PrometheusEndpoint("http://localhost:9090").headers()


def test_a_day_with_no_growth_is_not_a_day_with_usage() -> None:
    # Live: desktop sessions left open kept exporting unchanged totals, so two idle days came
    # back as zero-token rows and the report claimed five days with usage instead of three.
    idle = [
        _series({"model": "claude-sonnet-5", "type": "input", "user_id": "ana"},
                [(DAY1, "120"), (DAY2, "0")]),
    ]
    parsed = _source(token_series=idle).fetch(DAY, date(2026, 9, 8))
    assert [r.day for r in parsed.records] == [DAY]
