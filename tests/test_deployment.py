from __future__ import annotations

from pathlib import Path
import os
import plistlib
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from roster.deployment import Deployer, DeploymentPaths, resolve_workspace
from roster.launchd_scheduler import PLIST_FILENAME, LaunchdScheduler


class FakeLaunchctl:
    def __init__(self):
        self.loaded = set()

    def __call__(self, arguments, **kwargs):
        action = arguments[1]
        if action == "print":
            label = arguments[2].rsplit("/", 1)[-1]
            return subprocess.CompletedProcess(arguments, 0 if label in self.loaded else 113, "", "")
        if action == "bootstrap":
            self.loaded.add(plistlib.loads(Path(arguments[3]).read_bytes())["Label"])
            return subprocess.CompletedProcess(arguments, 0, "", "")
        if action == "bootout":
            self.loaded.discard(arguments[2].rsplit("/", 1)[-1])
            return subprocess.CompletedProcess(arguments, 0, "", "")
        return subprocess.CompletedProcess(arguments, 1, "", "unexpected")


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.temporary = Path(self.directory.name).resolve()
        self.workspace = Path(__file__).resolve().parents[1]
        self.paths = DeploymentPaths.for_root(self.temporary / "Application Support" / "wife-roster")
        self.fake_dependency = self.temporary / "dependency" / "airportsdata"
        self.fake_dependency.mkdir(parents=True)
        (self.fake_dependency / "__init__.py").write_text(
            "def load(kind):\n    return {}\n",
            encoding="utf-8",
        )

    def tearDown(self):
        self.directory.cleanup()

    def deployer(self):
        return Deployer(
            workspace=self.workspace,
            paths=self.paths,
            dependency_locator=lambda name: self.fake_dependency,
            require_git=False,
        )

    def test_deployment_path_resolution(self):
        with patch.dict(
            os.environ,
            {"WIFE_ROSTER_DEPLOY_ROOT": str(self.paths.root)},
            clear=False,
        ):
            resolved = DeploymentPaths.default()
        self.assertEqual(resolved.root, self.paths.root)
        self.assertEqual(resolved.database, self.paths.root / "runtime" / "state.db")
        self.assertEqual(resolved.venv, self.paths.root / "venv")

    def test_workspace_resolution_finds_project(self):
        resolved = resolve_workspace(self.workspace / "src" / "roster", require_git=False)
        self.assertEqual(resolved, self.workspace)

    def test_runtime_directories_and_allowlisted_app_are_created(self):
        result = self.deployer().deploy()
        self.assertTrue(result.verified)
        self.assertTrue(self.paths.python.is_file())
        self.assertTrue(self.paths.logs.is_dir())
        self.assertTrue(self.paths.private.is_dir())
        self.assertTrue((self.paths.app / "src" / "roster" / "dispatcher.py").is_file())
        self.assertTrue((self.paths.app / "vendor" / "airportsdata" / "__init__.py").is_file())
        self.assertFalse((self.paths.app / "tests").exists())
        self.assertFalse((self.paths.app / ".git").exists())
        self.assertFalse((self.paths.app / "runtime").exists())
        self.assertFalse((self.paths.app / ".env").exists())

    def test_runtime_state_private_and_env_survive_redeployment(self):
        self.paths.runtime.mkdir(parents=True)
        self.paths.private.mkdir()
        self.paths.database.write_bytes(b"stable database history")
        private_file = self.paths.private / "preserve.txt"
        private_file.write_text("private", encoding="utf-8")
        runtime_env = self.paths.root / ".env"
        runtime_env.write_text("LOCAL_SETTING=preserved\n", encoding="utf-8")
        deployer = self.deployer()
        deployer.deploy()
        deployer.deploy()
        self.assertEqual(self.paths.database.read_bytes(), b"stable database history")
        self.assertEqual(private_file.read_text(encoding="utf-8"), "private")
        self.assertEqual(
            runtime_env.read_text(encoding="utf-8"),
            "LOCAL_SETTING=preserved\n",
        )
        self.assertEqual(self.paths.root.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.paths.runtime.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.paths.private.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.paths.database.stat().st_mode & 0o777, 0o600)
        self.assertEqual(runtime_env.stat().st_mode & 0o777, 0o600)

    def test_plist_uses_only_absolute_application_support_paths(self):
        self.deployer().deploy()
        launchctl = FakeLaunchctl()
        scheduler = LaunchdScheduler(
            database_path=self.paths.database,
            workflow_dir=self.paths.root,
            plist_path=self.temporary / "LaunchAgents" / PLIST_FILENAME,
            command=[str(self.paths.python), "-m", "roster.cli"],
            python_paths=self.paths.python_paths,
            log_path=self.paths.dispatcher_log,
            runner=launchctl,
        )
        value = plistlib.loads(scheduler.generate_planner_plist())
        rendered = scheduler.generate_planner_plist().decode("utf-8")
        self.assertTrue(Path(value["ProgramArguments"][0]).is_absolute())
        self.assertTrue(Path(value["WorkingDirectory"]).is_absolute())
        self.assertTrue(Path(value["EnvironmentVariables"]["WIFE_ROSTER_DB"]).is_absolute())
        self.assertIn("Application Support/wife-roster", rendered)
        self.assertNotIn("Documents", rendered)
        status = scheduler.refresh()
        self.assertTrue(status.loaded)


if __name__ == "__main__":
    unittest.main()
