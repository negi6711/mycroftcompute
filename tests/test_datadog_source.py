"""Reading coding-agent usage from Datadog.

Responses here are built from the documented API shape rather than recorded from an account.
Four of Datadog's conventions differ from Prometheus in ways that each produce a silently wrong
answer rather than an error, and most of these tests exist to pin exactly those:

* metric names keep their dots, and tag keys do too;
* an OTLP Histogram becomes a distribution, so `.sum` is the token total and `.count` is not;
* tag keys and values are lowercased on ingestion, so `cacheRead` arrives as `cacheread`;
* the request takes epoch seconds and the response returns milliseconds.
"""

from __future__ import annotations

import urllib.parse
from datetime import date
from decimal import Decimal
from typing import Any, ClassVar

import pytest

from mycroftcompute.analysis.findings import analyse
from mycroftcompute.sources.base import SourceError
from mycroftcompute.sources.datadog import DatadogEndpoint, DatadogSource, _tag_pairs
from mycroftcompute.vendors import Vendor

DAY = date(2026, 9, 7)
DAY1_MS = 1_788_739_200_000  # 2026-09-07T00:00:00Z, in milliseconds
DAY2_MS = DAY1_MS + 86_400_000

#: What a Datadog org instrumented through the GenAI conventions actually holds. The histogram
#: family is present in full, because that is the trap: only `.sum` is a token count.
OTEL_METRICS = [
    "gen_ai.client.token.usage.sum",
    "gen_ai.client.token.usage.count",
    "gen_ai.client.token.usage.min",
    "gen_ai.client.token.usage.max",
    "gen_ai.client.token.usage.bucket",
    "system.cpu.idle",
]

OTEL_TAGS = [
    "gen_ai.token.type:input",
    "gen_ai.token.type:output",
    "gen_ai.request.model:claude-sonnet-5",
    "gen_ai.request.model:gpt-5-6-terra",
    "gen_ai.system:anthropic",
    "gen_ai.system:openai",
    "user.email:ana@example.com",
    "repository:api",
]


def _series(tags: list[str], points: list[tuple[int, float]]) -> dict[str, Any]:
    return {
        "metric": "gen_ai.client.token.usage.sum",
        "scope": ",".join(tags),
        "tag_set": tags,
        "pointlist": [[t, v] for t, v in points],
    }


def _backend(
    metrics: list[str] | None = None,
    tags: list[str] | None = None,
    ingested_tags: list[str] | None = None,
    series: list[dict[str, Any]] | None = None,
    cost_series: list[dict[str, Any]] | None = None,
) -> Any:
    """A fake Datadog answering the three calls the connector makes."""
    calls: list[str] = []

    known = metrics if metrics is not None else OTEL_METRICS

    def get(url: str, headers: dict[str, str]) -> dict[str, Any]:
        calls.append(url)
        if "/api/v1/metrics/" in url:
            # Per-metric metadata: 200 for a metric the org has, 404 otherwise - verified live.
            name = urllib.parse.unquote(url.split("/api/v1/metrics/")[1].split("?")[0])
            if name in known:
                return {"type": "count"}
            raise SourceError("not_found", f"no such metric: {name}")
        if "/api/v1/metrics" in url:
            return {"metrics": known, "from": "0"}
        if "/all-tags" in url:
            return {
                "data": {
                    "type": "metrics",
                    "attributes": {
                        "tags": tags if tags is not None else OTEL_TAGS,
                        "ingested_tags": ingested_tags or [],
                    },
                }
            }
        if "cost" in url:
            return {"status": "ok", "series": cost_series or []}
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get("query", [""])[0]
        if query.startswith("sum:") and "}" in query and " by " not in query:
            # An existence probe: a known metric has data behind it.
            name = query[4:].split("{")[0]
            return {"status": "ok", "series": [{"pointlist": [[0, 1]]}] if name in known else []}
        return {"status": "ok", "series": series or []}

    get.calls = calls  # type: ignore[attr-defined]
    return get


def _source(**kwargs: Any) -> DatadogSource:
    return DatadogSource(
        DatadogEndpoint(api_key="dd-api", app_key="dd-app", site="us1"), _backend(**kwargs)
    )


