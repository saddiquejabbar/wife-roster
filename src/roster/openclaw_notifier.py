from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
from typing import Any, Callable

from .models import Alert


DEFAULT_OPENCLAW_BIN = Path.home() / ".openclaw" / "bin" / "openclaw"
DEFAULT_TIMEOUT_SECONDS = 30.0


class OpenClawError(RuntimeError):
    """A controlled error that does not expose delivery configuration."""


class OpenClawConfigurationError(OpenClawError):
    pass


class OpenClawDeliveryError(OpenClawError):
    pass


Runner = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(frozen=True, slots=True)
class OpenClawConfig:
    executable: Path
    account: str = field(repr=False)
    target: str = field(repr=False)
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        executable = self.executable.expanduser()
        account = self.account.strip()
        target = self.target.strip()
        object.__setattr__(self, "executable", executable)
        object.__setattr__(self, "account", account)
        object.__setattr__(self, "target", target)
        if not executable.is_absolute():
            raise OpenClawConfigurationError(
                "WIFE_ROSTER_OPENCLAW_BIN must be an absolute executable path"
            )
        if not executable.is_file():
            raise OpenClawConfigurationError(
                "WIFE_ROSTER_OPENCLAW_BIN does not identify an executable file"
            )
        if not os.access(executable, os.X_OK):
            raise OpenClawConfigurationError(
                "WIFE_ROSTER_OPENCLAW_BIN does not identify an executable file"
            )
        if not account:
            raise OpenClawConfigurationError(
                "WIFE_ROSTER_OPENCLAW_ACCOUNT is required"
            )
        if not target:
            raise OpenClawConfigurationError(
                "WIFE_ROSTER_OPENCLAW_TARGET is required"
            )
        if self.timeout_seconds <= 0:
            raise OpenClawConfigurationError(
                "OpenClaw delivery timeout must be greater than zero"
            )

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str] | None = None,
        *,
        env_file: str | Path | None = None,
    ) -> "OpenClawConfig":
        values = dict(environment if environment is not None else os.environ)
        file_values = read_openclaw_env(
            env_file or default_runtime_env_path(values)
        )

        def configured(name: str, default: str = "") -> str:
            if name in values:
                return values[name].strip()
            if name in file_values:
                return file_values[name].strip()
            return default

        return cls(
            executable=Path(
                configured("WIFE_ROSTER_OPENCLAW_BIN", str(DEFAULT_OPENCLAW_BIN))
            ),
            account=configured("WIFE_ROSTER_OPENCLAW_ACCOUNT"),
            target=configured("WIFE_ROSTER_OPENCLAW_TARGET"),
        )


class OpenClawNotifier:
    def __init__(
        self,
        config: OpenClawConfig,
        *,
        runner: Runner | None = None,
    ) -> None:
        self.config = config
        self.runner = runner or subprocess.run

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str] | None = None,
        *,
        env_file: str | Path | None = None,
    ) -> "OpenClawNotifier":
        return cls(OpenClawConfig.from_environment(environment, env_file=env_file))

    def send(self, alert: Alert, *, sent_at: datetime) -> str:
        del sent_at  # The existing deterministic alert body is sent unchanged.
        arguments = [
            str(self.config.executable),
            "message",
            "send",
            "--channel",
            "telegram",
            "--account",
            self.config.account,
            "--target",
            self.config.target,
            "--message",
            alert.message,
            "--json",
        ]
        if any("\x00" in argument for argument in arguments):
            raise OpenClawDeliveryError("OpenClaw delivery input was invalid")
        try:
            result = self.runner(
                arguments,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=self.config.timeout_seconds,
                shell=False,
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise OpenClawDeliveryError("OpenClaw delivery timed out") from None
        except OSError:
            raise OpenClawDeliveryError("OpenClaw delivery command could not run") from None
        if result.returncode != 0:
            raise OpenClawDeliveryError("OpenClaw delivery command failed")
        response = _parse_response(result.stdout)
        message_id = _confirmed_message_id(response)
        return f"openclaw:telegram:{message_id}"


def _parse_response(raw: str) -> Mapping[str, Any]:
    try:
        response = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        raise OpenClawDeliveryError("OpenClaw returned an invalid response") from None
    if not isinstance(response, dict):
        raise OpenClawDeliveryError("OpenClaw returned an invalid response")
    return response


def _confirmed_message_id(response: Mapping[str, Any]) -> str:
    if (
        response.get("action") != "send"
        or response.get("channel") != "telegram"
        or response.get("dryRun") is not False
    ):
        raise OpenClawDeliveryError("OpenClaw response did not confirm delivery")
    message_id = response.get("messageId")
    if isinstance(message_id, bool):
        message_id = None
    if isinstance(message_id, int) and message_id > 0:
        return str(message_id)
    if isinstance(message_id, str) and message_id.isdigit() and int(message_id) > 0:
        return message_id
    raise OpenClawDeliveryError("OpenClaw response did not confirm delivery")


def default_runtime_env_path(
    environment: Mapping[str, str] | None = None,
) -> Path:
    values = environment if environment is not None else os.environ
    configured = values.get("WIFE_ROSTER_DEPLOY_ROOT", "").strip()
    root = (
        Path(configured).expanduser()
        if configured
        else Path.home() / "Library" / "Application Support" / "wife-roster"
    )
    return root / ".env"


def read_openclaw_env(path: str | Path) -> dict[str, str]:
    env_path = Path(path).expanduser()
    if not env_path.is_file():
        return {}
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        raise OpenClawConfigurationError(
            "OpenClaw environment file could not be read"
        ) from None
    allowed = {
        "WIFE_ROSTER_OPENCLAW_BIN",
        "WIFE_ROSTER_OPENCLAW_ACCOUNT",
        "WIFE_ROSTER_OPENCLAW_TARGET",
    }
    values: dict[str, str] = {}
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip()
        if name not in allowed:
            continue
        cleaned = value.strip()
        if len(cleaned) >= 2 and cleaned[0] == cleaned[-1] and cleaned[0] in {'"', "'"}:
            cleaned = cleaned[1:-1]
        values[name] = cleaned
    return values
