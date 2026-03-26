from typing import Literal

from pydantic import Field
from datetime import datetime

from pydantic import BaseModel

from backend.rules_store.watering import Schedule, Condition, Action



class RuleUpsert(BaseModel):
    enabled: bool = Field(
        default=True,
        description="Whether the rule is enabled"
    )
    paused: bool = Field(
        default=False,
        description="Whether the rule is paused"
    )
    schedule: Schedule = Field(description="Schedule for when the rule should be executed")
    conditions: list[Condition] = Field(
        default_factory=list,
        description="Conditions that must be met for the rule to be executed"
    )
    action: Action = Field(description="Action to be performed when the rule is executed")
    expected_version: int | None = Field(
        default=None,
        ge=1,
        description="Optional optimistic-lock version. If provided and mismatched, backend should return 409.",
    )


class RuleView(BaseModel):
    controller_id: str
    version: int
    enabled: bool
    paused: bool
    schedule: Schedule
    conditions: list[Condition]
    action: Action
    created_at: datetime | None
    updated_at: datetime | None


class PolicyFactView(BaseModel):
    field: str
    status: Literal["ok", "missing", "invalid", "unsupported"]
    message: str
    source_entity_ids: list[str] = Field(default_factory=list)
    selected_entity_id: str | None = None
    selected_state_raw: str | None = None
    selected_value: float | None = None
    comparison_passed: bool | None = None
    operator: str | None = None
    expected_value: int | float | str | None = None
    unit: str | None = None


class PolicyDecisionView(BaseModel):
    outcome: Literal["allow", "deny", "unknown"]
    reason_code: str
    message: str
    evaluated_at: datetime
    effective_seconds: int | None = None
    facts: list[PolicyFactView] = Field(default_factory=list)


class RulePreviewView(BaseModel):
    controller_id: str
    summary: str
    next_run_at: datetime | None = None
    decision: PolicyDecisionView
