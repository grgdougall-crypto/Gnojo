import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.data_root import APPLICATION_ROOT, resolve_data_path, resolve_data_root
from app.knowledge.knowledge_base import KnowledgeBase
from app.repositories.command_repository import CommandRepository
from app.repositories.knowledge_repository import KnowledgeRepository
from app.repositories.script_repository import ScriptRepository
from app.services.autonomous_growth_service import AutonomousGrowthService
from app.services.curator_article_link_repair_service import CuratorArticleLinkRepairService
from app.services.curator_batch_service import CuratorBatchService
from app.services.curator_dashboard_service import CuratorDashboardService
from app.services.curator_fix_session_service import CuratorFixSessionService
from app.services.curator_growth_service import CuratorGrowthService
from app.services.curator_resolution_service import CuratorResolutionService
from app.services.curator_stage_b_reconciliation_service import CuratorStageBReconciliationService
from app.services.curator_task_service import CuratorTaskService
from app.services.device_profile_service import DeviceProfileService
from app.services.knowledge_campaign_orchestration_service import KnowledgeCampaignOrchestrationService
from app.services.knowledge_coverage_planner_service import KnowledgeCoveragePlannerService
from app.services.review_workspace_service import ReviewWorkspaceService
from app.services.script_authoring_service import ScriptAuthoringService
from app.services.troubleshooting_history_service import TroubleshootingHistoryService
from app.services.workflow_draft_service import WorkflowDraftService
from app.services.workflow_publication_service import WorkflowPublicationService
from curator.auditor import CuratorAuditor
from curator.observation_runner import CuratorObservationRunner


