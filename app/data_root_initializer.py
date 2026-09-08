"""One-time initialization for a configured Gnojo production data root."""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Mapping

from app.data_root import APPLICATION_ROOT


class DataRootInitializationError(RuntimeError):
    """Raised when a production data root cannot be initialized safely."""


BASELINE_DIRECTORIES = (
    Path("app/decision_trees"),
    Path("knowledge_base/published"),
    Path("knowledge_base/commands"),
    Path("knowledge_base/scripts"),
)

BASELINE_FILES = (
    Path("knowledge_base/aliases.json"),
    Path("knowledge_base/inventory.json"),
)

EMPTY_RUNTIME_DIRECTORIES = (
    Path("app/workflow_drafts"),
    Path("app/workflow_publications"),
    Path("app/device_profiles"),
    Path("app/troubleshooting_history"),
    Path("knowledge_base/drafts"),
    Path("knowledge_base/archive"),
    Path("knowledge_base/deleted"),
)

PREINITIALIZATION_SCAFFOLD_DIRECTORIES = (
    Path("lost+found"),
    Path("knowledge_base/published"),
    *EMPTY_RUNTIME_DIRECTORIES,
)


def initialize_data_root(
    *,
    source_root: str | Path | None = None,
    environment: Mapping[str, str] | None = None,
) -> dict[str, object]:
    """Copy the trusted baseline into one empty configured data root."""
    environment = environment if environment is not None else os.environ
    configured = str(environment.get("GNOJO_DATA_ROOT") or "").strip()
    if not configured:
        raise DataRootInitializationError(
            "GNOJO_DATA_ROOT must be set before initialization."
        )

    source = Path(source_root or APPLICATION_ROOT).expanduser().resolve()
    target = Path(configured).expanduser().resolve()
    if target == source or source in target.parents:
        raise DataRootInitializationError(
            "GNOJO_DATA_ROOT must be outside the source repository."
        )

    _validate_baseline(source)
    _require_empty_target(target)

    target.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    for relative in BASELINE_DIRECTORIES:
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source / relative, destination, dirs_exist_ok=True)
        copied.append(relative.as_posix())
    for relative in BASELINE_FILES:
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / relative, destination)
        copied.append(relative.as_posix())
    for relative in EMPTY_RUNTIME_DIRECTORIES:
        (target / relative).mkdir(parents=True, exist_ok=True)

    return {
        "status": "INITIALIZED",
        "data_root": str(target),
        "copied": copied,
        "created_empty_directories": [
            relative.as_posix() for relative in EMPTY_RUNTIME_DIRECTORIES
        ],
    }


def _validate_baseline(source: Path) -> None:
    if not source.is_dir():
        raise DataRootInitializationError(
            f"Baseline source repository is unavailable: {source}"
        )
    for relative in BASELINE_DIRECTORIES:
        path = source / relative
        if not path.is_dir() or not any(item.is_file() for item in path.rglob("*")):
            raise DataRootInitializationError(
                f"Required baseline directory is missing or empty: {relative.as_posix()}"
            )
    for relative in BASELINE_FILES:
        path = source / relative
        if not path.is_file():
            raise DataRootInitializationError(
                f"Required baseline file is missing: {relative.as_posix()}"
            )


def _require_empty_target(target: Path) -> None:
    if not target.exists():
        return
    if not target.is_dir():
        raise DataRootInitializationError(
            "GNOJO_DATA_ROOT exists but is not a directory."
        )
    allowed_directories = _allowed_preinitialization_directories()
    try:
        _validate_preinitialization_tree(target, target, allowed_directories)
    except OSError as error:
        raise DataRootInitializationError(
            f"GNOJO_DATA_ROOT cannot be inspected: {error}"
        ) from error


def _validate_preinitialization_tree(
    target: Path,
    directory: Path,
    allowed_directories: set[Path],
) -> None:
    with os.scandir(directory) as entries:
        for entry in entries:
            path = Path(entry.path)
            relative = path.relative_to(target)
            if (
                entry.is_symlink()
                or not entry.is_dir(follow_symlinks=False)
                or relative not in allowed_directories
            ):
                raise DataRootInitializationError(
                    "GNOJO_DATA_ROOT is already populated; initialization refused."
                )
            _validate_preinitialization_tree(target, path, allowed_directories)


def _allowed_preinitialization_directories() -> set[Path]:
    allowed: set[Path] = set()
    for directory in PREINITIALIZATION_SCAFFOLD_DIRECTORIES:
        allowed.add(directory)
        allowed.update(parent for parent in directory.parents if parent != Path("."))
    return allowed
