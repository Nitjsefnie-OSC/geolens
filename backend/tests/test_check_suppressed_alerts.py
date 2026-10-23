"""Open alerts at source-suppressed locations are dismissed directly."""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_suppressed_alerts.py"
_spec = importlib.util.spec_from_file_location("check_suppressed_alerts", _SCRIPT)
assert _spec and _spec.loader
checker = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(checker)

PATH = "backend/app/processing/ingest/metadata_extent.py"


def _result(line: int, *, suppressed: bool) -> dict:
    result = {
        "ruleId": "py/sql-injection",
        "locations": [
            {
                "physicalLocation": {
                    "artifactLocation": {"uri": PATH},
                    "region": {"startLine": line, "startColumn": 13},
                }
            }
        ],
    }
    if suppressed:
        result["suppressions"] = [{"kind": "inSource"}]
    return result


def _alert(number: int, line: int) -> dict:
    return {
        "number": number,
        "rule": {"id": "py/sql-injection"},
        "most_recent_instance": {
            "location": {"path": PATH, "start_line": line, "start_column": 13}
        },
    }


def test_open_alert_at_a_suppressed_location_is_reported() -> None:
    sarif = {"runs": [{"results": [_result(148, suppressed=True)]}]}
    assert [a["number"] for a in checker.stuck_alerts(sarif, [_alert(136, 148)])] == [
        136
    ]


def test_open_alert_without_a_suppression_is_left_alone() -> None:
    sarif = {
        "runs": [
            {"results": [_result(148, suppressed=True), _result(200, suppressed=False)]}
        ]
    }
    assert checker.stuck_alerts(sarif, [_alert(7, 200), _alert(8, 300)]) == []


def test_stuck_alert_is_dismissed_with_marker_and_audit_log(
    monkeypatch, capsys
) -> None:
    sarif = {"runs": [{"results": [_result(148, suppressed=True)]}]}
    monkeypatch.setattr(checker, "_load_sarif", lambda path: sarif)
    monkeypatch.setattr(checker, "_open_alerts", lambda repo: [_alert(136, 148)])
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(checker.subprocess, "run", run)
    status = checker.main(["check_suppressed_alerts.py", "results.sarif", "owner/repo"])

    assert calls == [
        (
            [
                "gh",
                "api",
                "--method",
                "PUT",
                "repos/owner/repo/code-scanning/alerts/136",
                "-f",
                "state=dismissed",
                "-f",
                "dismissed_reason=wont_fix",
                "-f",
                "dismissed_comment=Suppressed in source by # codeql[py/sql-injection].",
            ],
            {"check": True, "capture_output": True, "text": True},
        )
    ]
    assert status == 0
    assert capsys.readouterr().out.splitlines() == [
        f"Dismissed alert 136 (py/sql-injection at {PATH}:148)"
    ]


def test_failed_dismissal_returns_failure_with_error(monkeypatch, capsys) -> None:
    sarif = {"runs": [{"results": [_result(148, suppressed=True)]}]}
    monkeypatch.setattr(checker, "_load_sarif", lambda path: sarif)
    monkeypatch.setattr(checker, "_open_alerts", lambda repo: [_alert(136, 148)])
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        raise subprocess.CalledProcessError(1, argv, stderr="dismissal denied")

    monkeypatch.setattr(checker.subprocess, "run", run)
    status = checker.main(["check_suppressed_alerts.py", "results.sarif", "owner/repo"])

    assert len(calls) == 1
    assert status == 1
    assert capsys.readouterr().out.splitlines() == [
        f"::error::Failed to dismiss alert 136 (py/sql-injection at {PATH}:148): dismissal denied"
    ]


def test_non_stuck_alerts_are_not_dismissed(monkeypatch) -> None:
    sarif = {
        "runs": [
            {"results": [_result(148, suppressed=True), _result(200, suppressed=False)]}
        ]
    }
    monkeypatch.setattr(checker, "_load_sarif", lambda path: sarif)
    monkeypatch.setattr(
        checker, "_open_alerts", lambda repo: [_alert(7, 200), _alert(8, 300)]
    )
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        raise AssertionError("no dismissal expected")

    monkeypatch.setattr(checker.subprocess, "run", run)
    assert (
        checker.main(["check_suppressed_alerts.py", "results.sarif", "owner/repo"]) == 0
    )
    assert calls == []
