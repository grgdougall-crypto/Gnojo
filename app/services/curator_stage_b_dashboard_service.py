from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from curator.reconciliation import (
    StageBJournalError,
    StageBJournalEvent,
    StageBJournalRepository,
)


class CuratorStageBDashboardService:
    """Project persisted Stage B journal evidence without executing reconciliation."""

    CAPABILITIES = (
        (
            "cur-wr-progress-verification-refresh",
            1,
            "Progress Verification Refresh",
        ),
        (
            "cur-wr-terminal-evidence-verification-refresh",
            1,
            "Terminal Evidence Verification Refresh",
        ),
        (
            "cur-wr-terminal-evidence-current-evidence-sync",
            1,
            "Terminal Evidence Synchronization",
        ),
        (
            "cur-wr-early-convergence-verification-refresh",
            1,
            "Early Convergence Verification Refresh",
        ),
        (
            "cur-wr-signal-retention-verification-refresh",
            1,
            "Signal Retention Verification Refresh",
        ),
    )

    def __init__(self, repository_root: Path):
        self.repository = StageBJournalRepository(
            Path(repository_root).resolve() / "curation_memory"
        )

    def project(self, *, controls: dict[str, Any] | None = None) -> dict[str, Any]:
        control_projection = self._controls(controls or {})
        try:
            events = self._events()
        except (StageBJournalError, OSError) as error:
            return {
                "journal_status": "CORRUPT_BLOCKED",
                "journal_status_label": "Corrupt / Blocked",
                "journal_error": str(error),
                "counts": self._counts(()),
                "capabilities": [
                    self._capability(capability, (), journal_blocked=True)
                    for capability in self.CAPABILITIES
                ],
                "controls": control_projection,
            }

        incomplete = self._incomplete(events)
        status = "INCOMPLETE" if incomplete else "HEALTHY"
        return {
            "journal_status": status,
            "journal_status_label": status.title(),
            "journal_error": "",
            "counts": self._counts(events),
            "capabilities": [
                self._capability(capability, events)
                for capability in self.CAPABILITIES
            ],
            "controls": control_projection,
        }

    def _events(self) -> tuple[StageBJournalEvent, ...]:
        self.repository.validate_all()
        if not self.repository.root.exists():
            return ()
        result: list[StageBJournalEvent] = []
        for directory in sorted(self.repository.root.iterdir()):
            result.extend(self.repository.get(directory.name))
        return tuple(sorted(result, key=self._event_order, reverse=True))

    def _capability(
        self,
        capability: tuple[str, int, str],
        events: tuple[StageBJournalEvent, ...],
        *,
        journal_blocked: bool = False,
    ) -> dict[str, Any]:
        capability_id, version, label = capability
        matching = tuple(
            event for event in events
            if event.capability_id == capability_id
            and event.capability_version == version
        )
        incomplete = self._incomplete(matching)
        committed = tuple(event for event in matching if event.status == "COMMITTED")
        failed = tuple(event for event in matching if event.status == "FAILED")
        skipped = tuple(event for event in matching if event.status == "SKIPPED")
        if journal_blocked or incomplete:
            acceptance = "INCOMPLETE"
        elif committed:
            acceptance = "COMMITTED_ACCEPTANCE"
        elif failed:
            acceptance = "FAILED_ONLY"
        else:
            acceptance = "NO_COMMITTED_ACCEPTANCE"
        return {
            "capability_id": capability_id,
            "capability_version": version,
            "label": label,
            "acceptance": acceptance,
            "acceptance_label": acceptance.replace("_", " ").title(),
            "has_committed_acceptance": bool(committed) and not journal_blocked,
            "latest": self._summary(matching[0] if matching else None),
            "latest_committed": self._summary(committed[0] if committed else None),
            "latest_failed": self._summary(failed[0] if failed else None),
            "latest_skipped": self._summary(skipped[0] if skipped else None),
            "incomplete_prepared": [self._summary(event) for event in incomplete],
        }

    @staticmethod
    def _incomplete(
        events: tuple[StageBJournalEvent, ...],
    ) -> tuple[StageBJournalEvent, ...]:
        latest_by_key: dict[str, StageBJournalEvent] = {}
        for event in events:
            current = latest_by_key.get(event.idempotency_key)
            if current is None or event.revision > current.revision:
                latest_by_key[event.idempotency_key] = event
        return tuple(
            sorted(
                (event for event in latest_by_key.values() if event.status == "PREPARED"),
                key=CuratorStageBDashboardService._event_order,
                reverse=True,
            )
        )

    @staticmethod
    def _summary(event: StageBJournalEvent | None) -> dict[str, Any] | None:
        if event is None:
            return None
        return {
            "status": event.status,
            "at": event.at,
            "task_id": event.task_id,
            "finding_id": event.finding_id,
            "run_id": event.run_id,
            "correlation_id": event.correlation_id,
            "reason": event.reason,
        }

    @staticmethod
    def _counts(events: tuple[StageBJournalEvent, ...]) -> dict[str, int]:
        return {
            "incomplete_prepared": len(
                CuratorStageBDashboardService._incomplete(events)
            ),
            "committed": sum(event.status == "COMMITTED" for event in events),
            "failed": sum(event.status == "FAILED" for event in events),
            "skipped": sum(event.status == "SKIPPED" for event in events),
        }

    @staticmethod
    def _controls(controls: dict[str, Any]) -> dict[str, Any]:
        return {
            "global_disabled": bool(controls.get("global_disabled")),
            "scheduled_runs_disabled": bool(
                controls.get("scheduled_runs_disabled", True)
            ),
            "stage_b_scheduling_configured": False,
            "stage_b_scheduling_message": (
                "Stage B scheduled execution is not configured."
            ),
        }

    @staticmethod
    def _event_order(event: StageBJournalEvent) -> tuple[datetime, str]:
        try:
            parsed = datetime.fromisoformat(event.at.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            parsed = parsed.astimezone(timezone.utc)
        except ValueError:
            parsed = datetime.min.replace(tzinfo=timezone.utc)
        return parsed, event.event_id
