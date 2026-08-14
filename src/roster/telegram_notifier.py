from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
import json
import os
from pathlib import Path
import socket
import time
from typing import Any, Callable, Protocol
from urllib import error, parse, request

from .models import Alert


TELEGRAM_API_ROOT = "https://api.telegram.org"
DEFAULT_TIMEOUT_SECONDS = 15.0
DEFAULT_RETRY_DELAYS = (10.0, 30.0)


class TelegramError(RuntimeError):
    """A controlled, credential-safe Telegram error."""


class TelegramConfigurationError(TelegramError):
    pass


class TelegramDeliveryError(TelegramError):
    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


class TelegramTransport(Protocol):
    def post(
        self,
        url: str,
        payload: Mapping[str, str],
        *,
        timeout: float,
    ) -> Mapping[str, Any]: ...


@dataclass(frozen=True, slots=True)
class TelegramConfig:
    bot_token: str
    chat_ids: tuple[str, ...]
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str] | None = None,
        *,
        env_file: str | Path | None = None,
    ) -> "TelegramConfig":
        values = dict(environment if environment is not None else os.environ)
        file_values = read_telegram_env(env_file or default_runtime_env_path())
        token = values.get("TELEGRAM_BOT_TOKEN", "").strip() or file_values.get(
            "TELEGRAM_BOT_TOKEN", ""
        ).strip()
        raw_chat_ids = values.get("TELEGRAM_CHAT_IDS", "").strip() or file_values.get(
            "TELEGRAM_CHAT_IDS", ""
        ).strip()
        if not token:
            raise TelegramConfigurationError("TELEGRAM_BOT_TOKEN is required")
        if not raw_chat_ids:
            raise TelegramConfigurationError("TELEGRAM_CHAT_IDS is required")
        chat_ids = tuple(dict.fromkeys(part.strip() for part in raw_chat_ids.split(",") if part.strip()))
        if not chat_ids:
            raise TelegramConfigurationError("TELEGRAM_CHAT_IDS must contain a recipient")
        return cls(bot_token=token, chat_ids=chat_ids)


class UrllibTelegramTransport:
    def post(
        self,
        url: str,
        payload: Mapping[str, str],
        *,
        timeout: float,
    ) -> Mapping[str, Any]:
        encoded = parse.urlencode(payload).encode("utf-8")
        telegram_request = request.Request(
            url,
            data=encoded,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        try:
            with request.urlopen(telegram_request, timeout=timeout) as response:
                body = response.read()
        except error.HTTPError as exc:
            retryable = exc.code == 429 or exc.code >= 500
            raise TelegramDeliveryError(
                f"Telegram request failed with HTTP {exc.code}",
                retryable=retryable,
            ) from None
        except (error.URLError, TimeoutError, socket.timeout):
            raise TelegramDeliveryError("Telegram request timed out or was unavailable", retryable=True) from None
        try:
            value = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise TelegramDeliveryError("Telegram returned an invalid response", retryable=True) from None
        if not isinstance(value, dict):
            raise TelegramDeliveryError("Telegram returned an invalid response", retryable=True)
        return value


class TelegramNotifier:
    def __init__(
        self,
        config: TelegramConfig,
        *,
        transport: TelegramTransport | None = None,
        sleeper: Callable[[float], None] | None = None,
        retry_delays: Sequence[float] = DEFAULT_RETRY_DELAYS,
    ) -> None:
        if len(retry_delays) != 2 or any(delay < 0 for delay in retry_delays):
            raise ValueError("Telegram retry_delays must contain two non-negative delays")
        self.config = config
        self.transport = transport or UrllibTelegramTransport()
        self.sleeper = sleeper or time.sleep
        self.retry_delays = tuple(float(delay) for delay in retry_delays)

    @classmethod
    def from_environment(cls) -> "TelegramNotifier":
        return cls(TelegramConfig.from_environment())

    def send(self, alert: Alert, *, sent_at: datetime) -> str:
        del sent_at  # Telegram messages must contain only the existing alert body.
        message_ids: list[str] = []
        for chat_id in self.config.chat_ids:
            response = self._send_with_retry(chat_id, alert.message)
            result = response.get("result")
            message_id = result.get("message_id") if isinstance(result, dict) else None
            if not isinstance(message_id, int):
                raise TelegramDeliveryError("Telegram response did not confirm delivery", retryable=True)
            message_ids.append(str(message_id))
        return "telegram:" + ",".join(message_ids)

    def _send_with_retry(self, chat_id: str, message: str) -> Mapping[str, Any]:
        url = f"{TELEGRAM_API_ROOT}/bot{self.config.bot_token}/sendMessage"
        payload = {"chat_id": chat_id, "text": message}
        for attempt in range(3):
            try:
                response = self.transport.post(
                    url,
                    payload,
                    timeout=self.config.timeout_seconds,
                )
                return self._confirmed_response(response)
            except TelegramDeliveryError as exc:
                if not exc.retryable or attempt == 2:
                    message = (
                        "Telegram delivery failed after 3 attempts"
                        if exc.retryable
                        else "Telegram delivery was rejected"
                    )
                    raise TelegramDeliveryError(message, retryable=False) from None
            except Exception:
                if attempt == 2:
                    raise TelegramDeliveryError("Telegram delivery failed after 3 attempts") from None
            self.sleeper(self.retry_delays[attempt])
        raise TelegramDeliveryError("Telegram delivery failed after 3 attempts")

    @staticmethod
    def _confirmed_response(response: Mapping[str, Any]) -> Mapping[str, Any]:
        if response.get("ok") is True:
            result = response.get("result")
            if not isinstance(result, dict) or not isinstance(result.get("message_id"), int):
                raise TelegramDeliveryError(
                    "Telegram response did not confirm delivery",
                    retryable=True,
                )
            return response
        error_code = response.get("error_code")
        safe_code = str(error_code) if isinstance(error_code, int) else "unknown"
        retryable = error_code == 429 or (isinstance(error_code, int) and error_code >= 500)
        raise TelegramDeliveryError(
            f"Telegram API rejected the request ({safe_code})",
            retryable=retryable,
        )


def default_runtime_env_path() -> Path:
    configured = os.environ.get("WIFE_ROSTER_DEPLOY_ROOT", "").strip()
    root = (
        Path(configured).expanduser()
        if configured
        else Path.home() / "Library" / "Application Support" / "wife-roster"
    )
    return root / ".env"


def read_telegram_env(path: str | Path) -> dict[str, str]:
    env_path = Path(path).expanduser()
    if not env_path.is_file():
        return {}
    values: dict[str, str] = {}
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        raise TelegramConfigurationError("Telegram environment file could not be read") from None
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip()
        if name not in {"TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_IDS"}:
            continue
        cleaned = value.strip()
        if len(cleaned) >= 2 and cleaned[0] == cleaned[-1] and cleaned[0] in {'"', "'"}:
            cleaned = cleaned[1:-1]
        values[name] = cleaned
    return values
