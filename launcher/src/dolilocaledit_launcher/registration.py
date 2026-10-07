"""Per-user protocol registration for the initial Windows and Linux targets."""

from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
import sys

from .errors import ConfigurationError


_WINDOWS_PROTOCOL_KEY = r"Software\Classes\dolilocaledit"
_WINDOWS_UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\DoliLocalEdit"
_WINDOWS_LAUNCHER_NAME = re.compile(
    r"dolilocaledit-launcher(?:-[0-9]+\.[0-9]+\.[0-9]+-[a-f0-9]{64})?\.exe",
    re.IGNORECASE,
)


def windows_install_directory() -> Path:
    """Return the one installation directory owned by the current Windows user."""
    base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    return base / "Programs" / "DoliLocalEdit"


def is_owned_windows_launcher(path: Path) -> bool:
    """Recognize only direct, non-redirected launcher files in our per-user directory."""
    if not path.is_absolute() or _WINDOWS_LAUNCHER_NAME.fullmatch(path.name) is None:
        return False
    directory = windows_install_directory()
    if os.path.normcase(os.path.abspath(str(path.parent))) != os.path.normcase(os.path.abspath(str(directory))):
        return False
    for candidate in (path, *path.parents):
        if candidate.is_symlink() or (hasattr(candidate, "is_junction") and candidate.is_junction()):
            return False
    return True


def register_protocol(executable: Path | None = None) -> Path | None:
    default_executable = Path(sys.executable) if getattr(sys, "frozen", False) else Path(sys.argv[0])
    command = (executable or default_executable).resolve()
    if not command.is_file() or not command.is_absolute():
        raise ConfigurationError("executable_invalid", "L’exécutable du lanceur est invalide.")
    if sys.platform == "win32":
        if is_owned_windows_launcher(command):
            from .installation import _windows_installation_lock

            with _windows_installation_lock():
                return _register_windows(command)
        return _register_windows(command)
    if sys.platform.startswith("linux"):
        return _register_linux(command)
    raise ConfigurationError(
        "registration_unsupported",
        "L’enregistrement macOS sera fourni par le paquet applicatif signé.",
    )


def _register_windows(executable: Path) -> None:
    import winreg

    _apply_windows_registry_values(winreg, _windows_protocol_values(winreg, executable))
    return None