TOKENS = [
    _series(
        [
            "gen_ai.token.type:input",
            "gen_ai.request.model:claude-sonnet-5",
            "gen_ai.system:anthropic",
            "user.email:ana@example.com",
            "repository:api",
        ],
        [(DAY1_MS, 120_000), (DAY2_MS, 90_000)],
    ),
    _series(
        [
            "gen_ai.token.type:output",
            "gen_ai.request.model:claude-sonnet-5",
            "gen_ai.system:anthropic",
            "user.email:ana@example.com",
            "repository:api",
        ],
        [(DAY1_MS, 12_000), (DAY2_MS, 9_000)],
    ),
    _series(
        [
            "gen_ai.token.type:input",
            "gen_ai.request.model:gpt-5-6-terra",
            "gen_ai.system:openai",
            "user.email:ben@example.com",
            "repository:web",
        ],
        [(DAY1_MS, 40_000)],
    ),
    _series(
        [
            "gen_ai.token.type:output",
            "gen_ai.request.model:gpt-5-6-terra",
            "gen_ai.system:openai",
            "user.email:ben@example.com",
            "repository:web",
        ],
        [(DAY1_MS, 4_000)],
    ),
]


class TestTheDistributionTrap:
    """`gen_ai.client.token.usage` is a Histogram, so Datadog stores `.sum`, `.count`, `.min`,
    `.max` and `.bucket`. Only one of them is a token count."""

    def test_it_reads_the_sum_and_nothing_else(self) -> None:
        found = _source().discover()
        assert found.token_metric == "gen_ai.client.token.usage.sum"

    def test_it_never_mistakes_the_bucket_or_count_for_a_total(self) -> None:
        # `.count` is the number of measurements and `.bucket` is a histogram bucket. Reading
        # either as tokens puts a number orders of magnitude wrong on the page.
        found = _source(
            metrics=["gen_ai.client.token.usage.count", "gen_ai.client.token.usage.bucket"]
        ).discover()
        assert found.token_metric is None
        assert not found.usable

    def test_a_plain_counter_keeps_its_bare_name(self) -> None:
        # Claude Code's own metric is a Sum, not a Histogram, so Datadog adds no suffix at all.
        found = _source(metrics=["claude_code.token.usage", "claude_code.cost.usage"]).discover()
        assert found.token_metric == "claude_code.token.usage"
        assert found.cost_metric == "claude_code.cost.usage"


class TestDottedNames:
    def test_tag_keys_keep_their_dots(self) -> None:
        found = _source().discover()
        assert found.kind_label == "gen_ai.token.type"
        assert found.model_label == "gen_ai.request.model"
        assert found.vendor_label == "gen_ai.system"
        assert found.user_label == "user.email"

    def test_tag_values_are_read_off_the_same_call(self) -> None:
        # Unlike Prometheus, Datadog returns keys and values together, so knowing which token
        # kinds exist costs no extra request.
        found = _source().discover()
        assert set(found.kinds) == {"input", "output"}

    def test_ingested_tags_count_too(self) -> None:
        # A company that has not indexed its GenAI tags still has them on the datapoints.
        found = _source(tags=[], ingested_tags=OTEL_TAGS).discover()
        assert found.model_label == "gen_ai.request.model"


class TestLowercasedTags:
    """Datadog normalises tag values on ingestion, so Claude Code's camelCase arrives folded."""

    def test_cacheread_lowercased_still_lands_in_the_cache_field(self) -> None:
        tags = ["type:input", "type:output", "type:cacheread", "type:cachecreation",
                "model:claude-sonnet-5", "user_id:ana"]
        series = [
            _series(["type:cacheread", "model:claude-sonnet-5", "user_id:ana"],
                    [(DAY1_MS, 900_000)]),
            _series(["type:input", "model:claude-sonnet-5", "user_id:ana"], [(DAY1_MS, 1_000)]),
        ]
        parsed = _source(
            metrics=["claude_code.token.usage"], tags=tags, series=series
        ).fetch(DAY, DAY)
        assert parsed.knows_cache_split
        assert parsed.records[0].cached_input_tokens == 900_000
        assert parsed.records[0].input_tokens == 1_000


class TestTimestampUnits:
    def test_the_response_is_read_as_milliseconds(self) -> None:
        # Reading these as seconds puts every record in 1970, which the range then drops - an
        # empty report with no error anywhere.
        parsed = _source(series=TOKENS).fetch(DAY, date(2026, 9, 8))
        assert sorted({r.day for r in parsed.records}) == [date(2026, 9, 7), date(2026, 9, 8)]

    def test_the_request_is_sent_as_seconds(self) -> None:
        source = _source(series=TOKENS)
        source.fetch(DAY, DAY)
        query = next(u for u in source._get.calls if "/api/v1/query" in u)  # type: ignore[attr-defined]
        assert "from=1788739200" in query
        assert "rollup%28sum%2C+86400%29" in query


