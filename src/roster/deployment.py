from __future__ import annotations

from dataclasses import dataclass
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import tomllib
from typing import Callable
import venv


APPLICATION_SUPPORT_NAME = "wife-roster"
RUNTIME_PYTHON_MARKER = ".wife-roster-python"
APP_FILES = ("pyproject.toml", "README.md", "SPEC.md", ".env.example")


class DeploymentError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class DeploymentPaths:
    root: Path
    app: Path
    runtime: Path
    database: Path
    logs: Path
    private: Path
    venv: Path

    @property
    def python(self) -> Path:
        return self.venv / "bin" / "python"

    @property
    def python_paths(self) -> tuple[Path, Path]:
        return self.app / "src", self.app / "vendor"

    @property
    def dispatcher_log(self) -> Path:
        return self.logs / "dispatcher.log"

    @classmethod
    def for_root(cls, root: str | Path) -> "DeploymentPaths":
        resolved = Path(root).expanduser().resolve()
        runtime = resolved / "runtime"
        return cls(
            root=resolved,
            app=resolved / "app",
            runtime=runtime,
            database=runtime / "state.db",
            logs=runtime / "logs",
            private=runtime / "private",
            venv=resolved / "venv",
        )

    @classmethod
    def default(cls) -> "DeploymentPaths":
        configured = os.environ.get("WIFE_ROSTER_DEPLOY_ROOT", "").strip()
        root = (
            Path(configured).expanduser()
            if configured
            else Path.home() / "Library" / "Application Support" / APPLICATION_SUPPORT_NAME
        )
        return cls.for_root(root)


@dataclass(frozen=True, slots=True)
class DeploymentResult:
    workspace: Path
    paths: DeploymentPaths
    verified: bool


class Deployer:
    def __init__(
        self,
        *,
        workspace: str | Path,
        paths: DeploymentPaths | None = None,
        dependency_locator: Callable[[str], Path] | None = None,
        require_git: bool = True,
    ) -> None:
        self.workspace = resolve_workspace(workspace, require_git=require_git)
        self.paths = paths or DeploymentPaths.default()
        self.dependency_locator = dependency_locator or locate_package

    def deploy(self) -> DeploymentResult:
        self._create_runtime_directories()
        self._ensure_venv()
        staging = Path(tempfile.mkdtemp(prefix=".app-staging-", dir=self.paths.root))
        backup = self.paths.root / ".app-previous"
        try:
            self._copy_application(staging)
            self._verify(staging)
            if backup.exists():
                shutil.rmtree(backup)
            if self.paths.app.exists():
                self.paths.app.replace(backup)
            staging.replace(self.paths.app)
            try:
                self._verify(self.paths.app)
            except Exception:
                shutil.rmtree(self.paths.app, ignore_errors=True)
                if backup.exists():
                    backup.replace(self.paths.app)
                raise
            if backup.exists():
                shutil.rmtree(backup)
            self._harden_permissions()
        finally:
            if staging.exists():
                shutil.rmtree(staging)
        return DeploymentResult(self.workspace, self.paths, True)

    def _create_runtime_directories(self) -> None:
        self.paths.root.mkdir(parents=True, exist_ok=True)
        self.paths.runtime.mkdir(exist_ok=True)
        self.paths.logs.mkdir(exist_ok=True)
        self.paths.private.mkdir(exist_ok=True)
        self._harden_permissions()

    def _harden_permissions(self) -> None:
        for directory in (
            self.paths.root,
            self.paths.runtime,
            self.paths.logs,
            self.paths.private,
        ):
            if directory.exists():
                directory.chmod(0o700)
        sensitive_files = (
            self.paths.database,
            self.paths.root / ".env",
            self.paths.logs / "dispatcher.log",
            self.paths.logs / "planner.log",
            self.paths.private / "schedule.request",
        )
        for path in sensitive_files:
            if path.is_file():
                path.chmod(0o600)

    def _ensure_venv(self) -> None:
        expected = f"{sys.version_info.major}.{sys.version_info.minor}"
        marker = self.paths.venv / RUNTIME_PYTHON_MARKER
        current = marker.read_text(encoding="utf-8").strip() if marker.is_file() else None
        if self.paths.python.is_file() and current == expected:
            return
        if self.paths.venv.exists():
            shutil.rmtree(self.paths.venv)
        venv.EnvBuilder(with_pip=False, clear=False, symlinks=True).create(self.paths.venv)
        marker.write_text(expected + "\n", encoding="utf-8")

    def _copy_application(self, destination: Path) -> None:
        source_package = self.workspace / "src" / "roster"
        shutil.copytree(
            source_package,
            destination / "src" / "roster",
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
        )
        prompts = self.workspace / "prompts"
        if prompts.is_dir():
            shutil.copytree(prompts, destination / "prompts")
        for name in APP_FILES:
            source = self.workspace / name
            if source.is_file():
                shutil.copy2(source, destination / name)
        dependency = self.dependency_locator("airportsdata")
        vendor = destination / "vendor"
        vendor.mkdir()
        if dependency.is_dir():
            shutil.copytree(
                dependency,
                vendor / dependency.name,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
            )
        else:
            shutil.copy2(dependency, vendor / dependency.name)

    def _verify(self, app_path: Path) -> None:
        python_paths = (app_path / "src", app_path / "vendor")
        environment = os.environ.copy()
        environment["PYTHONPATH"] = os.pathsep.join(str(path) for path in python_paths)
        environment["WIFE_ROSTER_DB"] = str(self.paths.database)
        completed = subprocess.run(
            [
                str(self.paths.python),
                "-c",
                (
                    "import airportsdata, roster; "
                    "from roster.cli import build_parser; "
                    "assert build_parser().prog == 'roster'; "
                    "print(roster.__version__)"
                ),
            ],
            cwd=self.paths.root,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
            timeout=60,
        )
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip() or "unknown verification error"
            raise DeploymentError(f"runtime verification failed: {detail}")


def resolve_workspace(start: str | Path, *, require_git: bool = True) -> Path:
    current = Path(start).expanduser().resolve()
    if current.is_file():
        current = current.parent
    candidates = (current, *current.parents)
    for candidate in candidates:
        pyproject = candidate / "pyproject.toml"
        package = candidate / "src" / "roster"
        if not pyproject.is_file() or not package.is_dir():
            continue
        try:
            project = tomllib.loads(pyproject.read_text(encoding="utf-8")).get("project", {})
        except (OSError, tomllib.TOMLDecodeError):
            continue
        if project.get("name") != "wife-roster":
            continue
        if require_git:
            completed = subprocess.run(
                ["git", "-C", str(candidate), "rev-parse", "--show-toplevel"],
                text=True,
                capture_output=True,
                check=False,
            )
            if completed.returncode != 0:
                raise DeploymentError("wife-roster development directory is not inside a Git workspace")
        return candidate
    raise DeploymentError("could not identify the wife-roster development workspace")


def locate_package(name: str) -> Path:
    spec = importlib.util.find_spec(name)
    if spec is None:
        raise DeploymentError(
            f"runtime dependency {name!r} is unavailable; run deploy from the project virtual environment"
        )
    if spec.submodule_search_locations:
        return Path(next(iter(spec.submodule_search_locations))).resolve()
    if spec.origin:
        return Path(spec.origin).resolve()
    raise DeploymentError(f"could not locate runtime dependency {name!r}")
