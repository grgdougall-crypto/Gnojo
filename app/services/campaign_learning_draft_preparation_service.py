from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from app.data_root import resolve_data_root
from app.services.curator_workflow_lifecycle_service import (
    CuratorWorkflowLifecycleService,
)
from app.services.knowledge_campaign_orchestration_service import (
    KnowledgeCampaignOrchestrationError,
    KnowledgeCampaignOrchestrationService,
)
from app.services.knowledge_coverage_planner_service import (
    KnowledgeCoveragePlannerError,
    KnowledgeCoveragePlannerService,
)
from app.services.workflow_draft_persistence import WorkflowDraftPersistenceError
from app.services.workflow_draft_service import WorkflowDraftError, WorkflowDraftService
from app.services.workflow_help_text_service import (
    WorkflowHelpTextError,
    WorkflowHelpTextService,
)
from app.services.workflow_validation_service import WorkflowValidationService
from curator.workflow_reasoning import WorkflowReasoningAuditor


class CampaignLearningDraftPreparationError(ValueError):
    """Raised when governed learning-draft preparation cannot proceed safely."""


class CampaignLearningDraftPreparationService:
    """Prepare AI help text for one campaign-owned editable workflow draft."""

    EVENT = "learning_draft_prepared"
    PROVENANCE_KEY = "help_text_generation"

    def __init__(
        self,
        repository_root: Path | None = None,
        campaign_root: Path | None = None,
        *,
        planner: Any | None = None,
        help_text: WorkflowHelpTextService | None = None,
        now: Callable[[], str] | None = None,
    ) -> None:
        self.root = resolve_data_root(
            repository_root, legacy_root=Path(__file__).resolve().parents[2]
        )
        self.campaign_root = Path(
            campaign_root or self.root / "knowledge_campaigns"
        ).resolve()
        self.planner = planner or KnowledgeCoveragePlannerService(
            self.root, self.campaign_root
        )
        self.help_text = help_text or WorkflowHelpTextService()
        self.now = now or (lambda: datetime.now(timezone.utc).isoformat())

    def preview(
        self, campaign_id: str, orchestration_id: str, work_item_id: str
    ) -> dict[str, Any]:
        context = self._context(campaign_id, orchestration_id, work_item_id)
        return self._preview_from_context(context)

    def _preview_from_context(self, context: dict[str, Any]) -> dict[str, Any]:
        workflow = context["workflow"]
        nodes = workflow["nodes"]
        validation = WorkflowValidationService().validate(workflow)
        candidates = KnowledgeCoveragePlannerService.learning_help_text_candidate_ids(
            workflow
        )
        supported = [
            (node_id, node) for node_id, node in nodes.items()
            if isinstance(node, dict)
            and node.get("type") in {"question", "instruction"}
        ]
        skipped = [
            {
                "node_id": node_id,
                "reason": "This blank node requires manual learning review because it includes a state-changing action.",
            }
            for node_id, node in supported
            if not str(node.get("help_text") or "").strip()
            and node_id not in candidates
        ]
        return {
            "campaign_id": context["orchestration"]["campaign_id"],
            "orchestration_id": context["orchestration"]["orchestration_id"],
            "work_item_id": context["work"]["work_item_id"],
            "workflow_id": context["workflow_id"],
            "workflow_filename": context["filename"],
            "workflow_name": workflow.get("name") or context["workflow_id"],
            "nodes_scanned": len(nodes),
            "existing_help_preserved": sum(
                bool(str(node.get("help_text") or "").strip())
                for _, node in supported
            ),
            "eligible_nodes": [
                {
                    "node_id": node_id,
                    "title": nodes[node_id].get("title")
                    or nodes[node_id].get("question")
                    or node_id.replace("_", " ").title(),
                }
                for node_id in candidates
            ],
            "eligible_count": len(candidates),
            "skipped_nodes": skipped,
            "draft_fingerprint": context["draft_fingerprint"],
            "workflow_validation_clean": bool(validation.get("is_valid")),
            "completion_ready": not candidates and bool(validation.get("is_valid")),
            "publication_unchanged": True,
            "human_gate_unchanged": True,
        }

    def prepare(
        self, campaign_id: str, orchestration_id: str, work_item_id: str,
        *, actor: str,
    ) -> dict[str, Any]:
        context = self._context(campaign_id, orchestration_id, work_item_id)
        workflow = context["workflow"]
        nodes = workflow["nodes"]
        candidates = KnowledgeCoveragePlannerService.learning_help_text_candidate_ids(
            workflow
        )
        preview = self._preview_from_context(context)
        if not candidates:
            return {
                **preview,
                "status": "no_changes",
                "generated_saved": 0,
                "generated_nodes": [],
                "skipped_failed_nodes": deepcopy(preview["skipped_nodes"]),
                "remaining_blank_eligible_nodes": [],
                "provenance": [],
            }

        proposed = deepcopy(workflow)
        baseline_findings = self._finding_identities(workflow)
        generated: list[dict[str, Any]] = []
        failures: list[dict[str, str]] = deepcopy(preview["skipped_nodes"])
        accepted_at = self.now()
        for node_id in candidates:
            current_node = nodes[node_id]
            try:
                suggestion = self.help_text.suggest(
                    workflow, node_id, current_node, allow_fallback=False
                )
                if suggestion.get("used_fallback"):
                    raise WorkflowHelpTextError(
                        "Automated batch preparation requires a configured AI provider."
                    )
                candidate_text = self.help_text.validate_candidate(
                    current_node, suggestion.get("help_text")
                )
                candidate_node = deepcopy(proposed["nodes"][node_id])
                candidate_node["help_text"] = candidate_text
                if KnowledgeCoveragePlannerService._safety_ambiguous(candidate_node):
                    raise WorkflowHelpTextError(
                        "Generated guidance introduced an unrelated state-changing action."
                    )
                provenance = {
                    "provider": str(suggestion.get("provider") or "Unknown"),
                    "model": str(suggestion.get("model") or "Unknown"),
                    "node_id": node_id,
                    "generated_at": accepted_at,
                    "accepted_at": accepted_at,
                    "campaign_id": campaign_id,
                    "work_item_id": work_item_id,
                }
                candidate_node[self.PROVENANCE_KEY] = provenance
                simulated = deepcopy(proposed)
                simulated["nodes"][node_id] = candidate_node
                validation = WorkflowValidationService().validate(simulated)
                if not validation["is_valid"]:
                    raise WorkflowHelpTextError(
                        "Generated guidance did not preserve workflow validation."
                    )
                if self._finding_identities(simulated) - baseline_findings:
                    raise WorkflowHelpTextError(
                        "Generated guidance introduced a new workflow reasoning finding."
                    )
                proposed = simulated
                generated.append({
                    "node_id": node_id,
                    "title": preview["eligible_nodes"][candidates.index(node_id)]["title"],
                    "help_text": candidate_text,
                    **provenance,
                })
            except (WorkflowHelpTextError, ValueError, RuntimeError) as error:
                failures.append({"node_id": node_id, "reason": str(error)})

        if self._without_generated_help(proposed) != self._without_generated_help(workflow):
            raise CampaignLearningDraftPreparationError(
                "Learning preparation attempted to change content outside help text."
            )
        for node_id, original in nodes.items():
            if (
                isinstance(original, dict)
                and str(original.get("help_text") or "").strip()
                and proposed["nodes"][node_id].get("help_text")
                != original.get("help_text")
            ):
                raise CampaignLearningDraftPreparationError(
                    "Learning preparation attempted to overwrite existing help text."
                )

        remaining = [node_id for node_id in candidates if node_id not in {
            item["node_id"] for item in generated
        }]
        result = {
            **preview,
            "status": "prepared" if generated and not failures else (
                "partially_prepared" if generated else "failed"
            ),
            "generated_saved": len(generated),
            "generated_nodes": generated,
            "skipped_failed_nodes": failures,
            "remaining_blank_eligible_nodes": remaining,
            "provenance": [
                {key: item[key] for key in (
                    "provider", "model", "node_id", "generated_at",
                    "accepted_at", "campaign_id", "work_item_id",
                )}
                for item in generated
            ],
        }
        self._commit(context, proposed, result, actor)
        current = self.preview(campaign_id, orchestration_id, work_item_id)
        result["completion_ready"] = current["completion_ready"]
        result["completion_draft_fingerprint"] = current["draft_fingerprint"]
        result["workflow_validation_clean"] = current["workflow_validation_clean"]
        return result

    def _context(
        self, campaign_id: str, orchestration_id: str, work_item_id: str
    ) -> dict[str, Any]:
        try:
            campaign = self.planner.get(campaign_id)
        except KnowledgeCoveragePlannerError as error:
            raise CampaignLearningDraftPreparationError(str(error)) from error
        works = [
            item for item in campaign.get("work_items", [])
            if isinstance(item, dict)
            and item.get("work_item_id") == work_item_id
            and item.get("work_type") == "learning_content"
        ]
        if len(works) != 1:
            raise CampaignLearningDraftPreparationError(
                "The weak-learning campaign work item is missing or ambiguous."
            )
        work = works[0]
        gaps = [
            item for item in campaign.get("gaps", [])
            if isinstance(item, dict)
            and item.get("gap_id") == work.get("gap_id")
            and item.get("gap_type") == "weak_learning_coverage"
        ]
        if len(gaps) != 1:
            raise CampaignLearningDraftPreparationError(
                "The work item lacks unambiguous weak-learning campaign provenance."
            )
        workflow_id = str(work.get("workflow_id") or "").strip()
        if not workflow_id:
            raise CampaignLearningDraftPreparationError(
                "The campaign does not identify one authoritative workflow."
            )

        orchestration_path = (
            self.campaign_root / "orchestration" / f"{orchestration_id}.json"
        )
        try:
            orchestration_raw = orchestration_path.read_bytes()
            orchestration = json.loads(orchestration_raw.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise CampaignLearningDraftPreparationError(
                "The campaign orchestration could not be read safely."
            ) from error
        states = [
            item for item in orchestration.get("work_item_states", [])
            if isinstance(item, dict) and item.get("work_item_id") == work_item_id
        ]
        if (
            orchestration.get("campaign_id") != campaign_id
            or orchestration.get("orchestration_id") != orchestration_id
            or len(states) != 1
            or states[0].get("state") != "awaiting_human_review"
            or states[0].get("action_authority") != "human_gate"
            or states[0].get("next_action") != "author_learning_content"
        ):
            raise CampaignLearningDraftPreparationError(
                "The work item is no longer at its governed learning-authoring gate."
            )

        drafts = CuratorWorkflowLifecycleService(self.root).drafts(workflow_id)
        if len(drafts) != 1:
            raise CampaignLearningDraftPreparationError(
                "Create or reconcile exactly one editable workflow draft before preparing learning content."
            )
        target = drafts[0]
        draft_path = (self.root / target.source_path).resolve()
        expected_directory = (self.root / "app" / "workflow_drafts").resolve()
        if draft_path.parent != expected_directory or target.workflow_id != workflow_id:
            raise CampaignLearningDraftPreparationError(
                "The editable workflow identity could not be resolved safely."
            )
        try:
            draft_raw = draft_path.read_bytes()
        except OSError as error:
            raise CampaignLearningDraftPreparationError(
                "The editable workflow draft could not be read safely."
            ) from error
        return {
            "campaign": campaign,
            "work": work,
            "orchestration": orchestration,
            "orchestration_path": orchestration_path,
            "orchestration_raw": orchestration_raw,
            "workflow_id": workflow_id,
            "filename": target.filename,
            "workflow": deepcopy(target.workflow),
            "draft_raw": draft_raw,
            "draft_fingerprint": self._sha(draft_raw),
        }

    def _commit(
        self, context: dict[str, Any], proposed: dict[str, Any],
        result: dict[str, Any], actor: str,
    ) -> None:
        orchestration = deepcopy(context["orchestration"])
        event_id = hashlib.sha256(
            "|".join((
                context["orchestration"]["orchestration_id"],
                context["work"]["work_item_id"],
                context["draft_fingerprint"],
                ",".join(item["node_id"] for item in result["generated_nodes"]),
            )).encode("utf-8")
        ).hexdigest()[:16].upper()
        if any(
            event.get("event") == self.EVENT
            and event.get("preparation_id") == event_id
            for event in orchestration.get("history", [])
        ):
            return
        event = {
            "event": self.EVENT,
            "preparation_id": event_id,
            "at": self.now(),
            "actor": str(actor or "Reviewer"),
            "campaign_id": orchestration["campaign_id"],
            "work_item_id": context["work"]["work_item_id"],
            "workflow_id": context["workflow_id"],
            "workflow_filename": context["filename"],
            "status": result["status"],
            "summary": {
                key: deepcopy(result[key]) for key in (
                    "nodes_scanned", "existing_help_preserved", "generated_saved",
                    "skipped_failed_nodes", "remaining_blank_eligible_nodes",
                )
            },
            "provenance": deepcopy(result["provenance"]),
            "draft_fingerprint_before": context["draft_fingerprint"],
        }
        orchestration.setdefault("history", []).append(event)
        orchestration["updated_at"] = event["at"]
        orchestration_lock = self.campaign_root / ".learning-draft-preparation.lock"
        draft_service = WorkflowDraftService(self.root / "app" / "workflow_drafts")
        try:
            with KnowledgeCampaignOrchestrationService._decision_lock(
                orchestration_lock, operation="learning draft preparation"
            ):
                if context["orchestration_path"].read_bytes() != context["orchestration_raw"]:
                    raise CampaignLearningDraftPreparationError(
                        "The campaign changed during preparation. Reload and try again."
                    )
                with draft_service._locked(context["filename"]) as draft:
                    before = draft.read()
                    if before.content != context["draft_raw"]:
                        raise CampaignLearningDraftPreparationError(
                            "The editable workflow changed during preparation. Reload and try again."
                        )
                    replacement = None
                    try:
                        if result["generated_saved"]:
                            replacement = draft.replace(before.raw_sha256, proposed)
                            event["draft_fingerprint_after"] = replacement.after.raw_sha256
                        else:
                            event["draft_fingerprint_after"] = before.raw_sha256
                        KnowledgeCampaignOrchestrationService._atomic_write_bytes(
                            context["orchestration_path"],
                            KnowledgeCampaignOrchestrationService._json_bytes(orchestration),
                        )
                    except Exception as error:
                        if replacement is not None:
                            try:
                                draft.restore(
                                    replacement.after.raw_sha256,
                                    replacement.before.content,
                                )
                            except Exception as rollback_error:
                                raise CampaignLearningDraftPreparationError(
                                    "Learning preparation failed and exact draft rollback was incomplete: "
                                    f"{rollback_error}"
                                ) from error
                        if isinstance(error, CampaignLearningDraftPreparationError):
                            raise
                        raise CampaignLearningDraftPreparationError(
                            f"Learning preparation failed; the draft was restored: {error}"
                        ) from error
        except (
            KnowledgeCampaignOrchestrationError,
            WorkflowDraftError,
            WorkflowDraftPersistenceError,
            OSError,
        ) as error:
            raise CampaignLearningDraftPreparationError(str(error)) from error

    @staticmethod
    def _without_generated_help(workflow: dict[str, Any]) -> dict[str, Any]:
        value = deepcopy(workflow)
        for node in (value.get("nodes") or {}).values():
            if isinstance(node, dict):
                node.pop("help_text", None)
                node.pop(CampaignLearningDraftPreparationService.PROVENANCE_KEY, None)
        return value

    @staticmethod
    def _finding_identities(workflow: dict[str, Any]) -> set[tuple[str, str, str]]:
        return {
            (item.rule, item.finding_type, item.node_id)
            for item in WorkflowReasoningAuditor().analyze(workflow)
        }

    @staticmethod
    def _sha(content: bytes) -> str:
        return hashlib.sha256(content).hexdigest()
