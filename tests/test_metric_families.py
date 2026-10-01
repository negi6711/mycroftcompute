"""Reading more than one telemetry vocabulary out of one backend.

A company running Claude Code alongside Codex typically has both the vendor-neutral
`gen_ai.client.token.usage` and Claude Code's own `claude_code.token.usage`. Reading only the
first loses whichever half matters, and the half that matters is usually the segments: the
standard conventions have no `query_source` and no `effort`, so without the native metric the
routable-segment finding - the strongest routing argument in the product - cannot run at all.

The other half of this file is the rule that keeps the merge safe: families are never summed.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from mycroftcompute.analysis.findings import FindingKind, analyse
from mycroftcompute.sources.grafana import GrafanaSource, PrometheusEndpoint
from mycroftcompute.sources.merging import Family, Part, merge
from mycroftcompute.sources.records import Accumulator, Key
from mycroftcompute.vendors import Vendor

DAY = date(2026, 9, 7)
#: Prometheus stamps a daily sample at the END of the window it covers, so the
#: sample carrying 2026-09-07's usage is stamped midnight on the 8th. Getting this backwards
#: dates every day's spend one day late - which is what a live Grafana Cloud stack revealed.
DAY1 = 1_788_825_600  # midnight 2026-09-08Z: the sample for 2026-09-07

STANDARD = "gen_ai_client_token_usage_tokens_sum"
NATIVE = "claude_code_token_usage_tokens_total"

#: The standard metric's vocabulary. No query_source, no effort - the conventions have neither.
STANDARD_LABELS = [
    "gen_ai_token_type",
    "gen_ai_request_model",
    "gen_ai_system",
    "user_email",
    "repository",
]

#: Claude Code's own. Different spellings for the same ideas, plus the two that matter.
NATIVE_LABELS = ["type", "model", "user_id", "repository", "query_source", "effort"]


def _series(labels: dict[str, str], value: str) -> dict[str, Any]:
    return {"metric": labels, "values": [[DAY1, value]]}


def _std(model: str, system: str, kind: str, value: str) -> dict[str, Any]:
    return _series(
        {
            "gen_ai_token_type": kind,
            "gen_ai_request_model": model,
            "gen_ai_system": system,
            "user_email": "ben@example.com",
            "repository": "web",
        },
        value,
    )


def _native(
    model: str, kind: str, value: str, *, source: str = "main", effort: str = "high"
) -> dict[str, Any]:
    return _series(
        {
            "type": kind,
            "model": model,
            "user_id": "ana",
            "repository": "api",
            "query_source": source,
            "effort": effort,
        },
        value,
    )


def _backend(
    names: list[str],
    labels: dict[str, list[str]],
    series: dict[str, list[dict[str, Any]]],
) -> Any:
    """A backend whose labels differ per metric, which is the whole point of this file."""

    def _metric_in(url: str) -> str:
        for name in names:
            if name in url:
                return name
        return ""

    def get(url: str, headers: dict[str, str]) -> dict[str, Any]:
        if "__name__/values" in url:
            return {"status": "success", "data": names}
        metric = _metric_in(url)
        if "/api/v1/labels" in url:
            return {"status": "success", "data": labels.get(metric, [])}
        if "/api/v1/label/" in url:
            key = url.split("/api/v1/label/")[1].split("/values")[0]
            seen = {s["metric"][key] for s in series.get(metric, []) if key in s["metric"]}
            return {"status": "success", "data": sorted(seen)}
        return {"status": "success", "data": {"result": series.get(metric, [])}}

    return get


def _source(
    names: list[str], labels: dict[str, list[str]], series: dict[str, Any]
) -> GrafanaSource:
    return GrafanaSource(
        PrometheusEndpoint("https://example.invalid"), _backend(names, labels, series)
    )


BOTH_NAMES = [STANDARD, NATIVE]
BOTH_LABELS = {STANDARD: STANDARD_LABELS, NATIVE: NATIVE_LABELS}


MIXED_SERIES = {
    STANDARD: [
        _std("gpt-5-6-terra", "openai", "input", "400000"),
        _std("gpt-5-6-terra", "openai", "output", "40000"),
    ],
    NATIVE: [
        _native("claude-opus-5", "input", "200000"),
        _native("claude-opus-5", "output", "20000"),
        _native("claude-opus-5", "input", "3000000", source="auxiliary", effort="low"),
        _native("claude-opus-5", "output", "300000", source="auxiliary", effort="low"),
    ],
}


class TestBothVocabulariesAreRead:
    """The bug this file was written for: discovery used to take the first family and stop."""

    def test_discovery_finds_every_family_not_just_the_first(self) -> None:
        found = _source(BOTH_NAMES, BOTH_LABELS, MIXED_SERIES).discover()
        assert [f.metric for f in found.families] == [STANDARD, NATIVE]

    def test_each_family_keeps_its_own_label_spellings(self) -> None:
        # The two vocabularies do not mix. A single backend-wide label set would have read one of
        # these families as unlabelled.
        found = _source(BOTH_NAMES, BOTH_LABELS, MIXED_SERIES).discover()
        standard, native = found.families
        assert standard.model_label == "gen_ai_request_model"
        assert standard.user_label == "user_email"
        assert native.model_label == "model"
        assert native.user_label == "user_id"

    def test_only_the_native_family_carries_segments(self) -> None:
        found = _source(BOTH_NAMES, BOTH_LABELS, MIXED_SERIES).discover()
        standard, native = found.families
        assert not standard.knows_segments
        assert native.segment_labels == ("query_source", "effort")

    def test_both_vendors_and_the_segments_survive_together(self) -> None:
        parsed = _source(BOTH_NAMES, BOTH_LABELS, MIXED_SERIES).fetch(DAY, DAY)
        assert parsed.all_vendors == frozenset({Vendor.ANTHROPIC, Vendor.OPENAI})
        assert {r.query_source for r in parsed.records} == {"main", "auxiliary", None}

    def test_the_routing_finding_can_now_run(self) -> None:
        # This is the point. Read through the standard alone, this finding is impossible.
        parsed = _source(BOTH_NAMES, BOTH_LABELS, MIXED_SERIES).fetch(DAY, DAY)
        kinds = [f.kind for f in analyse(parsed).findings]
        assert FindingKind.ROUTABLE_SEGMENT in kinds

    def test_the_reader_is_told_both_were_read(self) -> None:
        parsed = _source(BOTH_NAMES, BOTH_LABELS, MIXED_SERIES).fetch(DAY, DAY)
        issue = next(i for i in parsed.issues if i.code == "several_metric_families")
        assert STANDARD in issue.message
        assert NATIVE in issue.message

    def test_the_standard_alone_still_works_without_segments(self) -> None:
        parsed = _source([STANDARD], BOTH_LABELS, MIXED_SERIES).fetch(DAY, DAY)
        assert all(r.query_source is None for r in parsed.records)
        assert analyse(parsed).costed is not None


SAME_MODEL_SERIES = {
    STANDARD: [
        _std("claude-opus-5", "anthropic", "input", "1000000"),
        _std("claude-opus-5", "anthropic", "output", "100000"),
    ],
    NATIVE: [
        _native("claude-opus-5", "input", "1000000"),
        _native("claude-opus-5", "output", "100000"),
    ],
}


class TestFamiliesAreNeverSummed:
    """A doubled bill is the one error that ends the relationship. Worse than a missing
    dimension, which we can at least describe."""

    def test_a_model_both_families_report_is_counted_once(self) -> None:
        parsed = _source(BOTH_NAMES, BOTH_LABELS, SAME_MODEL_SERIES).fetch(DAY, DAY)
        assert sum(r.input_tokens or 0 for r in parsed.records) == 1_000_000

    def test_the_richer_family_wins_the_contested_model(self) -> None:
        # Both report it; the one that can say query_source is the better authority for its own
        # traffic, because it is emitted by the process that made the request.
        parsed = _source(BOTH_NAMES, BOTH_LABELS, SAME_MODEL_SERIES).fetch(DAY, DAY)
        assert [r.user for r in parsed.records] == ["ana"]  # the native family's identity label

    def test_the_reader_is_told_which_source_won(self) -> None:
        parsed = _source(BOTH_NAMES, BOTH_LABELS, SAME_MODEL_SERIES).fetch(DAY, DAY)
        issue = next(i for i in parsed.issues if i.code == "contested_models")
        assert "claude-opus-5" in issue.message
        assert NATIVE in issue.message
        assert "would double that model's bill" in issue.message


class TestTheMergeRuleOnItsOwn:
    @staticmethod
    def _part(metric: str, model: str, tokens: int, *, segments: bool) -> Part:
        family = Family(
            metric=metric,
            kind_label="type",
            model_label="model",
            segment_labels=("query_source",) if segments else (),
        )
        acc = Accumulator()
        acc.add_tokens(Key(model=model, day=DAY), "input_tokens", tokens)
        return Part(family=family, accumulator=acc)

    def test_disjoint_families_are_a_clean_union(self) -> None:
        merged = merge(
            [
                self._part(STANDARD, "gpt-5-6-terra", 100, segments=False),
                self._part(NATIVE, "claude-opus-5", 200, segments=True),
            ]
        )
        assert merged.contested == ()
        assert sum(c["input_tokens"] for c in merged.accumulator.tokens.values()) == 300

    def test_a_family_with_no_kind_label_contributes_nothing(self) -> None:
        # Input and output are priced five to ten times apart, so an undifferentiated total has
        # nothing safe to contribute.
        blind = Part(family=Family(metric="x", model_label="model"), accumulator=Accumulator())
        merged = merge([blind, self._part(NATIVE, "claude-opus-5", 200, segments=True)])
        assert sum(c["input_tokens"] for c in merged.accumulator.tokens.values()) == 200

    def test_nothing_readable_yields_nothing_rather_than_an_empty_claim(self) -> None:
        assert merge([]).accumulator.tokens == {}
        assert not merge([]).knows_tokens
