"""Reading Claude Code's own transcripts.

This is the only usage source a Pro or Max subscriber has, because a flat-fee plan produces no
API bill to export. It is also the richest one: per-message rather than per-day, and it reports
the five-minute against one-hour cache-write split that no vendor CSV carries.

It also reads files containing every prompt and every file the user has ever shown Claude, which
is why the first test here is about what it does *not* read.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from mycroftcompute.analysis.findings import analyse
from mycroftcompute.parsers.claude_code import parse_claude_code, transcript_paths
from mycroftcompute.parsers.model import ParseError, Severity
from mycroftcompute.vendors import Vendor

# A sentinel standing in for prompt text.
PROMPT_SENTINEL = "PRIVATE-PROMPT-THAT-MUST-NEVER-SURFACE"


def _line(
    *,
    day: str = "2026-09-08T10:00:00.000Z",
    model: str = "claude-sonnet-5",
    cwd: str = r"C:\Users\dell\some-project",
    uncached: int = 100,
    cache_read: int = 30_000,
    cache_write: int = 5_000,
    write_1h: int = 5_000,
    output: int = 500,
) -> str:
    return json.dumps(
        {
            "type": "assistant",
            "timestamp": day,
            "cwd": cwd,
            "sessionId": "abc-123",
            "message": {
                "model": model,
                # Real transcripts carry the whole conversation here.
                "content": [{"type": "text", "text": PROMPT_SENTINEL}],
                "usage": {
                    "input_tokens": uncached,
                    "cache_read_input_tokens": cache_read,
                    "cache_creation_input_tokens": cache_write,
                    "output_tokens": output,
                    "service_tier": "standard",
                    "cache_creation": {
                        "ephemeral_1h_input_tokens": write_1h,
                        "ephemeral_5m_input_tokens": cache_write - write_1h,
                    },
                },
            },
        }
    )


def _transcripts(tmp_path: Path, lines: list[str], name: str = "session.jsonl") -> Path:
    root = tmp_path / "projects" / "proj"
    root.mkdir(parents=True, exist_ok=True)
    (root / name).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return tmp_path / "projects"


class TestItNeverReadsTheConversation:
    """A cost tool that quietly ingests the text of everything you have ever asked would be a
    worse product than no tool at all."""

    def test_no_prompt_text_reaches_the_output(self, tmp_path: Path) -> None:
        parsed = parse_claude_code(_transcripts(tmp_path, [_line(), _line()]))
        rendered = json.dumps(
            [
                {
                    "model": r.model,
                    "project": r.project,
                    "tier": r.service_tier,
                    "day": r.day.isoformat(),
                }
                for r in parsed.records
            ]
        )
        assert PROMPT_SENTINEL not in rendered
        assert PROMPT_SENTINEL not in json.dumps([i.message for i in parsed.issues])

    def test_only_the_directory_leaf_is_kept_not_the_whole_path(self, tmp_path: Path) -> None:
        # A full path can carry a client name. The leaf is enough to group by.
        parsed = parse_claude_code(
            _transcripts(tmp_path, [_line(cwd=r"C:\Users\dell\clients\acme-corp\billing")])
        )
        assert parsed.records[0].project == "billing"


class TestTheAccounting:
    def test_messages_aggregate_into_daily_per_model_records(self, tmp_path: Path) -> None:
        root = _transcripts(tmp_path, [_line(), _line(), _line(day="2026-09-09T11:00:00.000Z")])
        parsed = parse_claude_code(root)
        assert parsed.rows_seen == 3
        assert len(parsed.records) == 2  # two days, one model, one project
        first = next(r for r in parsed.records if r.day == date(2026, 9, 8))
        assert first.requests == 2
        assert first.cached_input_tokens == 60_000
        assert first.input_tokens == 200

    def test_the_one_hour_cache_write_split_is_carried(self, tmp_path: Path) -> None:
        # Anthropic bills a one-hour cache write at 2x list against 1.25x for five minutes.
        # No console export reports which was used; a transcript does, and in real usage the
        # one-hour TTL dominates - so dropping this would understate the common case by 60%.
        root = _transcripts(tmp_path, [_line(cache_write=8_000, write_1h=6_000)])
        record = parse_claude_code(root).records[0]
        assert record.cache_write_tokens == 8_000
        assert record.cache_write_1h_tokens == 6_000

    def test_it_knows_the_cache_split_and_admits_it_knows_no_cost(self, tmp_path: Path) -> None:
        parsed = parse_claude_code(_transcripts(tmp_path, [_line()]))
        assert parsed.knows_cache_split
        assert not parsed.knows_cost  # a subscription transcript has no billed amount

    def test_models_are_kept_apart(self, tmp_path: Path) -> None:
        root = _transcripts(
            tmp_path, [_line(model="claude-opus-5"), _line(model="claude-sonnet-5")]
        )
        assert {r.model for r in parse_claude_code(root).records} == {
            "claude-opus-5",
            "claude-sonnet-5",
        }

    def test_synthetic_bookkeeping_records_are_ignored(self, tmp_path: Path) -> None:
        # Claude Code writes "<synthetic>" entries that carry no real billing; counting them
        # would put a phantom model in the report.
        root = _transcripts(tmp_path, [_line(), _line(model="<synthetic>")])
        parsed = parse_claude_code(root)
        assert parsed.rows_seen == 1
        assert all(not r.model.startswith("<") for r in parsed.records)


class TestItSaysWhatASubscriptionCannotTellYou:
    def test_the_flat_fee_caveat_is_always_raised(self, tmp_path: Path) -> None:
        parsed = parse_claude_code(_transcripts(tmp_path, [_line()]))
        issue = next(i for i in parsed.issues if i.code == "subscription_not_billed_per_token")
        assert issue.severity is Severity.WARNING
        assert "not what you paid" in issue.message
        assert "flat fee" in issue.message

    def test_it_survives_the_whole_pipeline(self, tmp_path: Path) -> None:
        root = _transcripts(tmp_path, [_line(uncached=9_000_000, cache_read=100)])
        parsed = parse_claude_code(root)
        analysis = analyse(parsed)
        assert parsed.vendor is Vendor.ANTHROPIC
        assert analysis.findings  # a cold cache is still a cold cache


class TestBadInput:
    def test_an_empty_directory_says_where_transcripts_live(self, tmp_path: Path) -> None:
        (tmp_path / "empty").mkdir()
        with pytest.raises(ParseError, match=r"~/\.claude/projects"):
            parse_claude_code(tmp_path / "empty")

    def test_unparseable_lines_are_counted_not_guessed(self, tmp_path: Path) -> None:
        root = _transcripts(tmp_path, [_line(), "{not json at all", _line()])
        parsed = parse_claude_code(root)
        assert parsed.rows_seen == 2
        assert parsed.rows_rejected == 1

    def test_a_record_with_no_timestamp_is_skipped(self, tmp_path: Path) -> None:
        bad = json.dumps({"message": {"model": "claude-sonnet-5", "usage": {"input_tokens": 1}}})
        parsed = parse_claude_code(_transcripts(tmp_path, [_line(), bad]))
        assert parsed.rows_seen == 1

    def test_a_non_integer_token_count_is_discarded_not_coerced(self, tmp_path: Path) -> None:
        weird = json.dumps(
            {
                "timestamp": "2026-09-08T10:00:00.000Z",
                "message": {
                    "model": "claude-sonnet-5",
                    "usage": {"input_tokens": "lots", "cache_read_input_tokens": 500},
                },
            }
        )
        record = parse_claude_code(_transcripts(tmp_path, [weird])).records[0]
        assert record.input_tokens == 0
        assert record.cached_input_tokens == 500

    def test_a_single_file_works_as_well_as_a_directory(self, tmp_path: Path) -> None:
        root = _transcripts(tmp_path, [_line()])
        one = next(iter(transcript_paths(root)))
        assert parse_claude_code(one).rows_seen == 1
