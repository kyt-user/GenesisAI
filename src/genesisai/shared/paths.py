"""Application locations shared by model configuration and runtime layers."""

import os
from pathlib import Path


def application_home() -> Path:
    return (
        Path(os.environ.get("GENESISAI_HOME") or Path.home() / ".genesisai")
        .expanduser()
        .resolve()
    )


def default_workspace() -> Path:
    return (Path.home() / "GenesisAI" / "workspace").resolve()