class TestReadingUsage:
    def test_two_vendors_come_back_from_one_query(self) -> None:
        parsed = _source(series=TOKENS).fetch(DAY, date(2026, 9, 8))
        assert parsed.all_vendors == frozenset({Vendor.ANTHROPIC, Vendor.OPENAI})
        assert parsed.is_multi_vendor

    def test_identity_and_repository_are_carried(self) -> None:
        parsed = _source(series=TOKENS).fetch(DAY, date(2026, 9, 8))
        assert {r.user for r in parsed.records} == {"ana@example.com", "ben@example.com"}
        assert {r.repo for r in parsed.records} == {"api", "web"}

    def test_a_null_point_is_skipped_not_counted_as_zero(self) -> None:
        gappy = [
            _series(
                ["gen_ai.token.type:input", "gen_ai.request.model:claude-sonnet-5"],
                [(DAY1_MS, 500)],
            )
        ]
        gappy[0]["pointlist"].append([DAY2_MS, None])
        parsed = _source(series=gappy).fetch(DAY, date(2026, 9, 8))
        assert [r.day for r in parsed.records] == [date(2026, 9, 7)]

    def test_cost_keeps_its_cents(self) -> None:
        cost = [
            {
                "metric": "claude_code.cost.usage",
                "tag_set": ["model:claude-sonnet-5", "user_id:ana"],
                "pointlist": [[DAY1_MS, 1.2345]],
            }
        ]
        parsed = _source(
            metrics=["claude_code.token.usage", "claude_code.cost.usage"],
            tags=["type:input", "model:claude-sonnet-5", "user_id:ana"],
            series=[_series(["type:input", "model:claude-sonnet-5", "user_id:ana"],
                            [(DAY1_MS, 1_000)])],
            cost_series=cost,
        ).fetch(DAY, DAY)
        assert parsed.records[0].cost == Decimal("1.2345")

    def test_it_feeds_the_existing_analysis_unchanged(self) -> None:
        parsed = _source(series=TOKENS).fetch(DAY, date(2026, 9, 8))
        assert analyse(parsed).costed is not None

    def test_scope_is_used_when_tag_set_is_empty(self) -> None:
        assert _tag_pairs({"scope": "model:x,user_id:ana"}) == {"model": "x", "user_id": "ana"}

    def test_a_wildcard_scope_names_nothing(self) -> None:
        assert _tag_pairs({"scope": "*"}) == {}


class TestDegradingLoudly:
    def test_the_standard_has_no_cache_type_and_says_so(self) -> None:
        parsed = _source(series=TOKENS).fetch(DAY, date(2026, 9, 8))
        assert not parsed.knows_cache_split
        assert all(r.cached_input_tokens is None for r in parsed.records)
        assert any(i.code == "no_cache_dimension" for i in parsed.issues)

    def test_no_identity_tag_is_declared_rather_than_guessed(self) -> None:
        parsed = _source(
            tags=["gen_ai.token.type:input", "gen_ai.request.model:claude-sonnet-5"],
            series=[_series(["gen_ai.token.type:input",
                             "gen_ai.request.model:claude-sonnet-5"], [(DAY1_MS, 1_000)])],
        ).fetch(DAY, DAY)
        assert all(r.user is None for r in parsed.records)
        issue = next(i for i in parsed.issues if i.code == "no_user_label")
        assert "cardinality" in issue.message

    def test_an_org_with_no_agent_metrics_says_how_to_turn_them_on(self) -> None:
        # It used to blame the site, claiming a wrong site "succeeds and returns nothing".
        # Verified live: a wrong site is a 403, handled separately. An empty result is just an
        # empty org.
        with pytest.raises(SourceError, match="managed settings"):
            _source(metrics=["system.cpu.idle"]).fetch(DAY, DAY)

    def test_an_empty_range_points_at_submission_not_at_us(self) -> None:
        with pytest.raises(SourceError, match="still being submitted"):
            _source(series=[]).fetch(DAY, DAY)


