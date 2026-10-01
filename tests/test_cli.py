"""The command line, and the exit codes it uses to say what went wrong."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mycroftcompute.__main__ import main

#: One assistant message, in the shape Claude Code writes to ~/.claude/projects.
TRANSCRIPT = json.dumps(
    {
        "timestamp": "2026-08-03T10:00:00Z",
        "cwd": "/home/dev/api",
        "message": {
            "model": "claude-sonnet-4-6",
            "usage": {
                "input_tokens": 9_000_000,
                "cache_read_input_tokens": 200_000,
                "cache_creation_input_tokens": 0,
                "output_tokens": 150_000,
            },
        },
    }
)


def _transcripts(tmp_path: Path) -> Path:
    path = tmp_path / "session.jsonl"
    path.write_text(TRANSCRIPT + "\n", encoding="utf-8")
    return tmp_path


class TestYourOwnTranscripts:
    """The default: with no backend named, the CLI reads Claude Code's own session files."""

    def test_it_reads_transcripts_and_exits_zero(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main([str(_transcripts(tmp_path)), "--today", "2026-09-22"]) == 0
        assert "MYCROFT" in capsys.readouterr().out

    def test_a_missing_path_says_so_without_a_traceback(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main([str(tmp_path / "nope")]) == 2
        err = capsys.readouterr().err
        assert "no Claude Code transcripts" in err
        assert "--sample" in err

    def test_a_directory_with_no_transcripts_explains_where_they_live(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main([str(tmp_path), "--today", "2026-09-22"]) == 1
        assert "~/.claude/projects" in capsys.readouterr().err

    def test_an_old_price_table_warns_and_still_reports(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # A personal tool outlives its last price check. It says how old the prices are, above
        # the report, rather than refusing to run.
        assert main([str(_transcripts(tmp_path)), "--today", "2027-01-01"]) == 0
        captured = capsys.readouterr()
        assert "price table may be out of date" in captured.err
        assert "MYCROFT" in captured.out

    def test_no_arguments_reads_the_default_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # CLAUDE_CONFIG_DIR moves ~/.claude, and the transcripts with it.
        (tmp_path / "projects").mkdir()
        _transcripts(tmp_path / "projects")
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        assert main(["--today", "2026-09-22"]) == 0
        assert "MYCROFT" in capsys.readouterr().out

    def test_no_transcripts_anywhere_points_at_the_sample(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "nothing-here"))
        assert main([]) == 2
        assert "--sample" in capsys.readouterr().err

    def test_the_sample_needs_no_data(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert main(["--sample"]) == 0
        out = capsys.readouterr().out
        assert "SINCE LAST TIME" in out
        assert "invented team" in out


class TestTheHistory:
    def test_a_later_period_compares_itself_with_the_earlier_one(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        db = tmp_path / "history.db"
        july = tmp_path / "july"
        july.mkdir()
        (july / "session.jsonl").write_text(
            TRANSCRIPT.replace("2026-08-03", "2026-07-06") + "\n", encoding="utf-8"
        )
        august = tmp_path / "august"
        august.mkdir()
        _transcripts(august)
        assert main([str(july), "--today", "2026-09-22", "--db", str(db)]) == 0
        capsys.readouterr()
        assert main([str(august), "--today", "2026-09-22", "--db", str(db)]) == 0
        assert "SINCE LAST TIME" in capsys.readouterr().out

    def test_reading_the_same_days_again_is_a_correction_not_a_comparison(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        db = tmp_path / "history.db"
        args = [str(_transcripts(tmp_path)), "--today", "2026-09-22", "--db", str(db)]
        assert main(args) == 0
        capsys.readouterr()
        assert main(args) == 0
        assert "SINCE LAST TIME" not in capsys.readouterr().out

    def test_a_read_under_an_old_price_table_is_still_kept(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The tokens are facts whatever the prices; the history keeps them for next month.
        db = tmp_path / "history.db"
        assert main([str(_transcripts(tmp_path)), "--today", "2027-01-01", "--db", str(db)]) == 0
        assert db.exists()


class TestReadingFromAMetricsBackend:
    def test_a_backend_that_is_not_there_says_so_without_a_traceback(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["--grafana", "http://127.0.0.1:1/api/prom", "--today", "2026-09-22"]) == 1
        err = capsys.readouterr().err
        assert "Traceback" not in err
        assert "Could not reach" in err or "mycroftcompute:" in err

    def test_the_token_can_come_from_the_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A token passed as a flag lands in shell history and in ps output, so the environment
        # is the documented path.
        seen: dict[str, object] = {}

        def fake(url: str, headers: dict[str, str]) -> dict[str, object]:
            seen["auth"] = headers.get("Authorization")
            raise SystemExit(0)

        monkeypatch.setenv("MYCROFT_METRICS_TOKEN", "glc_from_env")
        monkeypatch.setattr("mycroftcompute.__main__.GrafanaSource", _capturing_source(fake))
        with pytest.raises(SystemExit):
            main(["--grafana", "https://example.invalid", "--user", "1234", "--discover"])
        assert seen["auth"] is not None

    def test_discover_reports_what_the_backend_has(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        def fake(url: str, headers: dict[str, str]) -> dict[str, object]:
            if "__name__/values" in url:
                return {"status": "success", "data": ["claude_code_token_usage_tokens_total"]}
            return {"status": "success", "data": ["model", "type"]}

        monkeypatch.setattr("mycroftcompute.__main__.GrafanaSource", _capturing_source(fake))
        assert main(["--grafana", "https://example.invalid", "--discover"]) == 0
        out = " ".join(capsys.readouterr().out.split())
        assert "claude_code_token_usage_tokens_total" in out
        assert "https://example.invalid" in out
        # No identity label present, and the reader is told what that absence costs them.
        assert "[--] Developer identity" in out
        assert "Per-developer figures cannot be produced" in out


class TestReadingDatadog:
    """Datadog needs two keys, and the second one is the one people forget."""

    def test_both_keys_can_come_from_the_environment(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # A key on the command line lands in shell history and in `ps` output, so the
        # environment is the documented path.
        monkeypatch.setenv("MYCROFT_METRICS_TOKEN", "dd-api")
        monkeypatch.setenv("MYCROFT_DATADOG_APP_KEY", "dd-app")
        seen: dict[str, str] = {}

        def fake(url: str, headers: dict[str, str]) -> dict[str, object]:
            seen.update(headers)
            if "/api/v1/metrics" in url:
                return {"metrics": ["gen_ai.client.token.usage.sum"]}
            return {"data": {"attributes": {"tags": ["gen_ai.token.type:input"]}}}

        monkeypatch.setattr("mycroftcompute.__main__.DatadogSource", _capturing_datadog(fake))
        assert main(["--datadog", "--discover"]) == 0
        assert seen["DD-API-KEY"] == "dd-api"
        assert seen["DD-APPLICATION-KEY"] == "dd-app"
        assert "gen_ai.client.token.usage.sum" in capsys.readouterr().out

    def test_a_missing_application_key_says_which_one_is_missing(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("MYCROFT_METRICS_TOKEN", "dd-api")
        monkeypatch.delenv("MYCROFT_DATADOG_APP_KEY", raising=False)
        assert main(["--datadog"]) == 1
        err = capsys.readouterr().err
        assert "Application key" in err
        assert "metrics_read" in err

    def test_two_backends_at_once_is_refused(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit):
            main(["--grafana", "https://example.invalid", "--datadog", "eu1"])
        assert "one backend at a time" in capsys.readouterr().err


def _capturing_datadog(fake: object) -> object:
    from mycroftcompute.sources.datadog import DatadogSource

    def build(endpoint: object) -> DatadogSource:
        return DatadogSource(endpoint, fake)  # type: ignore[arg-type]

    return build


def _capturing_source(fake: object) -> object:
    from mycroftcompute.sources.grafana import GrafanaSource

    def build(endpoint: object) -> GrafanaSource:
        return GrafanaSource(endpoint, fake)  # type: ignore[arg-type]

    return build
