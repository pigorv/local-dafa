from __future__ import annotations

import pytest
from pydantic import ValidationError

from darkfactory.state import (
    ContractChanges,
    ImplementationBrief,
    VerificationPredicate,
    WorkPackage,
    work_package_dict_from_model,
)


def _work_package_payload() -> dict:
    return {
        "id": "WP-1",
        "story_id": "US-1",
        "title": "Add cursor pagination",
        "intent": "Return stable cursor pages for active users.",
        "verification": [
            "First page includes a next cursor when more active users exist.",
            "Final page omits the next cursor.",
        ],
        "repo_areas": ["Backend user lookup flow", "API error mapping"],
        "candidate_files": ["src/users/api.py", "tests/test_users_api.py"],
        "dependencies": [],
        "estimated_scope": "small",
        "notes": ["Keep existing offset pagination response fields for now."],
    }


def _brief_payload() -> dict:
    return {
        "rev": 1,
        "problem": "Users need stable pagination for large active-user lists.",
        "expected_behavior": [
            "Active users can be fetched page by page with a cursor.",
            "Existing offset pagination clients continue to receive known fields.",
        ],
        "current_understanding": "The API currently accepts limit and offset.",
        "proposed_design": "Add cursor parsing near the existing user lookup flow.",
        "contract_changes": {
            "api": ["Add optional cursor query parameter."],
            "data": [],
            "events": [],
        },
        "compatibility_risks": [
            "Clients may depend on the exact pagination metadata shape."
        ],
        "open_assumptions": ["Cursor ordering can reuse created_at and id."],
        "test_strategy": "Cover first, middle, and final cursor pages.",
        "work_packages": [_work_package_payload()],
    }


def test_implementation_brief_round_trips_target_shape():
    payload = _brief_payload()

    brief = ImplementationBrief.model_validate(payload)

    assert isinstance(brief.contract_changes, ContractChanges)
    assert isinstance(brief.work_packages[0], WorkPackage)
    assert all(
        isinstance(predicate, VerificationPredicate)
        for predicate in brief.work_packages[0].verification
    )
    assert brief.model_dump() == payload


@pytest.mark.parametrize(
    "field",
    [
        "problem",
        "expected_behavior",
        "current_understanding",
        "proposed_design",
        "contract_changes",
        "compatibility_risks",
        "open_assumptions",
        "test_strategy",
        "work_packages",
    ],
)
def test_implementation_brief_requires_target_shape_fields(field):
    payload = _brief_payload()
    del payload[field]

    with pytest.raises(ValidationError):
        ImplementationBrief.model_validate(payload)


@pytest.mark.parametrize(
    "field",
    [
        "id",
        "story_id",
        "title",
        "intent",
        "verification",
        "repo_areas",
        "candidate_files",
        "dependencies",
        "estimated_scope",
        "notes",
    ],
)
def test_work_package_requires_target_shape_fields(field):
    payload = _work_package_payload()
    del payload[field]

    with pytest.raises(ValidationError):
        WorkPackage.model_validate(payload)


@pytest.mark.parametrize("field", ["api", "data", "events"])
def test_contract_changes_requires_target_shape_fields(field):
    payload = {"api": [], "data": [], "events": []}
    del payload[field]

    with pytest.raises(ValidationError):
        ContractChanges.model_validate(payload)


def test_work_package_model_maps_to_state_dict_shape():
    work_package = WorkPackage.model_validate(
        {
            **_work_package_payload(),
            "id": "WP-1",
            "story_id": "US-1",
            "dependencies": ["WP-0"],
        }
    )

    work_package_dict = work_package_dict_from_model(work_package)

    assert work_package_dict["story_id"] == "WP-1"
    assert work_package_dict["approach"] == work_package.intent
    assert work_package_dict["affected_files"] == work_package.candidate_files
    assert work_package_dict["new_files"] == []
    assert work_package_dict["test_files"] == []
    assert work_package_dict["depends_on"] == ["WP-0"]
    assert work_package_dict["verification"] == [
        "First page includes a next cursor when more active users exist.",
        "Final page omits the next cursor.",
    ]


