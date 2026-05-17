"""Shared route label normalization (no service imports)."""


def normalize_route_label(label: str) -> str:
    return " ".join(label.strip().lower().split())
