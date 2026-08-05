from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .auditor import CuratorAuditor
from .locking import AuditAlreadyRunningError, AuditLock
from .memory import CuratorMemoryError, CuratorMemoryStore
from .models import AuditFilter


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="python -m curator", description="Gnojo Curator read-only auditor")
    commands = root.add_subparsers(dest="command", required=True)
    audit = commands.add_parser("audit", help="Audit Gnojo content without modifying it")
    audit.add_argument("--repository", default=".")
    audit.add_argument("--output", default="curation_runs")
    audit.add_argument("--platform")
    audit.add_argument("--category")
    audit.add_argument("--content-type", choices=("workflow", "article", "command", "script"))
    audit.add_argument("--severity", choices=("critical", "high", "medium", "low", "info"))
    audit.add_argument("--changed-since", help="Recorded for forward compatibility; filtering is not yet implemented")
    audit.add_argument("--memory", default="curation_memory")
    tasks = commands.add_parser("tasks", help="Inspect or update persistent Knowledge Tasks")
    tasks.add_argument("action", choices=("list", "update"))
    tasks.add_argument("task_id", nargs="?")
    tasks.add_argument("--repository", default=".")
    tasks.add_argument("--memory", default="curation_memory")
    tasks.add_argument("--status", choices=("open", "in_progress", "resolved", "ignored", "superseded"))
    tasks.add_argument("--owner", choices=("Curator", "Researcher", "Workflow Designer", "Script Engineer", "QA Reviewer", "Human"))
    tasks.add_argument("--note", default="")
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    repository = Path(args.repository).resolve()
    memory_path = Path(args.memory)
    memory_path = memory_path if memory_path.is_absolute() else repository / memory_path
    if args.command == "tasks":
        store = CuratorMemoryStore(memory_path)
        try:
            if args.action == "list":
                tasks = sorted(store.load().get("tasks", {}).values(), key=lambda item: item["task_id"])
                print(json.dumps({"status": "completed", "tasks": tasks}, indent=2, sort_keys=True))
                return 0
            if not args.task_id:
                raise CuratorMemoryError("A task ID is required for the update action.")
            task = store.update_task(args.task_id, status=args.status, owner=args.owner, note=args.note)
            print(json.dumps({"status": "completed", "task": task}, indent=2, sort_keys=True))
            return 0
        except CuratorMemoryError as error:
            print(json.dumps({"status": "failed", "error": str(error)}), file=sys.stderr)
            return 2
    filters = AuditFilter(args.platform, args.category, args.content_type, args.severity, args.changed_since)
    try:
        with AuditLock(repository / ".curator-audit.lock"):
            result, location = CuratorAuditor(repository, args.output, args.memory).audit(filters)
    except AuditAlreadyRunningError as error:
        print(json.dumps({"status": "overlap", "error": str(error)}), file=sys.stderr)
        return 3
    except Exception as error:
        print(json.dumps({"status": "failed", "error": str(error), "type": type(error).__name__}), file=sys.stderr)
        return 2
    summary = result.summary()
    print(json.dumps({"status": "completed", "run_id": result.run_id, "output": str(location), "summary": summary}, sort_keys=True))
    blocking_defects = sum(
        1 for item in result.findings
        if item.classification == "defect" and item.severity in {"critical", "high"}
    )
    return 1 if blocking_defects else 0


if __name__ == "__main__":
    raise SystemExit(main())
