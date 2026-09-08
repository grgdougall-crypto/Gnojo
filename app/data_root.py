"""Canonical resolution for Gnojo's runtime-mutable repository tree."""

from __future__ import annotations

import os
from pathlib import Path


APPLICATION_ROOT = Path(__file__).resolve().parent.parent


def resolve_data_root(
    explicit_root: str | Path | None = None,
    *,
    legacy_root: str | Path | None = None,
) -> Path:
    """Resolve the repository-shaped root for runtime-mutable data."""
    if explicit_root is not None and str(explicit_root).strip():
        return Path(explicit_root).expanduser().resolve()
    configured = str(os.getenv("GNOJO_DATA_ROOT") or "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return Path(legacy_root or APPLICATION_ROOT).expanduser().resolve()


def resolve_application_root(repository_root: str | Path | None = None) -> Path:
    """Keep immutable repository assets in source unless a custom root was explicit."""
    if repository_root is None:
        return APPLICATION_ROOT
    candidate = Path(repository_root).expanduser().resolve()
    configured = str(os.getenv("GNOJO_DATA_ROOT") or "").strip()
    if configured and candidate == Path(configured).expanduser().resolve():
        return APPLICATION_ROOT
    return candidate


def resolve_data_path(
    *parts: str,
    explicit_path: str | Path | None = None,
    legacy_path: str | Path,
) -> Path:
    """Resolve one mutable path, preserving explicit and legacy precedence."""
    if explicit_path is not None and str(explicit_path).strip():
        return Path(explicit_path)
    configured = str(os.getenv("GNOJO_DATA_ROOT") or "").strip()
    if not configured:
        return Path(legacy_path)
    root = resolve_data_root()
    path = root.joinpath(*parts).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValueError("Runtime data path escapes GNOJO_DATA_ROOT.") from exc
    return path