class DataRootTests(unittest.TestCase):
    def test_unset_data_root_preserves_legacy_defaults(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GNOJO_DATA_ROOT", None)
            self.assertEqual(resolve_data_root(), APPLICATION_ROOT)
            self.assertEqual(
                WorkflowDraftService().drafts_path,
                APPLICATION_ROOT / "app" / "workflow_drafts",
            )
            self.assertEqual(
                WorkflowPublicationService().publication_path,
                APPLICATION_ROOT / "app" / "workflow_publications",
            )
            self.assertEqual(
                KnowledgeRepository().knowledge_base_directory,
                APPLICATION_ROOT / "knowledge_base",
            )
            self.assertEqual(CommandRepository().base_path, Path("knowledge_base/commands"))
            self.assertEqual(ScriptRepository().base_path, Path("knowledge_base/scripts"))

    def test_configured_data_root_redirects_runtime_repositories(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            with patch.dict(os.environ, {"GNOJO_DATA_ROOT": str(root)}):
                drafts = WorkflowDraftService()
                publications = WorkflowPublicationService()
                profiles = DeviceProfileService()
                history = TroubleshootingHistoryService()
                knowledge = KnowledgeRepository()
                dashboard = CuratorDashboardService()
                tasks = CuratorTaskService()
                growth = CuratorGrowthService()
                repair = CuratorArticleLinkRepairService()
                stage_b = CuratorStageBReconciliationService()
                review = ReviewWorkspaceService()
                batch = CuratorBatchService()
                fix_sessions = CuratorFixSessionService()
                resolutions = CuratorResolutionService()
                campaigns = KnowledgeCoveragePlannerService()
                orchestration = KnowledgeCampaignOrchestrationService()
                autonomous_growth = AutonomousGrowthService()
                auditor = CuratorAuditor()
                observations = CuratorObservationRunner(root)

                expected = {
                    drafts.drafts_path: root / "app" / "workflow_drafts",
                    drafts.persistence.lock_path: root / "app" / ".workflow_draft_locks",
                    publications.publication_path: root / "app" / "workflow_publications",
                    profiles.profile_path: root / "app" / "device_profiles",
                    history.history_path: root / "app" / "troubleshooting_history",
                    knowledge.knowledge_base_directory: root / "knowledge_base",
                    knowledge.draft_directory: root / "knowledge_base" / "drafts",
                    knowledge.published_directory: root / "knowledge_base" / "published",
                    knowledge.archive_directory: root / "knowledge_base" / "archive",
                    knowledge.deleted_directory: root / "knowledge_base" / "deleted",
                    CommandRepository().base_path: root / "knowledge_base" / "commands",
                    ScriptRepository().base_path: root / "knowledge_base" / "scripts",
                    ScriptAuthoringService().base_path: root / "knowledge_base" / "scripts",
                    KnowledgeBase().knowledge_path: root / "knowledge_base",
                    dashboard.output_root: root / "curation_runs",
                    dashboard.memory_root: root / "curation_memory",
                    tasks.store.root: root / "curation_memory",
                    growth.store.root: root / "curation_memory",
                    repair.root: root,
                    stage_b.root: root,
                    review.root: root,
                    fix_sessions.directory: root / "curation_memory" / "fix_sessions",
                    resolutions.packages.root: root / "curation_memory" / "resolution_packages",
                    batch.lock_path: root / ".curator-resolution-batch.lock",
                    campaigns.campaign_root: root / "knowledge_campaigns",
                    orchestration.campaign_root: root / "knowledge_campaigns",
                    autonomous_growth.campaign_root: root / "knowledge_campaigns",
                    auditor.output_root: root / "curation_runs",
                    auditor.memory_root: root / "curation_memory",
                    observations.results.root: root / "curation_observations",
                    observations.memory_root: root / "curation_memory",
                    observations.lock_path: root / ".curator-observation.lock",
                    dashboard.repository_root / ".curator-audit.lock": root / ".curator-audit.lock",
                }
                for actual, wanted in expected.items():
                    self.assertEqual(actual.resolve(), wanted.resolve())
                    actual.resolve().relative_to(root)

    def test_explicit_repository_paths_override_configured_root(self):
        with tempfile.TemporaryDirectory() as configured, tempfile.TemporaryDirectory() as explicit:
            explicit_root = Path(explicit).resolve()
            with patch.dict(os.environ, {"GNOJO_DATA_ROOT": configured}):
                self.assertEqual(
                    CuratorDashboardService(explicit_root).repository_root,
                    explicit_root,
                )
                self.assertEqual(
                    KnowledgeRepository(explicit_root / "knowledge").knowledge_base_directory,
                    explicit_root / "knowledge",
                )

    def test_campaign_taxonomy_remains_source_relative_with_configured_data_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_root = Path(temporary).resolve()
            taxonomy = (
                APPLICATION_ROOT / "app" / "data"
                / "knowledge_coverage_taxonomy.json"
            )
            with patch.dict(os.environ, {"GNOJO_DATA_ROOT": str(data_root)}):
                orchestration = KnowledgeCampaignOrchestrationService()

            taxonomy_readers = (
                orchestration.planner,
                orchestration.research.planner,
                orchestration.evidence.research.planner,
                orchestration.generation.planner,
                orchestration.generation.research.planner,
                orchestration.generation.extraction.research.planner,
            )
            for reader in taxonomy_readers:
                self.assertEqual(reader.taxonomy_path.resolve(), taxonomy.resolve())
                self.assertNotEqual(
                    reader.taxonomy_path.resolve(),
                    (data_root / "app" / "data"
                     / "knowledge_coverage_taxonomy.json").resolve(),
                )

    def test_configured_data_path_rejects_traversal(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch.dict(os.environ, {"GNOJO_DATA_ROOT": temporary}):
                with self.assertRaisesRegex(ValueError, "escapes GNOJO_DATA_ROOT"):
                    resolve_data_path("..", "escape", legacy_path="unused")

    def test_flask_boot_uses_configured_data_root_for_global_repositories(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            environment = os.environ.copy()
            environment["GNOJO_DATA_ROOT"] = str(root)
            completed = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    (
                        "from unittest.mock import patch\n"
                        "with patch('dotenv.load_dotenv', return_value=False):\n"
                        "    import app.app as module\n"
                        "    root = module._structural_repository_root()\n"
                        "    assert module.knowledge_repository.knowledge_base_directory == root / 'knowledge_base'\n"
                        "    assert module.command_repository.base_path == root / 'knowledge_base' / 'commands'\n"
                        "    assert module.WorkflowDraftService().drafts_path == root / 'app' / 'workflow_drafts'\n"
                        "    assert module.app.test_client().get('/').status_code == 200\n"
                    ),
                ],
                cwd=APPLICATION_ROOT,
                env=environment,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)


if __name__ == "__main__":
    unittest.main()
