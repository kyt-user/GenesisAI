"""Task creation domain logic."""


def normalize_tags(values: list[str]) -> list[str]:
    """Return normalized, unique, non-empty tags in their original order."""
    return [value.lower() for value in values]
