"""Per-user protocol registration for the initial Windows and Linux targets."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

from .errors import ConfigurationError


def register_protocol(executable: Path | None = None) -> Path | None:
    default_executable = Path(sys.executable) if getattr(sys, "frozen", False) else Path(sys.argv[0])
    command = (executable or default_executable).resolve()
    if not command.is_file() or not command.is_absolute():
        raise ConfigurationError("executable_invalid", "L’exécutable du lanceur est invalide.")
    if sys.platform == "win32":
        return _register_windows(command)
    if sys.platform.startswith("linux"):
        return _register_linux(command)
    raise ConfigurationError(
        "registration_unsupported",
        "L’enregistrement macOS sera fourni par le paquet applicatif signé.",
    )


def _register_windows(executable: Path) -> None:
    import winreg

    base = r"Software\Classes\dolilocaledit"
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, base) as key:
        winreg.SetValueEx(key, None, 0, winreg.REG_SZ, "URL:Doli Local Edit Protocol")
        winreg.SetValueEx(key, "URL Protocol", 0, winreg.REG_SZ, "")
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, base + r"\shell\open\command") as key:
        escaped = str(executable).replace('"', '\\"')
        winreg.SetValueEx(key, None, 0, winreg.REG_SZ, f'"{escaped}" open "%1"')
    return None


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
