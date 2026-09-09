"""Per-user installation of a frozen launcher without administrator rights."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile

from .errors import ConfigurationError
from .registration import register_protocol, unregister_linux_protocol


def default_windows_install_path() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    return base / "Programs" / "DoliLocalEdit" / "dolilocaledit-launcher.exe"


def default_linux_install_path() -> Path:
    return Path.home() / ".local" / "bin" / "dolilocaledit-launcher"


def install_for_current_user(source: Path | None = None) -> Path:
    if sys.platform == "win32":
        target = default_windows_install_path()
    elif sys.platform.startswith("linux"):
        target = default_linux_install_path()
    else:
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

    try:
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as exc:
        raise ConfigurationError("installation_failed", "Le dossier d’installation est inaccessible.") from exc
    if source_path != target:
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
            os.replace(temporary, target)
        except OSError as exc:
            try:
                if descriptor >= 0:
                    os.close(descriptor)
                if temporary is not None:
                    temporary.unlink()
            except OSError:
                pass
            raise ConfigurationError("installation_failed", "La copie du lanceur a échoué.") from exc
    if os.name == "posix":
        try:
            os.chmod(target, 0o700)
        except OSError as exc:
            raise ConfigurationError("installation_failed", "Les permissions du lanceur sont invalides.") from exc
    register_protocol(target)
    return target


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
