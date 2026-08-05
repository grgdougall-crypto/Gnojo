from __future__ import annotations

import os
from pathlib import Path


class AuditAlreadyRunningError(RuntimeError):
    pass


class AuditLock:
    def __init__(self, path: Path):
        self.path = path
        self.acquired = False

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as error:
            raise AuditAlreadyRunningError(f"Curator audit lock already exists: {self.path}") from error
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(str(os.getpid()))
        self.acquired = True
        return self

    def __exit__(self, exc_type, exc, traceback):
        if self.acquired:
            self.path.unlink(missing_ok=True)
        self.acquired = False