def register_windows_installation(executable: Path, version: str) -> None:
    """Register protocol and Apps & Features together, restoring both on failure."""
    import winreg

    if not is_owned_windows_launcher(executable) or not executable.is_file():
        raise ConfigurationError("installation_target_invalid", "La cible d’installation est invalide.")
    if re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version) is None:
        raise ConfigurationError("installation_version_invalid", "La version du lanceur est invalide.")
    values = _windows_protocol_values(winreg, executable)
    values[_WINDOWS_UNINSTALL_KEY] = {
        "DisplayName": ("Doli Local Edit", winreg.REG_SZ),
        "DisplayVersion": (version, winreg.REG_SZ),
        "Publisher": ("Experts Conseils Chanton", winreg.REG_SZ),
        "InstallLocation": (str(executable.parent), winreg.REG_SZ),
        "DisplayIcon": (_windows_command(executable, ",0"), winreg.REG_SZ),
        "UninstallString": (_windows_command(executable, " uninstall"), winreg.REG_SZ),
        "QuietUninstallString": (_windows_command(executable, " uninstall --quiet"), winreg.REG_SZ),
        "NoModify": (1, winreg.REG_DWORD),
        "NoRepair": (1, winreg.REG_DWORD),
        "EstimatedSize": (max(1, (executable.stat().st_size + 1023) // 1024), winreg.REG_DWORD),
    }
    _apply_windows_registry_values(winreg, values)


def unregister_windows_installation() -> None:
    """Remove only registry entries that still identify our own per-user install."""
    import winreg

    try:
        command = _windows_registry_value(winreg, _WINDOWS_PROTOCOL_KEY + r"\shell\open\command", "")
        target = _windows_registered_target(command, ' open "%1"')
        if target is not None and is_owned_windows_launcher(target):
            _delete_windows_registry_tree(winreg, _WINDOWS_PROTOCOL_KEY)

        uninstall = _windows_registry_value(winreg, _WINDOWS_UNINSTALL_KEY, "UninstallString")
        target = _windows_registered_target(uninstall, " uninstall")
        if (
            target is not None
            and is_owned_windows_launcher(target)
            and _windows_registry_value(winreg, _WINDOWS_UNINSTALL_KEY, "DisplayName") == "Doli Local Edit"
            and _windows_registry_value(winreg, _WINDOWS_UNINSTALL_KEY, "InstallLocation") == str(target.parent)
        ):
            _delete_windows_registry_tree(winreg, _WINDOWS_UNINSTALL_KEY)
    except OSError as exc:
        raise ConfigurationError("unregistration_failed", "L’inscription Windows n’a pas pu être retirée.") from exc


def _windows_command(executable: Path, suffix: str) -> str:
    text = str(executable)
    if any(character in text for character in '\0\r\n"'):
        raise ConfigurationError("executable_invalid", "L’exécutable du lanceur est invalide.")
    return '"' + text + '"' + suffix


def _windows_protocol_values(winreg: object, executable: Path) -> dict[str, dict[str, tuple[object, int]]]:
    return {
        _WINDOWS_PROTOCOL_KEY: {
            "": ("URL:Doli Local Edit Protocol", winreg.REG_SZ),
            "URL Protocol": ("", winreg.REG_SZ),
        },
        _WINDOWS_PROTOCOL_KEY + r"\shell\open\command": {
            "": (_windows_command(executable, ' open "%1"'), winreg.REG_SZ),
        },
    }


def _windows_registry_value(winreg: object, path: str, name: str) -> object | None:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_READ) as key:
            return winreg.QueryValueEx(key, name)[0]
    except FileNotFoundError:
        return None


def _windows_registered_target(command: object, suffix: str) -> Path | None:
    if not isinstance(command, str) or not command.startswith('"') or not command.endswith('"' + suffix):
        return None
    value = command[1:-(len(suffix) + 1)]
    if not value or any(character in value for character in '\0\r\n"'):
        return None
    return Path(value)


def _apply_windows_registry_values(winreg: object, values: dict[str, dict[str, tuple[object, int]]]) -> None:
    """Snapshot just the managed values; preserve unrelated registry data on rollback."""
    snapshots: dict[str, dict[str, tuple[object, int] | None]] = {}
    missing_keys: list[str] = []
    for path, entries in values.items():
        base = _WINDOWS_PROTOCOL_KEY if path.startswith(_WINDOWS_PROTOCOL_KEY) else _WINDOWS_UNINSTALL_KEY
        components = path[len(base):].split("\\")
        branch = base
        for component in ("", *components):
            if component:
                branch += "\\" + component
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, branch, 0, winreg.KEY_READ):
                    pass
            except FileNotFoundError:
                if branch not in missing_keys:
                    missing_keys.append(branch)
        snapshots[path] = {}
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_READ) as key:
                for name in entries:
                    try:
                        snapshots[path][name] = winreg.QueryValueEx(key, name)
                    except FileNotFoundError:
                        snapshots[path][name] = None
        except FileNotFoundError:
            snapshots[path] = dict.fromkeys(entries)
    try:
        for path, entries in values.items():
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, path) as key:
                for name, (value, kind) in entries.items():
                    winreg.SetValueEx(key, name, 0, kind, value)
    except OSError as exc:
        rollback_failed = False
        for path, entries in reversed(tuple(snapshots.items())):
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_SET_VALUE) as key:
                    for name, previous in entries.items():
                        if previous is None:
                            try:
                                winreg.DeleteValue(key, name)
                            except FileNotFoundError:
                                pass
                        else:
                            winreg.SetValueEx(key, name, 0, previous[1], previous[0])
            except FileNotFoundError:
                pass
            except OSError:
                rollback_failed = True
        for path in sorted(missing_keys, key=lambda item: item.count("\\"), reverse=True):
            try:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, path)
            except FileNotFoundError:
                pass
            except OSError:
                rollback_failed = True
        message = "L’enregistrement Windows a échoué ; l’inscription précédente a été restaurée."
        if rollback_failed:
            message = "L’enregistrement Windows a échoué et sa restauration n’a pas pu être confirmée."
        raise ConfigurationError("registration_failed", message) from exc