class TestDiscoveryDoesNotWaitForDatadogsMetadata:
    """Verified live: after data first arrives, Datadog's active-metrics list lags by minutes and
    its per-metric tag list stays empty for longer. A connection check that trusted them told a
    customer "token counts missing" beside their own token data."""

    @staticmethod
    def _source(
        *, listed: list[str], with_data: dict[str, list[str]], tags_indexed: bool
    ) -> DatadogSource:
        """`with_data` maps a metric that has data to the tag strings its series carry."""

        def get(url: str, headers: dict[str, str]) -> dict[str, Any]:
            if "/api/v1/metrics/" in url:
                name = urllib.parse.unquote(url.split("/api/v1/metrics/")[1].split("?")[0])
                # Standard names answer 200 even with no data behind them - verified live.
                if name in with_data or name.startswith("gen_ai."):
                    return {"type": "count" if name in with_data else None}
                raise SourceError("not_found", name)
            if "/api/v1/metrics" in url:
                return {"metrics": listed}
            if "/all-tags" in url:
                name = urllib.parse.unquote(url.split("/api/v2/metrics/")[1].split("/all-tags")[0])
                tags = with_data.get(name, []) if tags_indexed else []
                return {"data": {"attributes": {"tags": tags}}}
            query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get("query", [""])[0]
            name = query[4:].split("{")[0]
            if name not in with_data:
                return {"status": "ok", "series": []}
            if " by " in query:
                # Grouped by every candidate label: absent tags come back as N/A.
                present = with_data[name]
                return {"status": "ok", "series": [
                    {"tag_set": [*present, "gen_ai.token.type:N/A", "repository:N/A"],
                     "pointlist": [[DAY1_MS, 1.0]]}
                ]}
            return {"status": "ok", "series": [{"pointlist": [[DAY1_MS, 1.0]]}]}

        return DatadogSource(DatadogEndpoint("k" * 32, "a" * 40, "us1"), get)

    TAGS: ClassVar[list[str]] = [
        "type:input",
        "type:cacheread",
        "model:claude-opus-5-5",
        "user.email:ana_example.com",
        "query_source:main",
        "effort:high",
    ]

    def test_a_metric_with_data_is_found_before_the_active_list_catches_up(self) -> None:
        found = self._source(
            listed=[], with_data={"claude_code.token.usage": self.TAGS}, tags_indexed=True
        ).discover()
        assert found.token_metric == "claude_code.token.usage"

    def test_a_standard_name_with_no_data_behind_it_is_not_a_metric(self) -> None:
        # Metadata answered 200 for gen_ai.client.cost in an org that had never sent one, and
        # the connection check reported it as the org's cost metric.
        found = self._source(
            listed=[], with_data={"claude_code.token.usage": self.TAGS}, tags_indexed=True
        ).discover()
        assert found.cost_metric is None
        assert [f.metric for f in found.families] == ["claude_code.token.usage"]

    def test_tags_are_read_from_the_data_when_metadata_has_none(self) -> None:
        found = self._source(
            listed=["claude_code.token.usage"],
            with_data={"claude_code.token.usage": self.TAGS},
            tags_indexed=False,
        ).discover()
        assert found.kind_label == "type"
        assert found.model_label == "model"
        assert found.user_label == "user.email"
        assert found.segment_labels == ("query_source", "effort")
        assert found.knows_cache

    def test_na_filler_never_becomes_a_label(self) -> None:
        found = self._source(
            listed=["claude_code.token.usage"],
            with_data={"claude_code.token.usage": self.TAGS},
            tags_indexed=False,
        ).discover()
        assert found.repo_label is None


class TestTheNAFiller:
    def test_na_is_dropped_rather_than_read_as_a_value(self) -> None:
        # Claude Code omits `effort` on models that do not support it; grouping by it anyway
        # returns effort:N/A, which would otherwise become an effort level called "N/A".
        assert _tag_pairs({"tag_set": ["effort:N/A", "model:m", "repo:n/a"]}) == {"model": "m"}

    def test_a_series_missing_a_segment_records_none_not_na(self) -> None:
        series = [
            {"tag_set": ["type:input", "model:claude-opus-5-5", "user.email:ana",
                         "query_source:main", "effort:N/A"],
             "pointlist": [[DAY1_MS, 1000.0]]},
        ]
        parsed = _source(
            metrics=["claude_code.token.usage"],
            tags=["type:input", "model:claude-opus-5-5", "user.email:ana",
                  "query_source:main", "effort:high"],
            series=series,
        ).fetch(DAY, DAY)
        assert parsed.records[0].effort is None
        assert parsed.records[0].query_source == "main"


