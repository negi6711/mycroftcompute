"""The screen a customer sees seconds after pasting a credential.

This is where trust is won or lost, and it is lost in two specific ways: printing nothing until
the full report arrives, or printing a clean page that quietly omits the half we could not read.
So every test here is about an absence being visible and carrying its consequence.
"""

from __future__ import annotations

import re
from typing import Any

from mycroftcompute.report.readiness import checks, render_readiness
from mycroftcompute.sources.grafana import GrafanaSource, PrometheusEndpoint

RICH_NAMES = [
    "gen_ai_client_token_usage_tokens_sum",
    "gen_ai_client_token_usage_tokens_bucket",
    "claude_code_token_usage_tokens_total",
    "claude_code_cost_usage_USD_total",
    "up",
]
RICH_LABELS = {
    "gen_ai_client_token_usage_tokens_sum": [
        "gen_ai_token_type",
        "gen_ai_request_model",
        "gen_ai_system",
        "user_email",
    ],
    "claude_code_token_usage_tokens_total": [
        "type",
        "model",
        "user_id",
        "repository",
        "query_source",
        "effort",
    ],
    "claude_code_cost_usage_USD_total": ["model", "user_id"],
}
KINDS = {
    "gen_ai_token_type": ["input", "output"],
    "type": ["input", "output", "cacheRead", "cacheCreation"],
}


def _discover(names: list[str], labels: dict[str, list[str]]) -> Any:
    def get(url: str, headers: dict[str, str]) -> Any:
        if "__name__/values" in url:
            return {"status": "success", "data": names}
        metric = next((n for n in names if n in url), "")
        if "/api/v1/labels" in url:
            return {"status": "success", "data": labels.get(metric, [])}
        if "/api/v1/label/" in url:
            key = url.split("/api/v1/label/")[1].split("/values")[0]
            return {"status": "success", "data": KINDS.get(key, [])}
        return {"status": "success", "data": {"result": []}}

    return GrafanaSource(PrometheusEndpoint("https://example.invalid"), get).discover()


def _prose(found: Any, *, where: str = "x") -> str:
    """The page with its line breaks collapsed.

    Prose is wrapped to the page width, so a sentence lands across two lines. Asserting on the
    wrapped form would make every copy edit a test failure over something that is not the point;
    line width has its own test below.
    """
    return re.sub(r"\s+", " ", render_readiness(found, where=where))


def _named(rows: list[Any], name: str) -> Any:
    return next(c for c in rows if c.name == name)


class TestAFullyInstrumentedBackend:
    def test_every_capability_is_present(self) -> None:
        rows = checks(_discover(RICH_NAMES, RICH_LABELS))
        assert all(c.present for c in rows)

    def test_it_says_ready_and_names_no_gaps(self) -> None:
        page = render_readiness(_discover(RICH_NAMES, RICH_LABELS), where="x")
        assert "Ready." in page
        assert "will be unavailable" not in page

    def test_both_metric_families_are_shown_not_just_the_first(self) -> None:
        rows = checks(_discover(RICH_NAMES, RICH_LABELS))
        detail = _named(rows, "Token counts").detail
        assert "gen_ai_client_token_usage_tokens_sum" in detail
        assert "claude_code_token_usage_tokens_total" in detail

    def test_the_cache_row_lists_only_cache_kinds(self) -> None:
        # Listing input and output there would read as though they were evidence of caching.
        detail = _named(checks(_discover(RICH_NAMES, RICH_LABELS)), "Cache split").detail
        assert "cacheRead" in detail
        assert "input" not in detail


class TestEveryBlindSpotCarriesItsCost:
    def test_a_missing_identity_label_says_what_it_costs_and_why_it_is_missing(self) -> None:
        labels = {
            "claude_code_token_usage_tokens_total": ["type", "model", "query_source", "effort"]
        }
        page = _prose(_discover(["claude_code_token_usage_tokens_total"], labels))
        assert "[--] Developer identity" in page
        assert "first thing a platform lead asks for" in page
        assert "cardinality" in page

    def test_missing_segments_name_the_finding_they_cost(self) -> None:
        labels = {
            "gen_ai_client_token_usage_tokens_sum": ["gen_ai_token_type", "gen_ai_request_model"]
        }
        page = _prose(_discover(["gen_ai_client_token_usage_tokens_sum"], labels))
        assert "[--] Request segments" in page
        assert "agent's own housekeeping" in page
        assert "GenAI conventions have no equivalent" in page

    def test_a_missing_cache_split_refuses_to_imply_a_zero_hit_rate(self) -> None:
        labels = {
            "gen_ai_client_token_usage_tokens_sum": ["gen_ai_token_type", "gen_ai_request_model"]
        }
        page = _prose(_discover(["gen_ai_client_token_usage_tokens_sum"], labels))
        assert "a cold cache are different things" in page

    def test_a_readable_backend_with_gaps_still_says_ready(self) -> None:
        labels = {
            "gen_ai_client_token_usage_tokens_sum": ["gen_ai_token_type", "gen_ai_request_model"]
        }
        page = _prose(_discover(["gen_ai_client_token_usage_tokens_sum"], labels))
        assert "Ready." in page
        assert "Some findings will be unavailable" in page


class TestABackendWeCannotUse:
    def test_no_agent_metrics_at_all_blocks_and_says_how_to_fix_it(self) -> None:
        page = _prose(_discover(["up", "go_goroutines"], {}))
        assert "cannot produce a report" in page
        assert "Token counts is missing" in page
        assert "org-wide through managed settings" in page

    def test_tokens_with_no_kind_label_block_rather_than_guess(self) -> None:
        # Input and output are priced five to ten times apart. One undifferentiated total is
        # not something to split on an assumed ratio.
        labels = {"gen_ai_client_token_usage_tokens_sum": ["gen_ai_request_model"]}
        page = _prose(_discover(["gen_ai_client_token_usage_tokens_sum"], labels))
        assert "cannot produce a report" in page
        assert "five to ten times apart" in page

    def test_a_histogram_with_only_buckets_counts_as_no_tokens(self) -> None:
        page = _prose(_discover(["gen_ai_client_token_usage_tokens_bucket"], {}))
        assert "Token counts is missing" in page


class TestThePageItself:
    def test_it_names_where_it_looked(self) -> None:
        page = render_readiness(_discover(RICH_NAMES, RICH_LABELS), where="https://x.invalid")
        assert "https://x.invalid" in page

    def test_no_line_is_wider_than_the_page(self) -> None:
        # It ends up pasted into an email more often than not.
        page = render_readiness(_discover(RICH_NAMES, RICH_LABELS), where="x")
        assert max(len(line) for line in page.splitlines()) <= 84

    def test_it_never_leaves_trailing_whitespace_on_a_row(self) -> None:
        page = render_readiness(_discover(RICH_NAMES, RICH_LABELS), where="x")
        assert not any(line != line.rstrip() for line in page.splitlines())
