"""Application-owned paths, independent of the current working directory."""

from dataclasses import dataclass
from pathlib import Path
from genesisai.shared.paths import application_home


@dataclass(frozen=True)
class ApplicationPaths:
    root: Path

    @classmethod
    def default(cls):
        return cls(application_home())

    @property
    def database(self):
        return self.root / "state" / "genesis.sqlite3"

    def project(self, project_id: str) -> Path:
        if (
            not project_id.startswith("project_")
            or not project_id.replace("_", "").isalnum()
        ):
            raise ValueError("Invalid project identity")
        return self.root / "projects" / project_id