class TestEveryForbiddenGetsTheRightSentence:
    """Verified live against a real Datadog org: a missing scope, a wrong Application key, a wrong
    API key and valid keys on the wrong site all return 403. Only the missing scope has a
    distinguishing body; `/api/v1/validate` separates the rest."""

    @staticmethod
    def _source(*, valid_on: str | None, site: str = "us1") -> DatadogSource:
        from mycroftcompute.sources.datadog import SITES

        def get(url: str, headers: dict[str, str]) -> dict[str, Any]:
            if url.endswith("/api/v1/validate"):
                if valid_on is not None and url.startswith(SITES[valid_on]):
                    return {"valid": True}
                raise SourceError("forbidden", "bare 403")
            raise SourceError("forbidden", "bare 403")

        return DatadogSource(DatadogEndpoint("k" * 32, "a" * 40, site), get)

    def test_keys_from_another_site_are_told_which_site(self) -> None:
        with pytest.raises(SourceError) as caught:
            self._source(valid_on="eu1", site="us1").discover()
        assert caught.value.code == "wrong_site"
        assert "--datadog eu1" in caught.value.message

    def test_a_valid_api_key_here_means_the_application_key_is_wrong(self) -> None:
        with pytest.raises(SourceError) as caught:
            self._source(valid_on="us1", site="us1").discover()
        assert caught.value.code == "bad_application_key"
        assert "timeseries_query" in caught.value.message

    def test_an_api_key_valid_nowhere_is_the_api_key(self) -> None:
        with pytest.raises(SourceError) as caught:
            self._source(valid_on=None).discover()
        assert caught.value.code == "bad_api_key"
        assert "32-character key" in caught.value.message

    def test_the_missing_scope_body_is_recognised_without_extra_calls(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import io
        import urllib.error

        from mycroftcompute.sources.datadog import _http_get

        def denied(request: Any, timeout: int = 0) -> Any:
            body = b'{"errors":["Forbidden","Failed permission authorization checks"]}'
            raise urllib.error.HTTPError(request.full_url, 403, "Forbidden", {}, io.BytesIO(body))  # type: ignore[arg-type]

        monkeypatch.setattr("urllib.request.urlopen", denied)
        with pytest.raises(SourceError) as caught:
            _http_get("https://api.datadoghq.com/api/v1/query", {})
        assert caught.value.code == "forbidden_scope"
        assert "metrics_read and timeseries_query" in caught.value.message

    def test_metrics_read_alone_is_not_enough(self) -> None:
        # The connector, the CLI and the runbook all said metrics_read. With only that, discovery
        # passed and every usage query failed.
        from mycroftcompute.sources.datadog import REQUIRED_SCOPES

        assert REQUIRED_SCOPES == ("metrics_read", "timeseries_query")


class TestCredentialsAndSites:
    def test_both_keys_are_sent(self) -> None:
        headers = DatadogEndpoint(api_key="a", app_key="b").headers()
        assert headers["DD-API-KEY"] == "a"
        assert headers["DD-APPLICATION-KEY"] == "b"

    def test_each_site_has_its_own_host(self) -> None:
        assert DatadogEndpoint("a", "b", "eu1").base_url == "https://api.datadoghq.eu"
        assert DatadogEndpoint("a", "b", "us3").base_url == "https://api.us3.datadoghq.com"

    def test_an_unknown_site_is_refused_with_the_list(self) -> None:
        # Reading the wrong site authenticates cleanly and returns nothing, which looks exactly
        # like "no data" and is not - so a typo has to fail loudly here.
        with pytest.raises(SourceError, match="not a Datadog site"):
            _ = DatadogEndpoint("a", "b", "eu-west-1").base_url

    def test_an_explicit_url_is_allowed_through(self) -> None:
        assert DatadogEndpoint("a", "b", "https://api.example.test/").base_url == (
            "https://api.example.test"
        )

    def test_a_backwards_range_is_refused(self) -> None:
        with pytest.raises(SourceError, match="end date is before"):
            _source().fetch(date(2026, 9, 8), DAY)

    def test_an_absurd_range_is_refused_before_datadog_sees_it(self) -> None:
        with pytest.raises(SourceError, match="is the limit in one pull"):
            _source().fetch(date(2020, 1, 1), DAY)
