from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.data_root import resolve_data_root
from app.services.autonomous_growth_service import AutonomousGrowthService, SUPPORTED_GAP_TYPES
from app.services.campaign_learning_draft_preparation_service import (
    CampaignLearningDraftPreparationService,
)
from app.services.knowledge_source_research_service import KnowledgeSourceResearchService
from app.services.supervised_campaign_autopilot_service import (
    SupervisedCampaignAutopilotError,
    SupervisedCampaignAutopilotService,
)


class BatchPropagationAutopilotError(ValueError):
    pass


class BatchPropagationAutopilotService:
    """Bounded coordinator over existing supervised Growth authorities."""

    POLICY_ID = "supervised-batch-propagation-v1"
    POLICY_VERSION = 1
    MIN_LIMIT = 1
    MAX_LIMIT = 10
    MAX_TRANSITIONS = 3
    MAX_AI_RETRIES = 2
    STATES = {
        "READY_FOR_FINAL_REVIEW", "HUMAN_EXCEPTION", "BLOCKED",
        "MACHINE_COMPLETE", "FAILED_SAFE",
    }

    def __init__(self, repository_root: Path | None = None, *, growth=None,
                 learning=None, research=None, source_autopilot=None, now=None):
        self.root = resolve_data_root(
            repository_root, legacy_root=Path(__file__).resolve().parents[2]
        )
        self.campaign_root = self.root / "knowledge_campaigns"
        self.batch_root = self.campaign_root / "propagation_batches"
        self.growth = growth or AutonomousGrowthService(
            self.root, self.campaign_root, max_transitions=self.MAX_TRANSITIONS,
            max_external_operations=1,
        )
        self.learning = learning or CampaignLearningDraftPreparationService(
            self.root, self.campaign_root
        )
        self.research = research or KnowledgeSourceResearchService(
            self.root, self.campaign_root
        )
        self.source_autopilot = source_autopilot or SupervisedCampaignAutopilotService(
            self.research
        )
        self.now = now or (lambda: datetime.now(timezone.utc).isoformat())

    def preview(self, *, domain: str, limit: int) -> dict[str, Any]:
        resolved, selected = self._selection(domain, limit)
        items = []
        for position, candidate in enumerate(selected, 1):
            outcome = self.growth.prepare_ranked_candidate(candidate, preview=True).as_dict()
            items.append(self._preview_item(position, candidate, outcome))
        return self._projection({
            "batch_id": None, "status": "PREVIEW", "preview": True,
            "domain": resolved, "requested_limit": int(limit),
            "selected_count": len(items), "items": items,
            "created_at": None, "updated_at": None, "history": [],
        })

    def operations(self, *, domain: str, limit: int = 5) -> dict[str, Any]:
        """Read-only launch projection over authoritative current coverage."""
        resolved, selected = self._selection(domain, limit)
        candidates = self.growth.ranked_candidates(resolved["id"])
        supported = [item for item in candidates
                     if item.get("gap_type") in SUPPORTED_GAP_TYPES]
        counts = {name: 0 for name in sorted(SUPPORTED_GAP_TYPES)}
        for item in supported:
            counts[item["gap_type"]] += 1
        capability_summary = {}
        if hasattr(self.growth.planner, "assess_domain"):
            assessment = self.growth.planner.assess_domain(resolved["id"])
            capability_summary = deepcopy(assessment.get("capability_summary") or {})
        if capability_summary.get("total"):
            catalog_counts = capability_summary.get("gap_counts") or {}
            stage2_counts = {name: 0 for name in counts}
            for item in supported:
                if not item.get("capability_id"):
                    stage2_counts[item["gap_type"]] += 1
            counts = {
                name: int(catalog_counts.get(name, 0)) + stage2_counts[name]
                for name in counts
            }
        batches = []
        if self.batch_root.exists():
            batches = [self._validated_record(self._read(path), path.stem)
                       for path in self.batch_root.glob("KPB-*.json")]
            batches = [item for item in batches
                       if item.get("domain", {}).get("id") == resolved["id"]]
        current = (self._projection(sorted(
            batches, key=lambda item: item.get("updated_at", "")
        )[-1]) if batches else None)
        return {
            "domains": [{"id": item["id"], "title": item["title"]}
                        for item in self.growth.planner.domains()],
            "domain": resolved, "limit": int(limit),
            "total_opportunities": sum(counts.values()), "counts_by_gap_type": counts,
            "capability_coverage": capability_summary,
            "next_candidates": [self._item_base(item) for item in selected],
            "current_batch": current, "read_only": True,
        }

    def run(self, *, domain: str | None = None, limit: int = 5,
            batch_id: str | None = None, actor: str = "Human") -> dict[str, Any]:
        with self._lock(self.campaign_root / ".batch-propagation.lock"):
            if batch_id:
                record = self._validated_record(self._read(self._path(batch_id)), batch_id)
            else:
                resolved, selected = self._selection(str(domain or ""), limit)
                record = self._find_resumable(resolved["id"], int(limit), selected)
                if record is None:
                    if not selected:
                        raise BatchPropagationAutopilotError(
                            "No supported propagation opportunity is currently available."
                        )
                    record = self._new_record(resolved, int(limit), selected, actor)
                    self._save(record)
                elif record.get("status") == "COMPLETE":
                    return self._projection(record)

            for selected in record.get("selection", []):
                identity = selected["gap_identity"]
                prior = next((item for item in record.get("items", [])
                              if item.get("gap_identity") == identity), None)
                try:
                    item = self._process(selected, prior)
                except Exception as error:
                    item = self._item_base(selected)
                    item.update(state="FAILED_SAFE", reason=f"{type(error).__name__}: {error}",
                                next_human_action="Inspect the failed-safe batch item.")
                record.setdefault("items", [])
                record["items"] = [item if value.get("gap_identity") == identity else value
                                   for value in record["items"]]
                if not any(value.get("gap_identity") == identity for value in record["items"]):
                    record["items"].append(item)
                record["updated_at"] = self.now()
                self._save(record)
            record["status"] = self._batch_status(record["items"])
            record["updated_at"] = self.now()
            record.setdefault("history", []).append({
                "event": "batch_pass_completed", "at": record["updated_at"],
                "actor": actor, "states": self._counts(record["items"]),
            })
            self._save(record)
            return self._projection(record)

    def get(self, batch_id: str) -> dict[str, Any]:
        return self._projection(self._validated_record(
            self._read(self._path(batch_id)), batch_id
        ))

    def _process(self, candidate, prior):
        attempts = []
        outcome = None
        for attempt in range(self.MAX_AI_RETRIES + 1):
            outcome = self.growth.prepare_ranked_candidate(candidate, preview=False).as_dict()
            reason = str((outcome.get("preparation") or {}).get("reason") or "")
            attempts.append({"attempt": attempt + 1, "status": outcome["status"],
                             "at": self.now(), "reason": reason})
            if outcome["status"] != "BLOCKED" or not self._retryable(reason):
                break
        item = self._item_base(candidate)
        item["attempts"] = attempts
        item["campaign"] = deepcopy(outcome.get("campaign") or {})
        item["what_completed"] = [
            value.get("action") for value in (outcome.get("preparation") or {}).get("artifacts", [])
            if value.get("action") and value.get("status") in {"completed", "package_reused"}
        ]
        item["reused"] = (outcome.get("campaign") or {}).get("disposition") in {
            "reuse", "reconciled", "completed_equivalent",
        }
        if outcome["status"] == "BLOCKED":
            preparation = outcome.get("preparation") or {}
            if (
                candidate.get("gap_type") == "missing_article"
                and preparation.get("blocker_stage") == "insufficient_evidence"
            ):
                item.update(
                    state="HUMAN_EXCEPTION",
                    reason=preparation.get(
                        "reason", "Authoritative evidence is insufficient."
                    ),
                    next_human_action="Review Evidence",
                    review_url=preparation.get("review_link") or "",
                    progress_summary="Evidence prepared",
                )
            else:
                item.update(state="BLOCKED", reason=preparation.get(
                    "reason", "Existing governed preparation is blocked."),
                    next_human_action="Resolve the authoritative campaign blocker.")
            return item
        if outcome["status"] == "NO-OP" and not (outcome.get("human_review") or {}).get("required"):
            item.update(state="MACHINE_COMPLETE", reason="Equivalent governed work is complete.",
                        next_human_action="")
            return item

        campaign = outcome.get("campaign") or {}
        human = outcome.get("human_review") or {}
        if candidate.get("gap_type") == "weak_learning_coverage" and campaign.get("campaign_id"):
            return self._process_learning(item, campaign, human)
        if human.get("required"):
            item = self._classify_human_gate(item, campaign, human)
            if candidate.get("gap_type") == "missing_article":
                item["progress_summary"] = self._article_progress_summary(
                    str(human.get("action") or "")
                )
            return item
        item.update(state="MACHINE_COMPLETE", reason="Allowlisted machine preparation completed.",
                    next_human_action="")
        return item

    @staticmethod
    def _article_progress_summary(action):
        if action == "approve_source":
            return "Source review required"
        if action in {"review_evidence", "review_claims"}:
            return "Evidence prepared"
        if action == "review_article_draft":
            return "Article draft ready for review"
        return "Researching sources"

    def _process_learning(self, item, campaign, human):
        ids = (campaign.get("campaign_id"), campaign.get("orchestration_id"),
               campaign.get("work_item_id"))
        if not all(ids):
            item.update(state="BLOCKED", reason="Learning campaign identity is incomplete.",
                        next_human_action="Inspect the campaign identity blocker.")
            return item
        result = None
        attempts = []
        for attempt in range(self.MAX_AI_RETRIES + 1):
            preview = self.learning.preview(*ids)
            if not preview.get("eligible_count"):
                result = preview
                break
            result = self.learning.prepare(*ids, actor="Batch Propagation Autopilot")
            attempts.append({"attempt": attempt + 1, "status": result.get("status"),
                             "at": self.now(), "failed": len(result.get("skipped_failed_nodes", [])),
                             "reasons": [value.get("reason", "")
                                         for value in result.get("skipped_failed_nodes", [])]})
            if not result.get("remaining_blank_eligible_nodes"):
                break
        item["ai_retry_provenance"] = attempts
        item["what_completed"].append(
            f"Saved {int((result or {}).get('generated_saved', 0))} safe Help Text suggestion(s)"
        )
        if (result or {}).get("skipped_nodes"):
            item.update(state="HUMAN_EXCEPTION",
                        reason="One or more unsafe learning nodes require human attention.",
                        next_human_action="Review the remaining learning exceptions.")
        elif (result or {}).get("remaining_blank_eligible_nodes"):
            item.update(state="FAILED_SAFE",
                        reason="Safe Help Text generation exhausted its bounded retries.",
                        next_human_action="Inspect learning-generation failures.")
        elif (result or {}).get("completion_ready"):
            item.update(state="READY_FOR_FINAL_REVIEW",
                        reason="Learning draft is complete and valid; governed completion remains human-controlled.",
                        next_human_action="Complete Learning Authoring")
        else:
            item.update(state="FAILED_SAFE", reason="Learning preparation exhausted its bounded retries.",
                        next_human_action="Inspect learning-generation failures.")
        item["review_url"] = human.get("specialized_review_link") or human.get("campaign_control_link")
        return item

    def _classify_human_gate(self, item, campaign, human):
        action = str(human.get("action") or "")
        item["review_url"] = human.get("specialized_review_link") or human.get(
            "review_workspace_link") or human.get("campaign_control_link")
        if action == "approve_source":
            packages = [value for value in self.research.list_for_campaign(campaign["campaign_id"])
                        if value.get("work_item_id") == campaign.get("work_item_id")]
            if len(packages) != 1:
                item.update(state="BLOCKED", reason="Source package identity is missing or ambiguous.",
                            next_human_action="Inspect the source-package identity blocker.")
                return item
            try:
                snapshot = self.source_autopilot.current_snapshot(packages[0]["package_id"])
            except SupervisedCampaignAutopilotError as error:
                item.update(state="BLOCKED", reason=str(error),
                            next_human_action="Analyze the source package.")
                return item
            if snapshot.get("approval_blockers"):
                item.update(state="BLOCKED", reason=" ".join(snapshot["approval_blockers"]),
                            next_human_action="Resolve source identity blockers.")
            elif snapshot.get("human_decision_count"):
                item.update(state="HUMAN_EXCEPTION",
                            reason=f"{snapshot['human_decision_count']} ambiguous source decision(s) require review.",
                            next_human_action="Review Source Package")
            else:
                item.update(state="READY_FOR_FINAL_REVIEW",
                            reason="High-confidence source curation is prepared; package approval remains human-controlled.",
                            next_human_action="Approve Source Package")
            item["what_completed"].append(
                f"Curated {snapshot.get('curated_count', 0)} source decision(s)"
            )
            item["review_url"] = (
                f"/curator/growth/source-research/{packages[0]['package_id']}/autopilot"
            )
            return item
        final_actions = {
            "review_evidence", "review_article_draft", "accept_workflow_content_studio",
            "approve_workflow_draft_creation",
        }
        action_labels = {
            "approve_workflow_draft_creation": "Approve Draft Creation",
        }
        item.update(
            state="READY_FOR_FINAL_REVIEW" if action in final_actions else "HUMAN_EXCEPTION",
            reason="The existing campaign reached a governed human decision.",
            next_human_action=(action_labels.get(action) or action.replace("_", " ").title())
            if action else "Review campaign decision",
        )
        return item

    def _selection(self, domain, limit):
        limit = int(limit)
        if not self.MIN_LIMIT <= limit <= self.MAX_LIMIT:
            raise BatchPropagationAutopilotError("Batch limit must be between 1 and 10.")
        normalized = str(domain or "").strip().casefold()
        matches = [item for item in self.growth.planner.domains()
                   if normalized in {str(item.get("id") or "").casefold(),
                                     str(item.get("title") or "").casefold()}]
        if len(matches) != 1:
            raise BatchPropagationAutopilotError(
                "Requested domain must match exactly one configured coverage domain."
            )
        candidates = self.growth.ranked_candidates(matches[0]["id"])
        selected, workflows, identities = [], set(), set()
        for candidate in candidates:
            identity = str(candidate.get("gap_identity") or "")
            workflow = str(candidate.get("workflow_id") or candidate.get("workflow_filename")
                           or f"area:{candidate.get('area_id') or identity}")
            if candidate.get("gap_type") not in SUPPORTED_GAP_TYPES or not identity:
                raise BatchPropagationAutopilotError("Growth candidate is unsupported or ambiguous.")
            if identity in identities:
                raise BatchPropagationAutopilotError("Growth candidate identity is duplicated.")
            identities.add(identity)
            if workflow in workflows:
                continue
            workflows.add(workflow)
            selected.append(deepcopy(candidate))
            if len(selected) == limit:
                break
        return {"id": matches[0]["id"], "title": matches[0]["title"]}, selected

    def _new_record(self, domain, limit, selection, actor):
        created = self.now()
        batch_id = self._stable_id(domain["id"], str(limit),
                                   *[item["gap_identity"] for item in selection])
        return {
            "schema_version": "1.0", "batch_id": batch_id,
            "policy_id": self.POLICY_ID, "policy_version": self.POLICY_VERSION,
            "status": "PROCESSING", "preview": False, "domain": deepcopy(domain),
            "requested_limit": limit, "selected_count": len(selection),
            "selection": deepcopy(selection),
            "items": [self._item_base(item) for item in selection],
            "created_at": created, "updated_at": created,
            "history": [{"event": "batch_created", "at": created, "actor": actor}],
            "limits": {"max_workflows": self.MAX_LIMIT,
                       "max_transitions_per_workflow": self.MAX_TRANSITIONS,
                       "max_ai_retries": self.MAX_AI_RETRIES},
            "authority": {"publication": False, "human_approval": False,
                          "command_execution": False, "recursive_batch": False},
        }

    @staticmethod
    def _item_base(candidate):
        return {"gap_identity": candidate.get("gap_identity"),
                "gap_type": candidate.get("gap_type"),
                "workflow_id": candidate.get("workflow_id") or candidate.get("area_id"),
                "workflow_name": candidate.get("area_title") or candidate.get("title")
                or candidate.get("gap_identity"), "state": "PROCESSING",
                "what_completed": [], "next_human_action": "", "reason": "",
                "review_url": "", "campaign": {}, "attempts": [], "reused": False}

    @staticmethod
    def _preview_item(position, candidate, outcome):
        item = BatchPropagationAutopilotService._item_base(candidate)
        item.update(position=position, state="PREVIEW", reason=candidate.get("selection_explanation", ""),
                    campaign=deepcopy(outcome.get("campaign") or {}),
                    reused=(outcome.get("campaign") or {}).get("disposition") == "reuse")
        return item

    def _projection(self, record):
        result = deepcopy(record)
        result["counts"] = self._counts(result.get("items", []))
        result["review_queue"] = [item for item in result.get("items", [])
                                  if item.get("state") in {"READY_FOR_FINAL_REVIEW", "HUMAN_EXCEPTION", "BLOCKED", "FAILED_SAFE"}]
        result["control_center_url"] = (f"/curator/growth/propagation-batches/{result['batch_id']}"
                                        if result.get("batch_id") else None)
        return result

    @staticmethod
    def _counts(items):
        counts = {name: 0 for name in BatchPropagationAutopilotService.STATES}
        counts["PROCESSING"] = 0
        for item in items:
            if item.get("state") in counts:
                counts[item["state"]] += 1
        counts["PROCESSED"] = sum(counts[name] for name in BatchPropagationAutopilotService.STATES)
        return counts

    @staticmethod
    def _batch_status(items):
        states = {item.get("state") for item in items}
        if "FAILED_SAFE" in states:
            return "PARTIAL_FAILED"
        if states & {"HUMAN_EXCEPTION", "BLOCKED", "READY_FOR_FINAL_REVIEW"}:
            return "AWAITING_HUMAN"
        return "COMPLETE"

    @staticmethod
    def _retryable(reason):
        text = str(reason or "").casefold()
        prohibited = ("identity", "ambiguous", "stale", "fingerprint", "human", "govern")
        if any(value in text for value in prohibited):
            return False
        return any(value in text for value in ("provider", "temporar", "validation", "ai"))

    def _find_resumable(self, domain_id, limit, selection):
        if not self.batch_root.exists():
            return None
        records = [self._validated_record(self._read(path), path.stem)
                   for path in self.batch_root.glob("KPB-*.json")]
        identities = [item["gap_identity"] for item in selection]
        matches = [value for value in records
                   if value.get("domain", {}).get("id") == domain_id
                   and value.get("requested_limit") == limit
                   and [item.get("gap_identity") for item in value.get("selection", [])]
                   == identities]
        return sorted(matches, key=lambda value: value.get("created_at", ""))[-1] if matches else None

    def _validated_record(self, record, expected_id):
        selection = list(record.get("selection") or [])
        if (record.get("batch_id") != expected_id
                or record.get("policy_id") != self.POLICY_ID
                or record.get("policy_version") != self.POLICY_VERSION
                or not 1 <= len(selection) <= self.MAX_LIMIT):
            raise BatchPropagationAutopilotError(
                "Persisted batch identity, policy, or bounds are invalid."
            )
        identities, workflows = set(), set()
        for candidate in selection:
            identity = str(candidate.get("gap_identity") or "")
            workflow = str(candidate.get("workflow_id") or candidate.get("workflow_filename")
                           or f"area:{candidate.get('area_id') or identity}")
            if (candidate.get("gap_type") not in SUPPORTED_GAP_TYPES
                    or not identity or identity in identities or workflow in workflows):
                raise BatchPropagationAutopilotError(
                    "Persisted batch selection is unsupported or ambiguous."
                )
            identities.add(identity); workflows.add(workflow)
        return record

    def _path(self, batch_id):
        if not str(batch_id or "").startswith("KPB-") or "/" in batch_id or "\\" in batch_id:
            raise BatchPropagationAutopilotError("Invalid batch identity.")
        return self.batch_root / f"{batch_id}.json"

    def _save(self, record):
        self.batch_root.mkdir(parents=True, exist_ok=True)
        self._write(self._path(record["batch_id"]), record)

    @staticmethod
    def _read(path):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise BatchPropagationAutopilotError(f"Batch state could not be read: {error}") from error

    @staticmethod
    def _write(path, value):
        temporary = None
        try:
            with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False,
                                             encoding="utf-8", suffix=".tmp") as output:
                temporary = output.name
                json.dump(value, output, indent=2, ensure_ascii=False, sort_keys=True)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
        finally:
            if temporary:
                Path(temporary).unlink(missing_ok=True)

    @staticmethod
    def _stable_id(*parts):
        digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:12].upper()
        return f"KPB-{digest}"

    @staticmethod
    @contextmanager
    def _lock(path, timeout=2.0):
        path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + timeout
        descriptor = None
        while descriptor is None:
            try:
                descriptor = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                if time.monotonic() >= deadline:
                    raise BatchPropagationAutopilotError("Another batch pass is active.")
                time.sleep(0.02)
        try:
            os.close(descriptor)
            descriptor = None
            yield
        finally:
            if descriptor is not None:
                os.close(descriptor)
            path.unlink(missing_ok=True)