def _delete_windows_registry_tree(winreg: object, path: str, depth: int = 0) -> None:
    if depth > 16:
        raise OSError("Unexpected Windows registration depth")
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_READ) as key:
            children = []
            for index in range(128):
                try:
                    children.append(winreg.EnumKey(key, index))
                except OSError as exc:
                    if getattr(exc, "winerror", None) == 259 or isinstance(exc, FileNotFoundError):
                        break
                    raise
            else:
                raise OSError("Unexpected Windows registration size")
        for child in children:
            _delete_windows_registry_tree(winreg, path + "\\" + child, depth + 1)
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, path)
    except FileNotFoundError:
        return


def _register_linux(executable: Path) -> Path:
    applications = _linux_applications_directory()
    applications.mkdir(mode=0o700, parents=True, exist_ok=True)
    desktop = applications / "dolilocaledit-launcher.desktop"
    quoted = _desktop_quote(str(executable))
    content = (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=Doli Local Edit\n"
        f"Exec={quoted} open %u\n"
        "Terminal=false\n"
        "NoDisplay=true\n"
        "MimeType=x-scheme-handler/dolilocaledit;\n"
    )
    temporary = desktop.with_suffix(".tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(temporary, flags, 0o644)
    with os.fdopen(descriptor, "w", encoding="utf-8", closefd=True) as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(temporary, 0o644)
    os.replace(temporary, desktop)
    try:
        _set_linux_protocol_default(desktop.name)
        _refresh_linux_desktop_database(applications)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ConfigurationError("registration_failed", "L’enregistrement du protocole a échoué.") from exc
    return desktop


def unregister_linux_protocol() -> Path:
    if not sys.platform.startswith("linux"):
        raise ConfigurationError("registration_unsupported", "La désinscription demandée n’est pas disponible.")
    applications = _linux_applications_directory()
    desktop = applications / "dolilocaledit-launcher.desktop"
    try:
        details = desktop.lstat()
        if not details or (not desktop.is_file() and not desktop.is_symlink()):
            raise ConfigurationError("unregistration_failed", "L’inscription du protocole est invalide.")
        desktop.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise ConfigurationError("unregistration_failed", "L’inscription du protocole n’a pas pu être retirée.") from exc
    _refresh_linux_desktop_database(applications)
    return desktop


def _linux_applications_directory() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "applications"


def _set_linux_protocol_default(desktop_name: str) -> None:
    xdg_mime = shutil_which_absolute("xdg-mime")
    if xdg_mime is not None:
        command = [str(xdg_mime), "default", desktop_name, "x-scheme-handler/dolilocaledit"]
    else:
        gio = shutil_which_absolute("gio")
        if gio is None:
            raise ConfigurationError(
                "xdg_mime_missing",
                "xdg-mime ou gio est requis pour enregistrer le protocole.",
            )
        command = [str(gio), "mime", "x-scheme-handler/dolilocaledit", desktop_name]
    subprocess.run(
        command,
        check=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        shell=False,
    )


def _refresh_linux_desktop_database(applications: Path) -> None:
    updater = shutil_which_absolute("update-desktop-database")
    if updater is None or not applications.is_dir():
        return
    subprocess.run(
        [str(updater), str(applications)],
        check=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        shell=False,
    )


def shutil_which_absolute(name: str) -> Path | None:
    from shutil import which

    result = which(name)
    return Path(result).resolve() if result else None


def _desktop_quote(value: str) -> str:
    if any(character in value for character in "\n\r\0"):
        raise ConfigurationError("executable_invalid", "L’exécutable du lanceur est invalide.")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("`", "\\`").replace("$", "\\$") + '"'
