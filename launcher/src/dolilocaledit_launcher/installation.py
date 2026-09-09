"""Per-user installation of a frozen launcher without administrator rights."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import tempfile

from .errors import ConfigurationError
from .registration import register_protocol, unregister_linux_protocol


_VERSION_PATTERN = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
_SHA256_PATTERN = re.compile(r"[a-f0-9]{64}")
_VERSIONED_WINDOWS_LAUNCHER = re.compile(
    r"dolilocaledit-launcher-[0-9]+\.[0-9]+\.[0-9]+-[a-f0-9]{64}\.exe",
    re.IGNORECASE,
)
_LEGACY_WINDOWS_LAUNCHER = "dolilocaledit-launcher.exe"


@dataclass(frozen=True)
class RunningLauncher:
    """One previous launcher process that may still own an editing session."""

    pid: int
    path: Path


@dataclass(frozen=True)
class InstallationResult:
    """Installed target and previous Windows instances retained for safety."""

    path: Path
    running_previous_instances: tuple[RunningLauncher, ...] = ()
    retained_previous_files: tuple[Path, ...] = ()


def default_windows_install_directory() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    return base / "Programs" / "DoliLocalEdit"


def default_windows_install_path(version: str, executable_sha256: str) -> Path:
    """Return an immutable per-release target so active workers never block upgrades."""
    if _VERSION_PATTERN.fullmatch(version) is None or _SHA256_PATTERN.fullmatch(executable_sha256) is None:
        raise ConfigurationError("installation_version_invalid", "La version du lanceur est invalide.")
    return default_windows_install_directory() / f"dolilocaledit-launcher-{version}-{executable_sha256}.exe"


def default_linux_install_path() -> Path:
    return Path.home() / ".local" / "bin" / "dolilocaledit-launcher"


def install_for_current_user(source: Path | None = None, version: str | None = None) -> InstallationResult:
    if sys.platform != "win32" and not sys.platform.startswith("linux"):
        raise ConfigurationError(
            "installation_unsupported",
            "L’installation intégrée est disponible sous Windows et Linux.",
        )
    candidate = source or Path(sys.executable if getattr(sys, "frozen", False) else sys.argv[0])
    try:
        source_path = candidate.expanduser().resolve(strict=True)
        details = source_path.stat()
    except OSError as exc:
        raise ConfigurationError("executable_invalid", "L’exécutable du lanceur est introuvable.") from exc
    if not stat.S_ISREG(details.st_mode):
        raise ConfigurationError("executable_invalid", "L’exécutable du lanceur est invalide.")

    source_sha256 = _file_sha256(source_path)
    if sys.platform == "win32":
        target = default_windows_install_path(version or "", source_sha256)
    else:
        target = default_linux_install_path()

    try:
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as exc:
        raise ConfigurationError(
            "installation_failed",
            _system_error_message("Le dossier d’installation est inaccessible", exc),
        ) from exc
    if source_path != target:
        if target.exists():
            try:
                target_details = target.lstat()
                target_sha256 = _file_sha256(target)
            except OSError as exc:
                raise ConfigurationError(
                    "installation_failed",
                    _system_error_message("La version déjà installée ne peut pas être vérifiée", exc),
                ) from exc
            if stat.S_ISLNK(target_details.st_mode) or not stat.S_ISREG(target_details.st_mode):
                raise ConfigurationError("installation_target_invalid", "La cible d’installation est invalide.")
            if target_sha256 != source_sha256:
                raise ConfigurationError(
                    "installation_target_invalid",
                    "Un exécutable inattendu utilise déjà le nom sécurisé de cette version.",
                )
        else:
            _copy_launcher(source_path, target, source_sha256)
    if os.name == "posix":
        try:
            os.chmod(target, 0o700)
        except OSError as exc:
            raise ConfigurationError(
                "installation_failed",
                _system_error_message("Les permissions du lanceur sont invalides", exc),
            ) from exc
    try:
        register_protocol(target)
    except ConfigurationError:
        raise
    except OSError as exc:
        raise ConfigurationError(
            "registration_failed",
            "L’enregistrement du protocole a échoué.",
        ) from exc
    if sys.platform == "win32":
        previous = _previous_windows_launchers(target)
        running = find_running_windows_launchers(previous)
        retained = cleanup_obsolete_windows_launchers(target, previous)
        return InstallationResult(target, running, retained)
    return InstallationResult(target)


def cleanup_obsolete_windows_launchers(
    active_target: Path,
    candidates: tuple[Path, ...] | None = None,
) -> tuple[Path, ...]:
    """Remove only known obsolete launcher names, retaining files still used by Windows."""
    if sys.platform != "win32":
        return ()
    try:
        active_path = active_target.resolve(strict=True)
    except OSError:
        return ()
    install_directory = default_windows_install_directory()
    try:
        if active_path.parent.resolve(strict=True) != install_directory.resolve(strict=True):
            return ()
    except OSError:
        return ()
    obsolete = candidates if candidates is not None else _previous_windows_launchers(active_path)
    retained: list[Path] = []
    for path in obsolete:
        try:
            details = path.lstat()
            if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode):
                continue
            path.unlink()
        except FileNotFoundError:
            continue
        except OSError:
            retained.append(path)
    return tuple(retained)


def find_running_windows_launchers(candidates: tuple[Path, ...]) -> tuple[RunningLauncher, ...]:
    """List processes executing exact previous launcher paths without reading command lines."""
    if sys.platform != "win32" or not candidates:
        return ()
    candidate_names = {_normalized_windows_path(path): path for path in candidates}
    try:
        return _snapshot_windows_processes(candidate_names)
    except (OSError, ValueError):
        return ()


def _copy_launcher(source_path: Path, target: Path, expected_sha256: str) -> None:
    descriptor = -1
    temporary: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=target.name + ".",
            suffix=".new",
            dir=target.parent,
        )
        temporary = Path(temporary_name)
        if os.name == "posix":
            os.fchmod(descriptor, 0o700)
        with source_path.open("rb") as source_stream, os.fdopen(descriptor, "wb", closefd=True) as target_stream:
            descriptor = -1
            shutil.copyfileobj(source_stream, target_stream, length=1_048_576)
            target_stream.flush()
            os.fsync(target_stream.fileno())
        if _file_sha256(temporary) != expected_sha256:
            raise OSError("copied launcher digest mismatch")
        os.replace(temporary, target)
    except OSError as exc:
        try:
            if descriptor >= 0:
                os.close(descriptor)
            if temporary is not None:
                temporary.unlink()
        except OSError:
            pass
        raise ConfigurationError(
            "installation_failed",
            _system_error_message("La copie du lanceur a échoué", exc),
        ) from exc


def _previous_windows_launchers(active_target: Path) -> tuple[Path, ...]:
    directory = active_target.parent
    try:
        entries = tuple(directory.iterdir())
    except OSError:
        return ()
    previous: list[Path] = []
    for path in entries:
        if path == active_target:
            continue
        name = path.name
        if name.lower() == _LEGACY_WINDOWS_LAUNCHER or _VERSIONED_WINDOWS_LAUNCHER.fullmatch(name) is not None:
            previous.append(path)
    return tuple(sorted(previous, key=lambda item: item.name.lower()))


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1_048_576), b""):
            digest.update(block)
    return digest.hexdigest()


def _system_error_message(prefix: str, error: OSError) -> str:
    number = getattr(error, "winerror", None) or error.errno
    return f"{prefix} (erreur système {number})." if isinstance(number, int) else prefix + "."


def _normalized_windows_path(path: Path) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def _snapshot_windows_processes(candidate_names: dict[str, Path]) -> tuple[RunningLauncher, ...]:
    class ProcessEntry32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Process32FirstW.argtypes = (wintypes.HANDLE, ctypes.POINTER(ProcessEntry32W))
    kernel32.Process32FirstW.restype = wintypes.BOOL
    kernel32.Process32NextW.argtypes = (wintypes.HANDLE, ctypes.POINTER(ProcessEntry32W))
    kernel32.Process32NextW.restype = wintypes.BOOL
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = (
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    )
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
    invalid_handle = wintypes.HANDLE(-1).value
    if snapshot == invalid_handle:
        raise ctypes.WinError(ctypes.get_last_error())
    results: list[RunningLauncher] = []
    try:
        entry = ProcessEntry32W()
        entry.dwSize = ctypes.sizeof(entry)
        available = bool(kernel32.Process32FirstW(snapshot, ctypes.byref(entry)))
        while available:
            process = kernel32.OpenProcess(0x1000, False, entry.th32ProcessID)
            if process:
                try:
                    buffer = ctypes.create_unicode_buffer(32768)
                    length = wintypes.DWORD(len(buffer))
                    if kernel32.QueryFullProcessImageNameW(process, 0, buffer, ctypes.byref(length)):
                        normalized = _normalized_windows_path(Path(buffer.value))
                        if normalized in candidate_names:
                            results.append(RunningLauncher(int(entry.th32ProcessID), candidate_names[normalized]))
                finally:
                    kernel32.CloseHandle(process)
            available = bool(kernel32.Process32NextW(snapshot, ctypes.byref(entry)))
    finally:
        kernel32.CloseHandle(snapshot)
    return tuple(sorted(results, key=lambda item: item.pid))


def uninstall_for_current_user() -> Path:
    if not sys.platform.startswith("linux"):
        raise ConfigurationError(
            "uninstallation_unsupported",
            "La désinstallation intégrée est actuellement disponible sous Linux uniquement.",
        )
    target = default_linux_install_path()
    unregister_linux_protocol()
    try:
        details = target.lstat()
        if not stat.S_ISREG(details.st_mode) and not stat.S_ISLNK(details.st_mode):
            raise ConfigurationError("uninstallation_failed", "La cible d’installation est invalide.")
        target.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise ConfigurationError("uninstallation_failed", "Le lanceur Linux n’a pas pu être supprimé.") from exc
    return target
