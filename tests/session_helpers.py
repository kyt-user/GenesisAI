"""Inspect persisted sessions without performing recovery during assertions."""

from genesisai.shared.paths import application_home
from genesisai.core.paths import ApplicationPaths
from genesisai.core.persistence.session import Session


def persisted_sessions(root=None):
    root = root or application_home()
    if not ApplicationPaths(root).database.exists():
        return []
    return [Session.inspect(root, row["id"])[0] for row in Session.list(root)]
