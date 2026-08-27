from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from .auditor import CuratorAuditor
from .locking import AuditAlreadyRunningError, AuditLock
from .memory import CuratorMemoryError, CuratorMemoryStore
from .models import AuditFilter
from .observation_models import FAILED, SKIPPED_OVERLAP, SUCCEEDED
from .observation_runner import CuratorObservationRunner, ObservationRunnerError


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
    observe = commands.add_parser(
        "observe", help="Run one allowlisted read-only Curator observation"
    )
    observe.add_argument(
        "--job",
        required=True,
        choices=("health", "audit", "integrity", "progress-policy", "analytics"),
    )
    observe.add_argument("--repository", default=".")
    observe.add_argument("--results", default="curation_observations")
    observe.add_argument("--memory", default="curation_memory")
    observe.add_argument("--trigger", choices=("manual", "scheduled"), default="manual")
    observe.add_argument("--correlation-id", default="")
    refresh = commands.add_parser(
        "refresh-progress-verification",
        help="Run the single allowlisted Stage B progress-verification reconciliation",
    )
    refresh.add_argument("--repository", default=".")
    refresh.add_argument("--task-id")
    refresh.add_argument("--trigger", choices=("manual", "scheduled"), default="manual")
    refresh.add_argument("--correlation-id", default="")
    refresh.add_argument("--dry-run", action="store_true")
    terminal_refresh = commands.add_parser(
        "refresh-terminal-evidence-verification",
        help=(
            "Run the allowlisted Stage B terminal-evidence verification "
            "reconciliation"
        ),
    )
    terminal_refresh.add_argument("--repository", default=".")
    terminal_refresh.add_argument("--task-id")
    terminal_refresh.add_argument(
        "--trigger", choices=("manual", "scheduled"), default="manual"
    )
    terminal_refresh.add_argument("--correlation-id", default="")
    terminal_refresh.add_argument("--dry-run", action="store_true")
    evidence_sync = commands.add_parser(
        "sync-terminal-evidence",
        help=(
            "Run the allowlisted Stage B terminal-evidence current-evidence "
            "synchronization"
        ),
    )
    evidence_sync.add_argument("--repository", default=".")
    evidence_sync.add_argument("--task-id")
    evidence_sync.add_argument(
        "--trigger", choices=("manual", "scheduled"), default="manual"
    )
    evidence_sync.add_argument("--correlation-id", default="")
    evidence_sync.add_argument("--dry-run", action="store_true")
    convergence_refresh = commands.add_parser(
        "refresh-early-convergence-verification",
        help=(
            "Run the allowlisted Stage B early-convergence verification "
            "reconciliation"
        ),
    )
    convergence_refresh.add_argument("--repository", default=".")
    convergence_refresh.add_argument("--task-id")
    convergence_refresh.add_argument(
        "--trigger", choices=("manual", "scheduled"), default="manual"
    )
    convergence_refresh.add_argument("--correlation-id", default="")
    convergence_refresh.add_argument("--dry-run", action="store_true")
    signal_refresh = commands.add_parser(
        "refresh-signal-retention-verification",
        help=(
            "Run the allowlisted Stage B signal-retention verification "
            "reconciliation"
        ),
    )
    signal_refresh.add_argument("--repository", default=".")
    signal_refresh.add_argument("--task-id")
    signal_refresh.add_argument(
        "--trigger", choices=("manual", "scheduled"), default="manual"
    )
    signal_refresh.add_argument("--correlation-id", default="")
    signal_refresh.add_argument("--dry-run", action="store_true")
    scheduled_stage_b = commands.add_parser(
        "stage-b-scheduled",
        help="Run the code-allowlisted scheduled Stage B reconciliation set",
    )
    scheduled_stage_b.add_argument("--repository", default=".")
    scheduled_stage_b.add_argument("--dry-run", action="store_true")
    scheduled_stage_b.add_argument("--correlation-id", default="")
    scheduled_stage_b.add_argument("--max-candidates", type=int, default=5)
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    repository = Path(args.repository).resolve()
    memory_path = Path(getattr(args, "memory", "curation_memory"))
    memory_path = memory_path if memory_path.is_absolute() else repository / memory_path
    if args.command == "stage-b-scheduled":
        from curator.stage_b_scheduled_runner import (
            CuratorStageBScheduledRunner,
            StageBScheduledRunnerError,
        )
        from curator.stage_b_scheduled_repository import (
            StageBScheduledRunRepositoryError,
        )
        try:
            result = CuratorStageBScheduledRunner(repository).run(
                dry_run=args.dry_run,
                correlation_id=args.correlation_id,
                max_candidates=args.max_candidates,
            )
        except (
            CuratorMemoryError,
            StageBScheduledRunnerError,
            StageBScheduledRunRepositoryError,
        ) as error:
            print(json.dumps({"status": "FAILED", "error": str(error)}), file=sys.stderr)
            return 2
        print(json.dumps(asdict(result), sort_keys=True))
        return 2 if result.status in {"FAILED", "PARTIAL_FAILED"} else 0
    if args.command in {
        "refresh-progress-verification",
        "refresh-terminal-evidence-verification",
        "sync-terminal-evidence",
        "refresh-early-convergence-verification",
        "refresh-signal-retention-verification",
    }:
        from app.services.curator_stage_b_reconciliation_service import (
            CuratorEarlyConvergenceStageBReconciliationService,
            CuratorSignalRetentionStageBReconciliationService,
            CuratorStageBReconciliationService,
            CuratorTerminalEvidenceCurrentEvidenceSyncService,
            CuratorTerminalEvidenceStageBReconciliationService,
            StageBReconciliationError,
        )
        service_types = {
            "refresh-progress-verification": CuratorStageBReconciliationService,
            "refresh-terminal-evidence-verification": (
                CuratorTerminalEvidenceStageBReconciliationService
            ),
            "sync-terminal-evidence": CuratorTerminalEvidenceCurrentEvidenceSyncService,
            "refresh-early-convergence-verification": (
                CuratorEarlyConvergenceStageBReconciliationService
            ),
            "refresh-signal-retention-verification": (
                CuratorSignalRetentionStageBReconciliationService
            ),
        }
        service_type = service_types[args.command]
        try:
            result = service_type(repository).run(
                task_id=args.task_id,
                trigger_source=args.trigger,
                correlation_id=args.correlation_id,
                dry_run=args.dry_run,
            )
        except (StageBReconciliationError, CuratorMemoryError) as error:
            print(json.dumps({"status": "FAILED", "error": str(error)}), file=sys.stderr)
            return 2
        payload = asdict(result)
        print(json.dumps(payload, sort_keys=True))
        return 2 if any(
            item.status == "FAILED" for item in result.task_results
        ) else 0
    if args.command == "observe":
        results_path = Path(args.results)
        results_path = (
            results_path if results_path.is_absolute() else repository / results_path
        )
        try:
            result = CuratorObservationRunner(
                repository,
                results_root=results_path,
                memory_root=memory_path,
            ).run(
                args.job,
                trigger_source=args.trigger,
                scheduler_correlation_id=args.correlation_id,
            )
        except ObservationRunnerError as error:
            print(json.dumps({"status": "FAILED", "error": str(error)}), file=sys.stderr)
            return 2
        except Exception as error:
            print(json.dumps({
                "status": "FAILED",
                "error": f"Observation runner failed ({type(error).__name__}).",
            }), file=sys.stderr)
            return 2
        print(json.dumps({
            "status": result.status,
            "run_id": result.run_id,
            "job": result.job_type,
            "counts": dict(result.observation_counts),
            "warnings": list(result.warnings),
            "errors": list(result.errors),
        }, sort_keys=True))
        if result.status == SUCCEEDED:
            return 0
        if result.status == SKIPPED_OVERLAP:
            return 3
        if result.status == FAILED and any(
            "disabled" in item.casefold() for item in result.errors
        ):
            return 4
        return 2
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
