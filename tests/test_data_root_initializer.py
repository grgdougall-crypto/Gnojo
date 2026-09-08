import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from app.data_root import APPLICATION_ROOT
from app.data_root_initializer import (
    BASELINE_DIRECTORIES,
    BASELINE_FILES,
    EMPTY_RUNTIME_DIRECTORIES,
    DataRootInitializationError,
    initialize_data_root,
)
from curator.__main__ import main


class DataRootInitializerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.target = self.root / "data"
        self._create_baseline(self.source)

    def tearDown(self):
        self.temporary.cleanup()

    def test_fresh_root_initializes_required_baseline_and_empty_stores(self):
        self.target.mkdir()
        before = self._fingerprint(self.source)
        result = initialize_data_root(
            source_root=self.source,
            environment={"GNOJO_DATA_ROOT": str(self.target)},
        )

        self.assertEqual(result["status"], "INITIALIZED")
        self.assertEqual(Path(result["data_root"]), self.target.resolve())
        for relative in BASELINE_DIRECTORIES:
            self.assertEqual(
                (self.target / relative / "baseline.json").read_bytes(),
                (self.source / relative / "baseline.json").read_bytes(),
            )
        for relative in BASELINE_FILES:
            self.assertEqual(
                (self.target / relative).read_bytes(),
                (self.source / relative).read_bytes(),
            )
        for relative in EMPTY_RUNTIME_DIRECTORIES:
            self.assertTrue((self.target / relative).is_dir())
            self.assertEqual(list((self.target / relative).iterdir()), [])
        for excluded in (
            "curation_memory",
            "curation_runs",
            "curation_observations",
            "knowledge_campaigns",
            "app/.workflow_draft_locks",
        ):
            self.assertFalse((self.target / excluded).exists())
        self.assertEqual(self._fingerprint(self.source), before)

    def test_railway_scaffold_and_lost_found_initialize_without_deletion(self):
        scaffold = (
            "lost+found",
            "app/workflow_drafts",
            "knowledge_base/archive",
            "knowledge_base/deleted",
            "knowledge_base/drafts",
            "knowledge_base/published",
        )
        for relative in scaffold:
            (self.target / relative).mkdir(parents=True, exist_ok=True)

        initialize_data_root(
            source_root=self.source,
            environment={"GNOJO_DATA_ROOT": str(self.target)},
        )

        for relative in scaffold:
            self.assertTrue((self.target / relative).is_dir())
        self.assertEqual(list((self.target / "lost+found").iterdir()), [])
        self.assertTrue((self.target / "knowledge_base/published/baseline.json").is_file())

    def test_file_inside_permitted_scaffold_causes_refusal(self):
        scaffold = self.target / "app" / "workflow_drafts"
        scaffold.mkdir(parents=True)
        sentinel = scaffold / "existing.json"
        sentinel.write_text("{}", encoding="utf-8")

        with self.assertRaisesRegex(DataRootInitializationError, "already populated"):
            initialize_data_root(
                source_root=self.source,
                environment={"GNOJO_DATA_ROOT": str(self.target)},
            )

        self.assertEqual(sentinel.read_text(encoding="utf-8"), "{}")

    def test_unexpected_directory_causes_refusal(self):
        unexpected = self.target / "unrecognized"
        unexpected.mkdir(parents=True)

        with self.assertRaisesRegex(DataRootInitializationError, "already populated"):
            initialize_data_root(
                source_root=self.source,
                environment={"GNOJO_DATA_ROOT": str(self.target)},
            )

        self.assertTrue(unexpected.is_dir())

    def test_populated_target_is_rejected_without_overwrite(self):
        self.target.mkdir()
        sentinel = self.target / "keep.txt"
        sentinel.write_text("unchanged", encoding="utf-8")

        with self.assertRaisesRegex(DataRootInitializationError, "already populated"):
            initialize_data_root(
                source_root=self.source,
                environment={"GNOJO_DATA_ROOT": str(self.target)},
            )

        self.assertEqual(sentinel.read_text(encoding="utf-8"), "unchanged")
        self.assertEqual(list(self.target.iterdir()), [sentinel])

    def test_unset_data_root_is_rejected(self):
        with self.assertRaisesRegex(DataRootInitializationError, "must be set"):
            initialize_data_root(source_root=self.source, environment={})

    def test_second_initialization_is_rejected_without_changes(self):
        environment = {"GNOJO_DATA_ROOT": str(self.target)}
        initialize_data_root(source_root=self.source, environment=environment)
        before = self._fingerprint(self.target)

        with self.assertRaisesRegex(DataRootInitializationError, "already populated"):
            initialize_data_root(source_root=self.source, environment=environment)

        self.assertEqual(self._fingerprint(self.target), before)

    def test_missing_required_source_content_is_rejected_before_target_creation(self):
        (self.source / "knowledge_base" / "inventory.json").unlink()

        with self.assertRaisesRegex(DataRootInitializationError, "inventory.json"):
            initialize_data_root(
                source_root=self.source,
                environment={"GNOJO_DATA_ROOT": str(self.target)},
            )

        self.assertFalse(self.target.exists())

    def test_cli_initializes_once_and_reports_second_attempt_as_failure(self):
        output = io.StringIO()
        errors = io.StringIO()
        with patch.dict(os.environ, {"GNOJO_DATA_ROOT": str(self.target)}), patch(
            "app.data_root_initializer.APPLICATION_ROOT", self.source
        ), redirect_stdout(output), redirect_stderr(errors):
            first = main(["init-data-root"])
            second = main(["init-data-root"])

        self.assertEqual(first, 0)
        self.assertEqual(json.loads(output.getvalue().splitlines()[0])["status"], "INITIALIZED")
        self.assertEqual(second, 2)
        self.assertIn("already populated", errors.getvalue())

    def test_initialized_real_baseline_supports_fresh_root_flask_boot(self):
        target = self.root / "real-data"
        initialize_data_root(
            source_root=APPLICATION_ROOT,
            environment={"GNOJO_DATA_ROOT": str(target)},
        )
        environment = os.environ.copy()
        environment["GNOJO_DATA_ROOT"] = str(target)
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "from unittest.mock import patch\n"
                    "with patch('dotenv.load_dotenv', return_value=False):\n"
                    "    import app.app as module\n"
                    "    assert len(module.knowledge_repository.get_published()) == 32\n"
                    "    assert module.command_repository.get_all()\n"
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

    @staticmethod
    def _create_baseline(source: Path) -> None:
        for index, relative in enumerate(BASELINE_DIRECTORIES):
            directory = source / relative
            directory.mkdir(parents=True)
            (directory / "baseline.json").write_text(
                json.dumps({"baseline": index}), encoding="utf-8"
            )
        for index, relative in enumerate(BASELINE_FILES):
            path = source / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"baseline": index}), encoding="utf-8")

    @staticmethod
    def _fingerprint(root: Path) -> str:
        digest = hashlib.sha256()
        for path in sorted(item for item in root.rglob("*") if item.is_file()):
            digest.update(path.relative_to(root).as_posix().encode("utf-8"))
            digest.update(b"\0")
            digest.update(path.read_bytes())
        return digest.hexdigest()


if __name__ == "__main__":
    unittest.main()
