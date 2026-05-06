from __future__ import annotations

import json

from darkfactory.runtime.approval import (
    ApprovalSignal,
    clear_authorization_cache,
    is_authorized,
    parse_command,
)


def test_parse_approve_command_with_mention() -> None:
    signal = parse_command(
        "  @darkfactory /df approve\nLGTM",
        author="octocat",
        comment_id=123,
        created_at="2026-05-06T10:00:00Z",
    )

    assert signal == ApprovalSignal(
        kind="Approve",
        author="octocat",
        comment_id=123,
        text="LGTM",
        created_at="2026-05-06T10:00:00Z",
    )


def test_parse_revise_multiline_feedback() -> None:
    signal = parse_command(
        "/df revise include the export edge case\nAlso cover empty results.",
        author="maintainer",
        comment_id=7,
    )

    assert signal is not None
    assert signal.kind == "Revise"
    assert signal.text == "include the export edge case\nAlso cover empty results."


def test_parse_reject_and_cancel() -> None:
    reject = parse_command("/df reject wrong product area", author="octocat")
    cancel = parse_command("/df cancel", author="octocat")

    assert reject is not None
    assert reject.kind == "Reject"
    assert reject.text == "wrong product area"
    assert cancel is not None
    assert cancel.kind == "Cancel"


def test_parse_ignores_malformed_commands() -> None:
    assert parse_command("/df") is None
    assert parse_command("/df revise") is None
    assert parse_command("Looks good\n/df approve") is None


def test_authorization_accepts_write_permissions() -> None:
    clear_authorization_cache()

    def runner(argv: list[str]) -> str:
        assert argv == [
            "gh",
            "api",
            "repos/octo/demo/collaborators/octocat/permission",
        ]
        return json.dumps({"permission": "write"})

    assert is_authorized("octocat", "octo/demo", runner=runner) is True


def test_authorization_rejects_read_permissions() -> None:
    def runner(_argv: list[str]) -> str:
        return json.dumps({"permission": "read"})

    assert is_authorized("reader", "octo/demo", runner=runner) is False
