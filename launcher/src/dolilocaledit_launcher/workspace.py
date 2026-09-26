"""Private recovery workspaces and stable upload snapshots."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import unicodedata
import uuid

from .errors import WorkspaceError


_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]{0,199}$")
_SAFE_EXTENSION = re.compile(r"^\.[A-Za-z0-9]{1,15}$")


@dataclass
class RecoveryWorkspace:
    directory: Path
    document: Path
    manifest: Path

    @classmethod
    def create(
        cls,
        root: Path,
        filename: str,
        origin: str,
        entity: int,
        session_uuid: str,
    ) -> "RecoveryWorkspace":
        _ensure_private_root(root)
        directory = root / str(uuid.uuid4())
        directory.mkdir(mode=0o700)
        safe_name = safe_local_filename(filename)
        workspace = cls(directory, directory / safe_name, directory / "recovery.json")
        workspace._write_manifest({
            "schema": 1,
            "status": "downloading",
            "origin": origin,
            "entity": entity,
            "session_uuid": session_uuid,
            "filename": safe_name,
        })
        return workspace

    def download_path(self) -> Path:
        return self.directory / ".download"

    def finish_download(self, temporary: Path, sha256: str) -> None:
        if temporary.parent != self.directory or not temporary.is_file() or not _is_sha256(sha256):
            raise WorkspaceError("download_invalid", "Le fichier téléchargé est invalide.")
        os.replace(temporary, self.document)
        try:
            self.document.chmod(0o600)
        except OSError:
            pass
        self.update_status("editing", sha256=sha256)

    def stable_snapshot(self) -> tuple[Path, str] | None:
        """Copy one unchanged revision for upload; return None if a save races the copy."""
        try:
            before = _file_signature(self.document)
        except FileNotFoundError:
            return None
        snapshot = self.directory / ".upload"
        try:
            snapshot.unlink()
        except FileNotFoundError:
            pass
        digest = hashlib.sha256()
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(snapshot, flags, 0o600)
        try:
            with os.fdopen(descriptor, "wb", closefd=True) as target, _open_regular_readonly(self.document) as source:
                while True:
                    chunk = source.read(65_536)
                    if not chunk:
                        break
                    digest.update(chunk)
                    target.write(chunk)
                target.flush()
                os.fsync(target.fileno())
            after = _file_signature(self.document)
        except FileNotFoundError:
            _safe_unlink(snapshot)
            return None
        except BaseException:
            _safe_unlink(snapshot)
            raise
        if before != after:
            _safe_unlink(snapshot)
            return None
        return snapshot, digest.hexdigest()

    def update_status(self, status_value: str, **metadata: object) -> None:
        current: dict[str, object] = {}
        try:
            loaded = json.loads(self.manifest.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                current = loaded
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            pass
        current.update(metadata)
        current["status"] = status_value
        self._write_manifest(current)

    def discard(self) -> None:
        try:
            uuid.UUID(self.directory.name)
            details = self.directory.lstat()
        except (OSError, ValueError) as exc:
            raise WorkspaceError("workspace_invalid", "Le dossier de reprise est invalide.") from exc
        if self.directory.parent == self.directory or stat.S_ISLNK(details.st_mode) or not stat.S_ISDIR(details.st_mode):
            raise WorkspaceError("workspace_invalid", "Le dossier de reprise est invalide.")
        shutil.rmtree(self.directory)

    def _write_manifest(self, payload: dict[str, object]) -> None:
        temporary = self.manifest.with_suffix(".tmp")
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(temporary, flags, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", closefd=True) as stream:
                json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.manifest)
        except BaseException:
            _safe_unlink(temporary)
            raise


def safe_local_filename(filename: str) -> str:
    normalized = unicodedata.normalize("NFKC", filename)
    windows_device = normalized.split(".", 1)[0].rstrip(" ").upper()
    if (
        not normalized
        or normalized in {".", ".."}
        or Path(normalized).name != normalized
        or not _SAFE_NAME.fullmatch(normalized)
        or normalized.endswith((".", " "))
        or windows_device in {"CON", "PRN", "AUX", "NUL"}
        or re.fullmatch(r"(?:COM|LPT)[1-9]", windows_device)
        or normalized.lower() in {"recovery.json", "recovery.tmp"}
    ):
        extension = Path(normalized).suffix.lower()
        return "document" + (extension if _SAFE_EXTENSION.fullmatch(extension) else ".bin")
    return normalized


def file_signature(path: Path) -> tuple[int, int, int]:
    return _file_signature(path)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with _open_regular_readonly(path) as stream:
        while True:
            chunk = stream.read(65_536)
            if not chunk:
                return digest.hexdigest()
            digest.update(chunk)


def _ensure_private_root(root: Path) -> None:
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    details = root.lstat()
    if stat.S_ISLNK(details.st_mode) or not stat.S_ISDIR(details.st_mode):
        raise WorkspaceError("recovery_invalid", "Le dossier de reprise local est invalide.")
    if os.name == "posix":
        if details.st_uid != os.getuid():
            raise WorkspaceError("recovery_owner", "Le dossier de reprise doit appartenir à l’utilisateur courant.")
        root.chmod(0o700)


def _file_signature(path: Path) -> tuple[int, int, int]:
    details = path.lstat()
    if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode):
        raise WorkspaceError("document_invalid", "Le document local n’est plus un fichier ordinaire.")
    return details.st_ino, details.st_size, details.st_mtime_ns


def _open_regular_readonly(path: Path):
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    details = os.fstat(descriptor)
    if not stat.S_ISREG(details.st_mode):
        os.close(descriptor)
        raise WorkspaceError("document_invalid", "Le document local n’est plus un fichier ordinaire.")
    return os.fdopen(descriptor, "rb", closefd=True)


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(char in "0123456789abcdef" for char in value)


def _safe_unlink(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass
