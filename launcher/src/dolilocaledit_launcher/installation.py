"""Per-user installation of a frozen launcher without administrator rights."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import base64
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
from typing import Iterator

from .errors import ConfigurationError
from .registration import (
    is_owned_windows_launcher,
    register_protocol,
    register_windows_installation,
    unregister_linux_protocol,
    unregister_windows_installation,
    windows_install_directory,
)


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


@dataclass(frozen=True)
class UninstallationResult:
    """Removed registration and any program files waiting for their processes to exit."""

    path: Path
    deferred_files: tuple[Path, ...] = ()


def default_windows_install_directory() -> Path:
    return windows_install_directory()


def default_windows_install_path(version: str, executable_sha256: str) -> Path:
    """Return an immutable per-release target so active workers never block upgrades."""
    if _VERSION_PATTERN.fullmatch(version) is None or _SHA256_PATTERN.fullmatch(executable_sha256) is None:
        raise ConfigurationError("installation_version_invalid", "La version du lanceur est invalide.")
    return default_windows_install_directory() / f"dolilocaledit-launcher-{version}-{executable_sha256}.exe"


def default_linux_install_path() -> Path:
    return Path.home() / ".local" / "bin" / "dolilocaledit-launcher"


def install_for_current_user(source: Path | None = None, version: str | None = None) -> InstallationResult:
    if sys.platform == "win32":
        with _windows_installation_lock():
            return _install_for_current_user_unlocked(source, version)
    return _install_for_current_user_unlocked(source, version)


def _install_for_current_user_unlocked(source: Path | None, version: str | None) -> InstallationResult:
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
                if sys.platform == "win32":
                    raise ConfigurationError(
                        "installation_target_invalid",
                        "Un exécutable inattendu utilise déjà le nom sécurisé de cette version.",
                    )
                # Linux uses a fixed path. Replace its directory entry atomically
                # so an existing worker can keep reading the previous inode.
                _copy_launcher(source_path, target, source_sha256)
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
        if sys.platform == "win32":
            register_windows_installation(target, version or "")
        else:
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


def uninstall_for_current_user() -> UninstallationResult:
    """Remove only this user's launcher and registration, retaining all user data."""
    if sys.platform == "win32":
        with _windows_installation_lock():
            return _uninstall_windows_for_current_user()
    if not sys.platform.startswith("linux"):
        raise ConfigurationError(
            "uninstallation_unsupported",
            "La désinstallation intégrée est disponible sous Windows et Linux.",
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
    return UninstallationResult(target)


def _uninstall_windows_for_current_user() -> UninstallationResult:
    directory = default_windows_install_directory().absolute()
    sentinel = directory / _LEGACY_WINDOWS_LAUNCHER
    if not is_owned_windows_launcher(sentinel):
        raise ConfigurationError("uninstallation_failed", "Le dossier d’installation Windows est invalide.")
    unregister_windows_installation()
    candidates = tuple(
        path for path in _previous_windows_launchers(directory / "uninstaller-placeholder")
        if is_owned_windows_launcher(path) and path.is_file()
    )
    running = find_running_windows_launchers(candidates)
    running_paths = {_normalized_windows_path(instance.path) for instance in running}
    if getattr(sys, "frozen", False):
        running_paths.add(_normalized_windows_path(Path(sys.executable)))
    deferred = []
    for path in candidates:
        if _normalized_windows_path(path) in running_paths:
            deferred.append(path)
            continue
        try:
            path.unlink()
        except FileNotFoundError:
            continue
        except OSError:
            deferred.append(path)
    if deferred:
        try:
            _schedule_windows_uninstall_cleanup(directory, tuple(deferred), running)
        except OSError as exc:
            raise ConfigurationError(
                "uninstallation_cleanup_failed",
                "L’inscription a été retirée, mais le nettoyage différé n’a pas pu démarrer. Les documents sont conservés.",
            ) from exc
    else:
        try:
            directory.rmdir()
        except OSError:
            # Unknown files, configuration and recovery directories are never removed.
            pass
    return UninstallationResult(directory, tuple(deferred))


def _windows_powershell_path() -> Path:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetSystemDirectoryW.argtypes = (wintypes.LPWSTR, wintypes.UINT)
    kernel32.GetSystemDirectoryW.restype = wintypes.UINT
    buffer = ctypes.create_unicode_buffer(32768)
    length = kernel32.GetSystemDirectoryW(buffer, len(buffer))
    if length == 0 or length >= len(buffer):
        raise ctypes.WinError(ctypes.get_last_error())
    executable = Path(buffer.value) / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    if not executable.is_file():
        raise OSError("Windows PowerShell is unavailable")
    return executable


def _windows_installation_mutex_name() -> str:
    directory = _normalized_windows_path(default_windows_install_directory())
    fingerprint = hashlib.sha256(directory.encode("utf-8")).hexdigest()
    return "Global\\DoliLocalEdit-Install-" + fingerprint


@contextmanager
def _windows_installation_lock() -> Iterator[None]:
    """Serialize installation and delayed deletion for this user's exact directory."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.ReleaseMutex.argtypes = (wintypes.HANDLE,)
    kernel32.ReleaseMutex.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.CreateMutexW(None, False, _windows_installation_mutex_name())
    if not handle:
        raise ConfigurationError("installation_lock_failed", "Le verrou d’installation Windows est inaccessible.")
    acquired = False
    try:
        result = kernel32.WaitForSingleObject(handle, 30000)
        acquired = result in {0, 0x00000080}
        if not acquired:
            raise ConfigurationError("installation_busy", "Une autre installation ou désinstallation est en cours. Réessayez.")
        yield
    finally:
        if acquired:
            kernel32.ReleaseMutex(handle)
        kernel32.CloseHandle(handle)


def _schedule_windows_uninstall_cleanup(
    directory: Path,
    candidates: tuple[Path, ...],
    running: tuple[RunningLauncher, ...],
) -> None:
    """Run fixed, hidden cleanup code after owned processes finish, without a shell."""
    entries = []
    for path in candidates:
        if not is_owned_windows_launcher(path) or path.parent != directory:
            raise OSError("Unsafe deferred launcher cleanup target")
        pids = [instance.pid for instance in running if instance.path == path]
        if getattr(sys, "frozen", False) and _normalized_windows_path(path) == _normalized_windows_path(Path(sys.executable)):
            pids.append(os.getpid())
        entries.append({"path": str(path), "sha256": _file_sha256(path), "pids": sorted(set(pids))})
    payload = json.dumps({"directory": str(directory), "mutex": _windows_installation_mutex_name(), "files": entries}).encode("utf-8")
    encoded = base64.b64encode(_WINDOWS_UNINSTALL_CLEANUP.encode("utf-16-le")).decode("ascii")
    process = subprocess.Popen(
        [str(_windows_powershell_path()), "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        shell=False,
        close_fds=True,
        creationflags=0x08000000 | 0x00000200,
    )
    # The child deliberately outlives this launcher. A daemon reaps it when this
    # process remains alive, without blocking shutdown or leaking Popen warnings.
    threading.Thread(target=process.wait, name="dolilocaledit-uninstall-cleanup", daemon=True).start()
    if process.stdin is None:
        raise OSError("Deferred cleanup input pipe is unavailable")
    try:
        process.stdin.write(payload)
    finally:
        process.stdin.close()


_WINDOWS_UNINSTALL_CLEANUP = r'''
$ErrorActionPreference = 'Stop'
[Console]::InputEncoding = [Text.UTF8Encoding]::new($false)
$data = ConvertFrom-Json ([Console]::In.ReadToEnd())
$root = [IO.Path]::GetFullPath([string]$data.directory)
$deadline = [DateTime]::UtcNow.AddHours(1)
$mutex = [Threading.Mutex]::new($false, [string]$data.mutex)
function Test-OwnedPath([string]$path) {
    $full = [IO.Path]::GetFullPath($path)
    if (-not [string]::Equals([IO.Path]::GetDirectoryName($full), $root, [StringComparison]::OrdinalIgnoreCase)) { return $false }
    if ([IO.Path]::GetFileName($full) -notmatch '^dolilocaledit-launcher(?:-[0-9]+\.[0-9]+\.[0-9]+-[a-f0-9]{64})?\.exe$') { return $false }
    $item = Get-Item -LiteralPath $full -Force -ErrorAction SilentlyContinue
    if ($null -eq $item -or $item.PSIsContainer) { return $false }
    while ($null -ne $item) {
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { return $false }
        $item = if ($item -is [IO.DirectoryInfo]) { $item.Parent } else { $item.Directory }
    }
    return $true
}
foreach ($entry in $data.files) {
    $target = [string]$entry.path
    if (-not (Test-OwnedPath $target)) { continue }
    $ready = $true
    foreach ($processId in $entry.pids) {
        try { $process = [Diagnostics.Process]::GetProcessById([int]$processId) }
        catch [ArgumentException] { continue }
        try {
            $image = $process.MainModule.FileName
            if (-not [string]::Equals($image, $target, [StringComparison]::OrdinalIgnoreCase)) { continue }
            $remaining = [int][Math]::Max(0, ($deadline - [DateTime]::UtcNow).TotalMilliseconds)
            if (-not $process.WaitForExit($remaining)) { $ready = $false; break }
        } catch { $ready = $false; break }
        finally { $process.Dispose() }
    }
    if (-not $ready) { continue }
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        $locked = $false
        try {
            try { $locked = $mutex.WaitOne(30000) }
            catch [Threading.AbandonedMutexException] { $locked = $true }
            if (-not $locked) { break }
            if (-not (Test-OwnedPath $target)) { break }
            $arp = Get-ItemProperty -LiteralPath 'Registry::HKEY_CURRENT_USER\Software\Microsoft\Windows\CurrentVersion\Uninstall\DoliLocalEdit' -ErrorAction SilentlyContinue
            $protocol = Get-ItemProperty -LiteralPath 'Registry::HKEY_CURRENT_USER\Software\Classes\dolilocaledit\shell\open\command' -ErrorAction SilentlyContinue
            if ($null -ne $arp -and $arp.UninstallString -eq ('"' + $target + '" uninstall')) { break }
            if ($null -ne $protocol -and $protocol.'(default)' -eq ('"' + $target + '" open "%1"')) { break }
            if ((Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash -ne [string]$entry.sha256) { break }
            Remove-Item -LiteralPath $target -Force
            break
        } catch { Start-Sleep -Milliseconds 100 }
        finally { if ($locked) { $mutex.ReleaseMutex() } }
    }
}
$mutex.Dispose()
# Leave the directory itself intact: another installation can already be using it.
'''
