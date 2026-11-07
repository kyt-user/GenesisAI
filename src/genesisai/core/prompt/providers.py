"""Ordered context contributions with explicit authority, provenance and budgets."""

from dataclasses import dataclass
from typing import Callable
from genesisai.core.prompt.compaction import estimate_tokens, fingerprint
from genesisai.shared.messages import Message


@dataclass(frozen=True)
class ContextBlock:
    source: str
    content: str
    scope: str = "session"
    role: str = "user"
    trust: str = "reference"
    token_budget: int = 6000
    required: bool = False


class ContextProviders:
    def __init__(self):
        self._providers: dict[str, tuple[int, Callable]] = {}

    def register(self, name, provider, *, priority=50):
        if not name or name in self._providers or not callable(provider):
            raise ValueError(
                "Context provider must have a unique name and callable implementation"
            )
        self._providers[name] = (priority, provider)

    def build(self, request):
        messages, manifest = [], []
        for name, (_, provider) in sorted(
            self._providers.items(), key=lambda item: (item[1][0], item[0])
        ):
            for block in provider(request):
                if not isinstance(block, ContextBlock) or block.role not in {
                    "system",
                    "user",
                }:
                    raise ValueError(f"Invalid context contribution: {name}")
                if block.role == "system" and block.trust != "policy":
                    raise ValueError(
                        "Retrieved reference material cannot become a system instruction"
                    )
                tokens = estimate_tokens(block.content)
                # Required contributions share the model's total input budget.
                # Their local sizing hint must not reject otherwise fitting tasks.
                included = block.required or tokens <= block.token_budget
                manifest.append(
                    dict(
                        provider=name,
                        source=block.source,
                        scope=block.scope,
                        role=block.role,
                        trust=block.trust,
                        tokens=tokens,
                        budget=None if block.required else block.token_budget,
                        included=included,
                        required=block.required,
                        hash=fingerprint(block.content),
                    )
                )
                if included and block.content:
                    messages.append(Message(role=block.role, content=block.content,
                                            metadata={'context_source': block.source, 'required': block.required}))
        return messages, manifest
