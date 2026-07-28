"""Regression guard for openspec_adapter's JSON parsing.

The fixtures in ``tests/fixtures/openspec/`` are real payloads captured from
``@fission-ai/openspec@1.6.0``. OpenSpec ships ~2 releases/month and its own
agent contract self-reports inconsistent key casing, so these tests exist to
fail loudly when a shape moves under us rather than letting a gate silently
start passing everything.

Regenerate the fixtures (after deliberately bumping the pin) by re-running the
capture documented in ``docs/LIFECYCLE.md``.
"""

import json
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

import openspec_adapter as osa

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "openspec"


def _result(fixture: str, exit_code: int) -> osa.Result:
    raw = (FIXTURES / fixture).read_text(encoding="utf-8")
    return osa.Result(
        ok=exit_code == 0,
        exit_code=exit_code,
        data=json.loads(raw),
        stdout=raw,
        stderr="",
        argv=["openspec"],
    )


# ---------------------------------------------------------------------------
# Naming — must round-trip, since there is no ticket<->change join table
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ticket_id,title,expected",
    [
        ("B-44", "Knowledge ingestion pipeline", "b-44-knowledge-ingestion-pipeline"),
        ("b-7", "Fix  double   spaces", "b-7-fix-double-spaces"),
        ("W-2", "Punctuation: it's here!", "w-2-punctuation-it-s-here"),
        ("B-9", "", "b-9"),
    ],
)
def test_change_name(ticket_id, title, expected):
    assert osa.change_name(ticket_id, title) == expected


def test_change_name_round_trips_to_ticket_id():
    name = osa.change_name("B-44", "Knowledge ingestion pipeline")
    assert osa.ticket_id_from_change_name(name) == "B-44"


def test_change_name_is_bounded():
    name = osa.change_name("B-1", "word " * 60)
    assert len(name) <= 80
    assert not name.endswith("-")


def test_ticket_id_from_unrecognised_change_name_is_empty():
    assert osa.ticket_id_from_change_name("some-hand-written-change") == ""


# ---------------------------------------------------------------------------
# validate — the exit code is the contract; the payload explains a refusal
# ---------------------------------------------------------------------------


def test_validate_ok_fixture_has_no_errors():
    res = _result("validate-ok.json", 0)
    assert res.ok
    assert osa.validation_errors(res) == []
    assert res.data["items"][0]["valid"] is True


def test_validate_fail_fixture_surfaces_only_error_level_issues():
    res = _result("validate-fail.json", 1)
    assert not res.ok
    errors = osa.validation_errors(res)
    assert len(errors) == 1, "INFO-level issues must not be reported as failures"
    assert "must include at least one scenario" in errors[0]
    assert errors[0].startswith("sample/spec.md:")


# ---------------------------------------------------------------------------
# archive — refusal must be legible, success must report the spec merge
# ---------------------------------------------------------------------------


def test_archive_refused_fixture_yields_no_summary_and_a_reason():
    res = _result("archive-refused.json", 1)
    assert not res.ok
    assert osa.archive_summary(res) == {}
    assert "Validation failed" in res.message


def test_archive_ok_fixture_reports_the_spec_merge():
    res = _result("archive-ok.json", 0)
    summary = osa.archive_summary(res)
    assert summary["change"] == "b-1-sample-capability"
    assert summary["specsUpdated"] is True
    assert summary["totals"]["added"] == 1
    assert summary["archivedAs"].endswith("b-1-sample-capability")


# ---------------------------------------------------------------------------
# status — artifact ids the Orchestrator prompt and the gates depend on
# ---------------------------------------------------------------------------


def test_status_fixture_exposes_the_four_spec_driven_artifacts():
    res = _result("status.json", 0)
    states = osa.artifact_states(res)
    assert set(states) == {"proposal", "design", "specs", "tasks"}
    assert res.data["changeName"] == "b-1-sample-capability"
    assert res.data["schemaName"] == "spec-driven"
    assert res.data["applyRequires"] == ["tasks"]


def test_artifact_states_tolerates_a_non_dict_payload():
    assert osa.artifact_states(osa.Result(ok=False, exit_code=1, data=None)) == {}


# ---------------------------------------------------------------------------
# Plumbing
# ---------------------------------------------------------------------------


def test_env_disables_telemetry_with_the_exact_string_the_cli_checks():
    env = osa._env()
    assert env["OPENSPEC_TELEMETRY"] == "0"
    assert env["DO_NOT_TRACK"] == "1"


def test_npx_fallback_never_uses_the_squatted_bare_package_name():
    assert osa._NPX_FALLBACK[-1] == f"{osa.NPM_PACKAGE}@{osa.REQUIRED_VERSION}"
    assert "openspec" not in osa._NPX_FALLBACK[:-1]


def test_json_parser_tolerates_a_leading_progress_line():
    assert osa._parse_json('- Creating...\n{"a": 1}') == {"a": 1}
    assert osa._parse_json("") is None
    assert osa._parse_json("not json at all") is None


def test_message_prefers_structured_status_over_stderr():
    res = osa.Result(
        ok=False,
        exit_code=1,
        data={"status": [{"severity": "error", "message": "Boom.", "fix": "Do X."}]},
        stderr="noise",
    )
    assert res.message == "Boom. Do X."
