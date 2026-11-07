"""Typed session aggregate. Run-specific policy state belongs to the agent only."""

from dataclasses import dataclass, field, asdict, fields, replace
from genesisai.shared.budgets import initial_run_runtime


@dataclass
class SessionRecord:
    id: str
    workspace: str
    project_id: str
    workspace_id: str
    status: str = "idle"
    messages: list = field(default_factory=list)
    calls: dict = field(default_factory=dict)
    job_receipts: dict = field(default_factory=dict)
    authorizations: list = field(default_factory=list)
    sources: dict = field(default_factory=dict)
    artifacts: dict = field(default_factory=dict)
    pending: list = field(default_factory=list)
    grants: list = field(default_factory=list)
    output: str = ""
    run_id: str | None = None
    rounds: int = 0
    tool_count: int = 0
    failures: int = 0
    deadline: float = 0
    usage: dict = field(default_factory=dict)
    history: list = field(default_factory=list)
    changes: list = field(default_factory=list)
    permission_mode: str = "user"
    tool_runtime: dict = field(default_factory=lambda: {"active_skills": {}})
    run_runtime: dict = field(default_factory=initial_run_runtime)
    context_checkpoint: dict | None = None
    context_estimate_factor: float = 1.0
    context_model: str | None = None
    last_run_outcome: dict | None = None
    mcp_tools: dict = field(default_factory=dict)
    answer: str = ""
    limits: dict = field(default_factory=dict)
    cancel_requested: bool = False

    def snapshot(self) -> dict:
        return asdict(self)

    def apply(self, **changes):
        """Apply agent-owned state fields, rejecting undeclared extension fields."""
        allowed = {f.name for f in fields(self)}
        if changes.keys() - allowed:
            raise ValueError(
                "Undeclared session fields: " + ", ".join(changes.keys() - allowed)
            )
        for name, value in changes.items():
            setattr(self, name, value)

    def evolve(self, **changes):
        return replace(self, **changes)
