from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
from typing import Any, Protocol, Sequence

from .models import RawTranscription


SUPPORTED_IMAGES = {".jpg", ".jpeg", ".png"}
SUPPORTED_SOURCES = SUPPORTED_IMAGES | {".pdf", ".json"}


class ExtractionError(RuntimeError):
    pass


class Transcriber(Protocol):
    def transcribe(
        self,
        sources: Sequence[Path],
        pdf_text_layers: dict[int, str],
        prompt_path: Path,
    ) -> dict[str, Any]: ...


class ExternalCommandTranscriber:
    """Thin JSON stdin/stdout boundary for a future local vision integration."""

    def __init__(self, command: str) -> None:
        self.command = command

    def transcribe(
        self,
        sources: Sequence[Path],
        pdf_text_layers: dict[int, str],
        prompt_path: Path,
    ) -> dict[str, Any]:
        request = {
            "schema_version": 1,
            "sources": [str(path.resolve()) for path in sources],
            "pdf_text_layers": {str(key): value for key, value in pdf_text_layers.items()},
            "visual_source_indexes": [
                index
                for index, path in enumerate(sources)
                if path.suffix.lower() in SUPPORTED_IMAGES
                or (path.suffix.lower() == ".pdf" and index not in pdf_text_layers)
            ],
            "prompt_path": str(prompt_path.resolve()),
        }
        try:
            completed = subprocess.run(
                shlex.split(self.command),
                input=json.dumps(request),
                text=True,
                capture_output=True,
                check=False,
                timeout=300,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ExtractionError(f"transcriber could not run: {exc}") from exc
        if completed.returncode != 0:
            detail = completed.stderr.strip() or f"exit status {completed.returncode}"
            raise ExtractionError(f"transcriber failed: {detail}")
        try:
            result = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise ExtractionError("transcriber returned invalid JSON") from exc
        if not isinstance(result, dict):
            raise ExtractionError("transcriber result must be a JSON object")
        return result


def extract_candidate(
    sources: Sequence[str | Path],
    *,
    transcription_path: str | Path | None = None,
    transcriber: Transcriber | None = None,
    prompt_path: str | Path | None = None,
) -> RawTranscription:
    paths = tuple(Path(source) for source in sources)
    if not paths:
        raise ExtractionError("at least one roster source is required")
    _validate_sources(paths)
    if transcription_path is not None:
        return load_transcription(Path(transcription_path))
    json_sources = [path for path in paths if path.suffix.lower() == ".json"]
    non_json_sources = [path for path in paths if path.suffix.lower() != ".json"]
    if len(json_sources) == 1:
        # A JSON source can accompany the real files in deterministic/offline
        # workflows; all files still participate in the ingestion file hash.
        return load_transcription(json_sources[0])
    if len(json_sources) > 1:
        raise ExtractionError("submit exactly one candidate transcription JSON")
    sidecar = _find_sidecar(non_json_sources)
    if sidecar is not None:
        return load_transcription(sidecar)
    pdf_text_layers = {
        index: text
        for index, path in enumerate(paths)
        if path.suffix.lower() == ".pdf"
        and (text := extract_pdf_text_layer(path)) is not None
    }
    if transcriber is None:
        command = os.environ.get("WIFE_ROSTER_TRANSCRIBER", "").strip()
        if command:
            transcriber = ExternalCommandTranscriber(command)
    if transcriber is None:
        source_kind = "PDF/image" if non_json_sources else "source"
        raise ExtractionError(
            f"{source_kind} transcription backend is not configured; provide "
            "--transcription JSON or set WIFE_ROSTER_TRANSCRIBER"
        )
    prompt = Path(prompt_path) if prompt_path else _default_prompt_path()
    return RawTranscription.from_dict(transcriber.transcribe(paths, pdf_text_layers, prompt))


def load_transcription(path: Path) -> RawTranscription:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ExtractionError(f"transcription not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ExtractionError(f"invalid transcription JSON: {exc}") from exc
    try:
        return RawTranscription.from_dict(value)
    except ValueError as exc:
        raise ExtractionError(f"transcription schema error: {exc}") from exc


def extract_pdf_text_layer(path: Path) -> str | None:
    executable = shutil.which("pdftotext")
    if executable is None:
        return None
    try:
        completed = subprocess.run(
            [executable, "-layout", str(path), "-"],
            text=True,
            capture_output=True,
            check=False,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0 or not _usable_roster_text(completed.stdout):
        return None
    return completed.stdout


def hash_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def hash_file_set(paths: Sequence[str | Path]) -> tuple[str, list[tuple[Path, str, int]]]:
    files: list[tuple[Path, str, int]] = []
    for source in paths:
        path = Path(source)
        digest = hash_file(path)
        files.append((path, digest, path.stat().st_size))
    canonical = sorted((digest, size) for _, digest, size in files)
    payload = json.dumps(canonical, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest(), files


def _validate_sources(paths: Sequence[Path]) -> None:
    for path in paths:
        if not path.is_file():
            raise ExtractionError(f"source not found: {path}")
        if path.suffix.lower() not in SUPPORTED_SOURCES:
            raise ExtractionError(f"unsupported source type: {path.suffix or '(none)'}")


def _find_sidecar(paths: Sequence[Path]) -> Path | None:
    candidates: list[Path] = []
    if paths:
        candidates.append(paths[0].with_suffix(paths[0].suffix + ".transcription.json"))
        if all(path.parent == paths[0].parent for path in paths):
            candidates.append(paths[0].parent / "roster-transcription.json")
    existing = [path for path in candidates if path.is_file()]
    if len(existing) > 1:
        raise ExtractionError("multiple automatic transcription sidecars found")
    return existing[0] if existing else None


def _usable_roster_text(text: str) -> bool:
    normalized = text.upper()
    signals = sum(token in normalized for token in ("RPT", "STD", "STA", "FLY"))
    return len(text.strip()) >= 80 and signals >= 2


def _default_prompt_path() -> Path:
    return Path(__file__).resolve().parents[2] / "prompts" / "roster_transcription.md"
