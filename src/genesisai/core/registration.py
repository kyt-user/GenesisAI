"""Explicit registration surface for installed tools, context providers and parsers."""

from importlib.metadata import entry_points


def register_installed_extensions(runtime):
    """Installed packages opt in with the genesisai.extensions entry-point group."""
    for entry in sorted(
        entry_points(group="genesisai.extensions"), key=lambda item: item.name
    ):
        register = entry.load()
        if not callable(register):
            raise TypeError(
                f"Extension {entry.name} must export a register(runtime) callable"
            )
        register(runtime)
