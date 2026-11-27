"""The task contract a caller submits, as HTTP and MCP both read it."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Workload = Literal["answer", "summarize", "build_content", "code_change"]


class BudgetLimits(BaseModel):
    wall_seconds: float | None = None
    model_calls: int | None = None
    model_tokens: int | None = None
    tool_calls: int | None = None
    bytes_scanned: int | None = None
    sandbox_seconds: float | None = None
    output_bytes: int | None = None
    spend_usd: float | None = None


class Destination(BaseModel):
    adapter: Literal["record", "file", "test_sink", "blind_sink"] = "record"
    target: str = ""
    expected_target_version: str | None = Field(
        None, description="Digest the target must still hold, or 'absent' to create only."
    )


class Criterion(BaseModel):
    """One observable acceptance check. Extra keys configure its grader."""

    model_config = ConfigDict(extra="allow")
    id: str
    type: str
    required: bool = True


class Edit(BaseModel):
    path: str
    content: str | None = None
    find: str | None = None
    replace: str | None = None
    delete: bool = False


class MediaInput(BaseModel):
    registration_id: str
    path: str
    region: dict[str, int] | None = None
    purpose: str = ""


class Fact(BaseModel):
    """A required fact: a label, and a pattern that finds its value in a source."""

    label: str
    pattern: str
    unit: str | None = None


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workload: Workload
    question: str = Field(..., min_length=1, max_length=8000)
    inputs: list[str] = Field(default_factory=list, description="Registration ids to work from.")
    fixtures: list[str] = Field(
        default_factory=list, description="Registrations only the verifier may read."
    )
    audience: str = ""
    purpose: str = ""
    output: dict[str, Any] = Field(default_factory=dict)
    acceptance: list[Criterion] = Field(default_factory=list)
    destination: Destination = Field(default_factory=Destination)
    budget: BudgetLimits = Field(default_factory=BudgetLimits)
    discovery: Literal["auto", "off"] = "auto"
    identifiers: list[str] = Field(default_factory=list, description="Exact literals to grep.")
    symbols: list[str] = Field(default_factory=list, description="Symbols to follow to callers.")
    media: list[MediaInput] = Field(default_factory=list)
    facts: list[Fact] = Field(default_factory=list)
    proposals: list[list[Edit]] = Field(
        default_factory=list,
        description="Candidate edit sets for a model-free code change, tried in order.",
    )
    test_command: list[str] = Field(default_factory=list)
    allowed_commands: list[str] = Field(default_factory=list)
    capabilities: list[str] = Field(default_factory=list)
    planner: Literal["auto", "rules", "model"] = "auto"
    max_repairs: int = Field(2, ge=0, le=6)
    context_chars: int = Field(24000, ge=200, le=400000)
    tenant: str | None = None


class StartRequest(BaseModel):
    request: RunRequest
    client_key: str = Field(..., min_length=1, max_length=200)


class ResumeRequest(BaseModel):
    input: dict[str, Any] = Field(default_factory=dict)


class RegisterRequest(BaseModel):
    uri: str
    resolver: Literal["git", "folder", "pheasant"] | None = None
    label: str | None = None
    scope: dict[str, Any] = Field(default_factory=dict)
    freshness_policy: dict[str, Any] = Field(default_factory=dict)
    operations: list[str] | None = None
    classification: str = "internal"


class GrepRequest(BaseModel):
    registration_id: str
    pattern: str
    revision: str | None = None
    options: dict[str, Any] = Field(default_factory=dict)
    cursor: str | None = None


class ReadRequest(BaseModel):
    registration_id: str
    path: str
    revision: str | None = None
    start_line: int | None = None
    end_line: int | None = None


class DiscoverRequest(BaseModel):
    question: str
    filters: dict[str, Any] = Field(default_factory=dict)
    cursor: str | None = None
    limit: int = Field(10, ge=1, le=50)
