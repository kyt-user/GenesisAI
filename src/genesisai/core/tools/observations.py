"""Typed observations let tools contribute facts without knowing memory storage."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Observation:
    kind: str
    path: str | None = None
    sha256: str | None = None
    passed: bool | None = None
    detail: str = ""

    def validate(self):
        if self.kind in {"resource_read", "resource_changed"}:
            if not self.path:
                raise ValueError("Resource observations require a path")
        elif self.kind == "verification":
            if type(self.passed) is not bool:
                raise ValueError("Verification observations require a boolean result")
        else:
            raise ValueError("Unknown observation kind")


class ObservationRegistry:
    """Adapters are registered alongside a tool, never dispatched by memory code."""

    def __init__(self):
        self.adapters = {}

    def register(self, name, adapter):
        if name in self.adapters:
            raise ValueError("Duplicate observation adapter: " + name)
        self.adapters[name] = adapter

    def extract(self, name, args, result):
        from dataclasses import asdict

        values = []
        if result.get("ok") and name in self.adapters:
            for item in self.adapters[name](args, result["data"]):
                item.validate()
                values.append(asdict(item))
        return values
