"""Private, machine-local launcher configuration."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import stat
import sys
from typing import Any

from .errors import ConfigurationError
from .protocol import normalize_origin


@dataclass(frozen=True)
class LauncherConfig:
    trusted_origins: tuple[str, ...] = ()
    editors: dict[str, tuple[str, ...]] | None = None
    poll_seconds: float = 1.0
    stable_seconds: float = 2.0
    api_timeout_seconds: float = 30.0
    untracked_editor_extensions: tuple[str, ...] = ()
    system_default_editor_extensions: tuple[str, ...] = ()

    def editor_for(self, extension: str) -> tuple[str, ...] | None:
        return (self.editors or {}).get(extension.lower())

    def editor_exit_is_reliable(self, extension: str) -> bool:
        normalized = extension.lower()
        return (
            normalized not in self.untracked_editor_extensions
            and normalized not in self.system_default_editor_extensions
        )

    def should_ask_for_editor(self, extension: str) -> bool:
        normalized = extension.lower()
        return self.editor_for(normalized) is None and normalized not in self.system_default_editor_extensions


def default_config_path() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return base / "DoliLocalEdit" / "config.json"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "DoliLocalEdit" / "config.json"
    base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "dolilocaledit" / "config.json"


def default_recovery_root() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return base / "DoliLocalEdit" / "recovery"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "DoliLocalEdit" / "recovery"
    base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return base / "dolilocaledit" / "recovery"


def load_config(path: Path | None = None) -> LauncherConfig:
    config_path = path or default_config_path()
    if not config_path.exists():
        return LauncherConfig(editors={})
    _assert_private_regular_file(config_path)
    try:
        raw = config_path.read_bytes()
        if len(raw) > 65_536:
            raise ConfigurationError("config_too_large", "La configuration locale est trop volumineuse.")
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConfigurationError("config_invalid", "La configuration locale est illisible.") from exc
    if not isinstance(payload, dict) or set(payload) - {
        "trusted_origins", "editors", "poll_seconds", "stable_seconds", "api_timeout_seconds",
        "untracked_editor_extensions", "system_default_editor_extensions",
    }:
        raise ConfigurationError("config_invalid", "La configuration locale contient des champs invalides.")
    origins_raw = payload.get("trusted_origins", [])
    if not isinstance(origins_raw, list) or len(origins_raw) > 100 or not all(isinstance(item, str) for item in origins_raw):
        raise ConfigurationError("config_invalid", "La liste des serveurs approuvés est invalide.")
    origins = tuple(dict.fromkeys(normalize_origin(item) for item in origins_raw))
    editors = _parse_editors(payload.get("editors", {}))
    untracked = _parse_extensions(payload.get("untracked_editor_extensions", []), editors)
    system_defaults = _parse_extensions(payload.get("system_default_editor_extensions", []))
    if set(untracked) & set(system_defaults) or set(editors) & set(system_defaults):
        raise ConfigurationError("config_invalid", "Les choix d’éditeur locaux sont incohérents.")
    poll = _bounded_number(payload.get("poll_seconds", 1.0), "poll_seconds", 0.2, 10.0)
    stable = _bounded_number(payload.get("stable_seconds", 2.0), "stable_seconds", 0.5, 30.0)
    timeout = _bounded_number(payload.get("api_timeout_seconds", 30.0), "api_timeout_seconds", 5.0, 120.0)
    return LauncherConfig(origins, editors, poll, stable, timeout, untracked, system_defaults)


def trust_origin(origin: str, path: Path | None = None) -> Path:
    config_path = path or default_config_path()
    current = load_config(config_path)
    normalized = normalize_origin(origin)
    origins = tuple(dict.fromkeys((*current.trusted_origins, normalized)))
    payload = _config_payload(current, trusted_origins=origins)
    _atomic_private_json(config_path, payload)
    return config_path


def remember_editor_choice(
    extension: str,
    command: tuple[str, ...] | None,
    path: Path | None = None,
) -> Path:
    config_path = path or default_config_path()
    current = load_config(config_path)
    normalized = _normalize_extension(extension)
    editors = dict(current.editors or {})
    untracked = tuple(item for item in current.untracked_editor_extensions if item != normalized)
    system_defaults = tuple(item for item in current.system_default_editor_extensions if item != normalized)
    if command is None:
        editors.pop(normalized, None)
        system_defaults = tuple(dict.fromkeys((*system_defaults, normalized)))
    else:
        validated = _parse_editors({normalized: list(command)})
        editors[normalized] = validated[normalized]
        untracked = tuple(dict.fromkeys((*untracked, normalized)))
    payload = _config_payload(
        current,
        editors=editors,
        untracked_editor_extensions=untracked,
        system_default_editor_extensions=system_defaults,
    )
    _atomic_private_json(config_path, payload)
    return config_path


def forget_editor_choice(extension: str, path: Path | None = None) -> Path:
    normalized = _normalize_extension(extension)
    config_path = path or default_config_path()
    current = load_config(config_path)
    editors = dict(current.editors or {})
    editors.pop(normalized, None)
    untracked = tuple(item for item in current.untracked_editor_extensions if item != normalized)
    system_defaults = tuple(item for item in current.system_default_editor_extensions if item != normalized)
    payload = _config_payload(
        current,
        editors=editors,
        untracked_editor_extensions=untracked,
        system_default_editor_extensions=system_defaults,
    )
    _atomic_private_json(config_path, payload)
    return config_path


def _config_payload(
    config: LauncherConfig,
    *,
    trusted_origins: tuple[str, ...] | None = None,
    editors: dict[str, tuple[str, ...]] | None = None,
    untracked_editor_extensions: tuple[str, ...] | None = None,
    system_default_editor_extensions: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    return {
        "trusted_origins": list(config.trusted_origins if trusted_origins is None else trusted_origins),
        "editors": {
            key: list(value) for key, value in ((config.editors or {}) if editors is None else editors).items()
        },
        "poll_seconds": config.poll_seconds,
        "stable_seconds": config.stable_seconds,
        "api_timeout_seconds": config.api_timeout_seconds,
        "untracked_editor_extensions": list(
            config.untracked_editor_extensions
            if untracked_editor_extensions is None
            else untracked_editor_extensions
        ),
        "system_default_editor_extensions": list(
            config.system_default_editor_extensions
            if system_default_editor_extensions is None
            else system_default_editor_extensions
        ),
    }


def _parse_editors(value: Any) -> dict[str, tuple[str, ...]]:
    if not isinstance(value, dict) or len(value) > 100:
        raise ConfigurationError("config_invalid", "Les associations d’éditeur sont invalides.")
    result: dict[str, tuple[str, ...]] = {}
    for extension, command in value.items():
        if (
            not isinstance(extension, str)
            or not extension.startswith(".")
            or extension != extension.lower()
            or len(extension) > 16
            or not extension[1:].isalnum()
            or not isinstance(command, list)
            or not 1 <= len(command) <= 32
            or not all(isinstance(argument, str) and 0 < len(argument) <= 1024 for argument in command)
        ):
            raise ConfigurationError("config_invalid", "Une association d’éditeur est invalide.")
        executable = Path(command[0])
        if not executable.is_absolute():
            raise ConfigurationError("editor_not_absolute", "Le chemin d’un éditeur doit être absolu.")
        result[extension] = tuple(command)
    return result


def _parse_extensions(
    value: Any,
    editors: dict[str, tuple[str, ...]] | None = None,
) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > 100:
        raise ConfigurationError("config_invalid", "La liste des éditeurs non suivis est invalide.")
    result: list[str] = []
    for extension in value:
        if (
            not isinstance(extension, str)
            or not extension.startswith(".")
            or extension != extension.lower()
            or len(extension) > 16
            or not extension[1:].isalnum()
            or (editors is not None and extension not in editors)
        ):
            raise ConfigurationError("config_invalid", "Une extension d’éditeur non suivi est invalide.")
        if extension not in result:
            result.append(extension)
    return tuple(result)


def _normalize_extension(extension: str) -> str:
    normalized = extension.strip().lower() if isinstance(extension, str) else ""
    if (
        not normalized.startswith(".")
        or len(normalized) > 16
        or not normalized[1:].isalnum()
    ):
        raise ConfigurationError("extension_invalid", "L’extension d’éditeur est invalide.")
    return normalized


def _bounded_number(value: Any, name: str, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigurationError("config_invalid", f"La valeur {name} est invalide.")
    result = float(value)
    if not minimum <= result <= maximum:
        raise ConfigurationError("config_invalid", f"La valeur {name} est hors limites.")
    return result


def _assert_private_regular_file(path: Path) -> None:
    try:
        details = path.lstat()
    except OSError as exc:
        raise ConfigurationError("config_invalid", "La configuration locale est inaccessible.") from exc
    if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode):
        raise ConfigurationError("config_invalid", "La configuration locale doit être un fichier ordinaire.")
    if os.name == "posix":
        if details.st_uid != os.getuid():
            raise ConfigurationError("config_owner", "La configuration locale doit appartenir à l’utilisateur courant.")
        if details.st_mode & 0o077:
            raise ConfigurationError("config_permissions", "La configuration locale doit avoir les droits 0600.")


def _atomic_private_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name == "posix":
        os.chmod(path.parent, 0o700)
    temporary = path.with_name(path.name + ".tmp")
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
        os.replace(temporary, path)
    except BaseException:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise
