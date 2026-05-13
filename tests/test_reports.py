"""Unit tests for tools/reports.py: structured-report readers.

The verifier no longer scrapes stdout — these readers consume the report
files declared by the verify_planner's VerificationPlan. Each test feeds
canned XML / SARIF fixtures and asserts the parsed shape.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from darkfactory.tools.reports import (
    JUnitSummary,
    read_checkstyle_xml,
    read_junit_xml,
    read_sarif,
)


SUREFIRE_PASS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<testsuite name="com.example.UserControllerTest" tests="3" failures="0" errors="0" skipped="0" time="0.12">
  <testcase classname="com.example.UserControllerTest" name="listReturnsFirstPage" time="0.04"/>
  <testcase classname="com.example.UserControllerTest" name="listReturnsSecondPage" time="0.04"/>
  <testcase classname="com.example.UserControllerTest" name="cursorPaginationReturnsFirstPage" time="0.04"/>
</testsuite>
"""

SUREFIRE_MIXED_XML = """<?xml version="1.0" encoding="UTF-8"?>
<testsuite name="com.example.OrderControllerTest" tests="4" failures="1" errors="1" skipped="1" time="0.20">
  <testcase classname="com.example.OrderControllerTest" name="passing" time="0.04"/>
  <testcase classname="com.example.OrderControllerTest" name="failing" time="0.04">
    <failure type="AssertionError" message="expected 5 was 4"/>
  </testcase>
  <testcase classname="com.example.OrderControllerTest" name="erroring" time="0.04">
    <error type="NullPointerException" message="boom"/>
  </testcase>
  <testcase classname="com.example.OrderControllerTest" name="skipped" time="0.04">
    <skipped/>
  </testcase>
</testsuite>
"""

PYTEST_WRAPPED_XML = """<?xml version="1.0" encoding="UTF-8"?>
<testsuites>
  <testsuite name="pytest" tests="2" failures="0" errors="0" skipped="0" time="0.01">
    <testcase classname="tests.test_foo" name="test_one"/>
    <testcase classname="tests.test_foo" name="test_two"/>
  </testsuite>
</testsuites>
"""

CHECKSTYLE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<checkstyle version="10.3.4">
  <file name="/workspace/src/main/java/com/example/UserController.java">
    <error line="20" column="5" severity="error" message="Missing a Javadoc comment."
           source="com.puppycrawl.tools.checkstyle.checks.javadoc.MissingJavadocMethodCheck"/>
    <error line="27" column="0" severity="warning" message="'100' is a magic number."
           source="com.puppycrawl.tools.checkstyle.checks.coding.MagicNumberCheck"/>
  </file>
  <file name="/workspace/src/main/java/com/example/UserRepository.java">
    <error line="14" severity="error" message="'50' is a magic number."
           source="com.puppycrawl.tools.checkstyle.checks.coding.MagicNumberCheck"/>
  </file>
</checkstyle>
"""

SARIF_RUFF = """{
  "version": "2.1.0",
  "runs": [
    {
      "tool": {"driver": {"name": "ruff", "rules": []}},
      "results": [
        {
          "ruleId": "F401",
          "level": "warning",
          "message": {"text": "`os` imported but unused"},
          "locations": [
            {
              "physicalLocation": {
                "artifactLocation": {"uri": "src/example.py"},
                "region": {"startLine": 3}
              }
            }
          ]
        },
        {
          "ruleId": "E501",
          "level": "error",
          "message": {"text": "Line too long (132 > 100 characters)"},
          "locations": [
            {
              "physicalLocation": {
                "artifactLocation": {"uri": "src/example.py"},
                "region": {"startLine": 42}
              }
            }
          ]
        }
      ]
    }
  ]
}
"""


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


# --- read_junit_xml ---


def test_read_junit_xml_aggregates_passing_suite(tmp_path: Path) -> None:
    _write(tmp_path / "target/surefire-reports/TEST-User.xml", SUREFIRE_PASS_XML)

    summary = read_junit_xml(["target/surefire-reports/TEST-*.xml"], tmp_path)

    assert summary.total == 3
    assert summary.passed == 3
    assert summary.failed == 0
    assert summary.skipped == 0
    assert summary.executed_tests == [
        "com.example.UserControllerTest.listReturnsFirstPage",
        "com.example.UserControllerTest.listReturnsSecondPage",
        "com.example.UserControllerTest.cursorPaginationReturnsFirstPage",
    ]
    assert summary.parse_errors == []


def test_read_junit_xml_counts_failures_and_skipped(tmp_path: Path) -> None:
    _write(tmp_path / "surefire/TEST-Order.xml", SUREFIRE_MIXED_XML)

    summary = read_junit_xml(["surefire/TEST-*.xml"], tmp_path)

    assert summary.total == 4
    assert summary.failed == 2
    assert summary.skipped == 1
    assert summary.passed == 1


def test_read_junit_xml_handles_testsuites_wrapper(tmp_path: Path) -> None:
    _write(tmp_path / "junit/results.xml", PYTEST_WRAPPED_XML)

    summary = read_junit_xml(["junit/*.xml"], tmp_path)

    assert summary.total == 2
    assert summary.passed == 2
    assert summary.executed_tests == [
        "tests.test_foo.test_one",
        "tests.test_foo.test_two",
    ]


def test_read_junit_xml_aggregates_across_files(tmp_path: Path) -> None:
    _write(tmp_path / "reports/TEST-A.xml", SUREFIRE_PASS_XML)
    _write(tmp_path / "reports/TEST-B.xml", SUREFIRE_MIXED_XML)

    summary = read_junit_xml(["reports/TEST-*.xml"], tmp_path)

    assert summary.total == 7
    assert summary.passed == 4
    assert summary.failed == 2
    assert summary.skipped == 1
    assert len(summary.parsed_files) == 2


def test_read_junit_xml_records_parse_errors(tmp_path: Path) -> None:
    _write(tmp_path / "reports/TEST-broken.xml", "<not-xml")

    summary = read_junit_xml(["reports/TEST-*.xml"], tmp_path)

    assert summary.total == 0
    assert summary.parsed_files == [str(tmp_path / "reports/TEST-broken.xml")]
    assert len(summary.parse_errors) == 1


def test_read_junit_xml_no_files_match(tmp_path: Path) -> None:
    summary = read_junit_xml(["does-not-exist/*.xml"], tmp_path)

    assert summary == JUnitSummary()


# --- read_checkstyle_xml ---


def test_read_checkstyle_xml_emits_findings(tmp_path: Path) -> None:
    _write(tmp_path / "target/checkstyle-result.xml", CHECKSTYLE_XML)

    findings = read_checkstyle_xml(
        ["target/checkstyle-result.xml"],
        tmp_path,
    )

    assert len(findings) == 3
    by_file = {(f["file"], f["line"]) for f in findings}
    assert (
        "/workspace/src/main/java/com/example/UserController.java",
        20,
    ) in by_file
    assert findings[0]["tool"] == "checkstyle"
    severities = {f["severity"] for f in findings}
    assert severities == {"error", "warn"}
    rules = {f["rule"] for f in findings}
    assert rules == {"MissingJavadocMethodCheck", "MagicNumberCheck"}


def test_read_checkstyle_xml_strips_repo_root_when_absolute(tmp_path: Path) -> None:
    # Re-anchor the absolute paths in the fixture to the tmp_path so the
    # repo-root prefix actually matches at parse time.
    rewritten = CHECKSTYLE_XML.replace("/workspace/", f"{tmp_path}/")
    _write(tmp_path / "target/checkstyle-result.xml", rewritten)

    findings = read_checkstyle_xml(
        ["target/checkstyle-result.xml"],
        tmp_path,
    )

    files = {f["file"] for f in findings}
    assert files == {
        "src/main/java/com/example/UserController.java",
        "src/main/java/com/example/UserRepository.java",
    }


def test_read_checkstyle_xml_no_files_match(tmp_path: Path) -> None:
    assert read_checkstyle_xml(["missing.xml"], tmp_path) == []


# --- read_sarif ---


def test_read_sarif_emits_findings_with_tool_name(tmp_path: Path) -> None:
    _write(tmp_path / ".darkfactory/ruff.sarif", SARIF_RUFF)

    findings = read_sarif([".darkfactory/*.sarif"], tmp_path)

    assert len(findings) == 2
    assert {f["tool"] for f in findings} == {"ruff"}
    assert {f["severity"] for f in findings} == {"warn", "error"}
    assert {f["rule"] for f in findings} == {"F401", "E501"}
    line_for_e501 = next(f for f in findings if f["rule"] == "E501")["line"]
    assert line_for_e501 == 42


def test_read_sarif_handles_malformed_json(tmp_path: Path) -> None:
    _write(tmp_path / "broken.sarif", "{this is not json}")
    assert read_sarif(["*.sarif"], tmp_path) == []


def test_read_sarif_handles_missing_locations(tmp_path: Path) -> None:
    payload = """{
      "version": "2.1.0",
      "runs": [
        {
          "tool": {"driver": {"name": "mypy"}},
          "results": [
            {"ruleId": "no-untyped-def", "level": "error",
             "message": {"text": "Function missing type annotation"}}
          ]
        }
      ]
    }
    """
    _write(tmp_path / "mypy.sarif", payload)

    findings = read_sarif(["mypy.sarif"], tmp_path)

    assert len(findings) == 1
    assert findings[0]["file"] == ""
    assert findings[0]["line"] == 0
    assert findings[0]["tool"] == "mypy"
