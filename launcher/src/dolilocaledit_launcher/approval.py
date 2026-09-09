"""Explicit local approval for a previously unknown Dolibarr origin."""

from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
import sys


def confirm_origin_trust(origin: str) -> bool:
    if sys.platform == "win32":
        return _confirm_windows(origin)
    if sys.platform == "darwin":
        return _confirm_macos(origin)
    if sys.platform.startswith("linux"):
        return _confirm_linux(origin)
    return False


def confirm_external_template_change(target: str, scope: str) -> bool:
    """Ask before publishing one exact file whose attached Word template changed."""
    safe_target = _safe_dialog_value(target, 2048)
    if not safe_target or scope not in {"local", "remote"}:
        return False
    if scope == "local":
        warning = (
            "Word a remplacé le modèle lié par la cible locale ci-dessous. "
            "Dolibarr ne peut pas vérifier si ce fichier existe ni son contenu sur ce poste."
        )
    else:
        warning = (
            "Word a remplacé le modèle lié par la cible HTTPS ci-dessous. "
            "Son ouverture peut contacter ce serveur et charger du contenu externe."
        )
    message = (
        warning
        + "\n\n"
        + safe_target
        + "\n\nPublier ce fichier exact avec cette nouvelle cible ? "
        + "N’acceptez que si vous reconnaissez et approuvez cette source."
    )
    if sys.platform == "win32":
        return _windows_message(message, 0x00000004 | 0x00000030 | 0x00000100) == 6
    if sys.platform == "darwin":
        return _confirm_macos_dialog(message, "Publier")
    if sys.platform.startswith("linux"):
        return _confirm_linux_dialog(message, "Publier")
    return False


def installation_success_message(
    path: Path,
    version: str,
    running_previous_pids: tuple[int, ...] = (),
    retained_previous_files: tuple[Path, ...] = (),
) -> str:
    """Build the explicit post-installation result shown to the current user."""
    safe_path = _safe_dialog_value(str(path), 512) or "emplacement local non déterminé"
    safe_version = version if re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version) else "non déterminée"
    message = (
        "Installation réussie\n\n"
        f"Version : {safe_version}\n"
        f"Emplacement : {safe_path}\n"
        "Protocole dolilocaledit:// : enregistré pour votre compte."
    )
    safe_pids = tuple(pid for pid in running_previous_pids if isinstance(pid, int) and 0 < pid <= 4_294_967_295)[:20]
    safe_files = tuple(
        safe_name
        for safe_name in (_safe_dialog_value(path.name, 160) for path in retained_previous_files[:10])
        if safe_name
    )
    if safe_pids or safe_files:
        message += "\n\nMise à niveau sans interruption :"
        if safe_pids:
            message += "\nAnciennes instances encore actives : PID " + ", ".join(str(pid) for pid in safe_pids) + "."
        if safe_files:
            message += "\nAnciens exécutables conservés temporairement : " + ", ".join(safe_files) + "."
        message += (
            "\nCes instances n’ont pas été fermées afin de protéger les documents en cours. "
            "Les nouvelles ouvertures utilisent déjà cette version."
        )
    return message + "\n\nRetournez dans Dolibarr et utilisez « Tester le lanceur » avant d’ouvrir le document."


def show_installation_complete(
    path: Path,
    version: str,
    running_previous_pids: tuple[int, ...] = (),
    retained_previous_files: tuple[Path, ...] = (),
) -> None:
    message = installation_success_message(path, version, running_previous_pids, retained_previous_files)
    if sys.platform == "win32":
        _windows_message(message, 0x00000040 | 0x00010000)
        return
    if sys.platform.startswith("linux") and _linux_message(message, "info"):
        return
    print(f"Doli Local Edit installé dans {path}")


def installation_error_message(code: str, message: str) -> str:
    """Build an actionable and credential-free installation failure."""
    safe_code = code if re.fullmatch(r"[a-z0-9_]{1,64}", code) else "installation_error"
    safe_message = _safe_dialog_value(message, 512) or "Une erreur locale non précisée s’est produite."
    return (
        "Échec de l’installation de Doli Local Edit\n\n"
        f"Problème : {safe_message}\n"
        "Le protocole dolilocaledit:// n’a pas été confirmé.\n\n"
        "Action conseillée : fermez toute ancienne instance du lanceur, vérifiez les permissions "
        "de votre profil puis relancez l’installateur.\n"
        f"Code de diagnostic : {safe_code}"
    )


def show_installation_error(code: str, message: str) -> None:
    """Show an explicit installation failure on supported desktop systems."""
    content = installation_error_message(code, message)
    if sys.platform == "win32":
        _windows_message(content, 0x00000010 | 0x00010000)
    elif sys.platform.startswith("linux") and _linux_message(content, "error"):
        return
    else:
        print(content, file=sys.stderr)


def show_launcher_check_error(code: str, message: str) -> None:
    """Explain why a Dolibarr-to-launcher capability check failed locally."""
    safe_code = code if re.fullmatch(r"[a-z0-9_]{1,64}", code) else "launcher_check_error"
    safe_message = _safe_dialog_value(message, 512) or "Le lanceur n’a pas pu répondre à Dolibarr."
    content = (
        "Le test du lanceur a échoué.\n\n"
        f"Problème : {safe_message}\n"
        "Retournez dans Dolibarr, réinstallez le lanceur si nécessaire, puis relancez le test.\n"
        f"Code de diagnostic : {safe_code}"
    )
    if sys.platform == "win32":
        _windows_message(content, 0x00000010 | 0x00010000)
    elif sys.platform.startswith("linux"):
        _linux_message(content, "error")


def show_launcher_error(
    code: str,
    message: str,
    *,
    document_name: str | None = None,
    server_origin: str | None = None,
    recovery_directory: str | None = None,
    local_changes: bool | None = None,
    server_cancelled: bool | None = None,
) -> None:
    """Show a credential-free failure from a background graphical launcher."""
    content = launcher_error_message(
        code,
        message,
        document_name=document_name,
        server_origin=server_origin,
        recovery_directory=recovery_directory,
        local_changes=local_changes,
        server_cancelled=server_cancelled,
    )
    if sys.platform == "win32":
        _windows_message(content, 0x00000010 | 0x00010000)
    elif sys.platform.startswith("linux"):
        _linux_message(content, "error")


def launcher_error_message(
    code: str,
    message: str,
    *,
    document_name: str | None = None,
    server_origin: str | None = None,
    recovery_directory: str | None = None,
    local_changes: bool | None = None,
    server_cancelled: bool | None = None,
) -> str:
    """Build concise, actionable and credential-free details for a launcher failure."""
    safe_code = code if re.fullmatch(r"[a-z0-9_]{1,64}", code) else "launcher_error"
    safe_message = _safe_dialog_value(message, 512) or "Une erreur locale non précisée s’est produite."
    safe_document = _safe_dialog_value(document_name, 255) if document_name is not None else None
    safe_origin = _safe_dialog_value(server_origin, 512) if server_origin is not None else None
    safe_recovery = _safe_dialog_value(recovery_directory, 512) if recovery_directory is not None else None
    lines = [
        "Doli Local Edit n’a pas pu démarrer ou terminer l’édition.",
        "",
        "Document : " + (
            safe_document
            if safe_document
            else "non déterminé (échec avant l’identification du fichier)"
        ),
        "Problème : " + safe_message,
    ]
    if safe_origin:
        lines.insert(3, "Serveur Dolibarr : " + safe_origin)
    if local_changes is True:
        lines.append("Modifications locales : des changements enregistrés sur disque ont été détectés.")
    elif local_changes is False:
        lines.append("Modifications locales : aucun changement enregistré sur disque n’a été détecté.")
    if safe_recovery:
        lines.extend((
            "Copie de reprise : conservée.",
            "Dossier : " + safe_recovery,
        ))
    elif local_changes is False:
        lines.append("Copie de reprise : aucune copie utile n’a été conservée.")
    if server_cancelled is True:
        lines.append("Verrou Dolibarr : libéré.")
    elif server_cancelled is False and safe_document:
        lines.append("Verrou Dolibarr : libération non confirmée ; il expirera automatiquement.")
    lines.extend((
        "",
        "Action conseillée : " + _recommended_action(safe_code, bool(safe_recovery)),
        "Code de diagnostic : " + safe_code,
    ))
    return "\n".join(lines)


def _recommended_action(code: str, has_recovery: bool) -> str:
    if has_recovery:
        if code == "document_conflict":
            return "conservez la copie indiquée, rechargez le document dans Dolibarr et comparez les deux versions."
        if code == "external_template_declined":
            return "conservez la copie indiquée ou choisissez un modèle approuvé, puis relancez l’édition depuis Dolibarr."
        return "conservez la copie indiquée, vérifiez son contenu, puis relancez l’édition depuis Dolibarr."
    if code in {"invalid_credential", "session_timeout"}:
        return "retournez dans Dolibarr et relancez une nouvelle session d’édition."
    if code in {"rate_limited"}:
        return "attendez quelques minutes avant de relancer l’édition depuis Dolibarr."
    if code.startswith("editor_"):
        return "vérifiez l’application choisie ou sélectionnez un autre éditeur lors du prochain essai."
    if code.startswith("config_"):
        return "réinstallez le lanceur ou faites contrôler sa configuration locale."
    if code in {"local_io_failed", "document_invalid", "recovery_invalid", "recovery_owner", "workspace_invalid"}:
        return "fermez l’éditeur, vérifiez l’espace disque et les permissions, puis réessayez."
    if code in {"api_failed", "download_failed", "download_mismatch", "invalid_response"}:
        return "vérifiez la connexion à Dolibarr, puis relancez l’édition."
    return "notez le code ci-dessous et relancez l’édition depuis Dolibarr ; contactez l’administrateur si le problème revient."


def _safe_dialog_value(value: object, maximum: int) -> str:
    if not isinstance(value, str):
        return ""
    return "".join(
        character for character in value if character >= " " and character != "\x7f"
    )[:maximum].strip()


def _confirm_windows(origin: str) -> bool:
    message = (
        "Autoriser ce serveur Dolibarr à ouvrir des documents locaux ?\n\n"
        f"{origin}\n\n"
        "N’acceptez que si vous reconnaissez exactement cette adresse."
    )
    return _windows_message(message, 0x00000004 | 0x00000030 | 0x00000100) == 6


def _windows_message(message: str, flags: int) -> int:
    import ctypes

    return int(ctypes.windll.user32.MessageBoxW(None, message, "Doli Local Edit", flags))


def _confirm_macos(origin: str) -> bool:
    return _confirm_macos_dialog("Autoriser ce serveur Dolibarr ?\n\n" + origin, "Autoriser")


def _confirm_macos_dialog(message: str, accept_label: str) -> bool:
    executable = Path("/usr/bin/osascript")
    if not executable.is_file() or accept_label not in {"Autoriser", "Publier"}:
        return False
    script = (
        "on run argv\n"
        'display dialog item 1 of argv buttons {"Refuser", item 2 of argv} '
        'default button "Refuser" with icon caution\n'
        "return button returned of result\n"
        "end run"
    )
    try:
        result = subprocess.run(
            [str(executable), "-e", script, "--", message, accept_label],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=60,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and accept_label in result.stdout


def _confirm_linux(origin: str) -> bool:
    return _confirm_linux_dialog("Autoriser ce serveur Dolibarr ?\n\n" + origin, "Autoriser")


def _confirm_linux_dialog(message: str, accept_label: str) -> bool:
    if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
        return False
    if accept_label not in {"Autoriser", "Publier"}:
        return False
    executable = _which_absolute("zenity")
    if executable is not None:
        command = [
            str(executable),
            "--question",
            "--title=Doli Local Edit",
            "--text=" + message,
            "--ok-label=" + accept_label,
            "--cancel-label=Refuser",
        ]
    else:
        executable = _which_absolute("kdialog")
        if executable is None:
            return False
        command = [
            str(executable),
            "--title",
            "Doli Local Edit",
            "--yesno",
            message,
            "--yes-label",
            accept_label,
            "--no-label",
            "Refuser",
        ]
    try:
        result = subprocess.run(
            command,
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=60,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _linux_message(message: str, kind: str) -> bool:
    if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
        return False
    zenity = _which_absolute("zenity")
    if zenity is not None:
        command = [str(zenity), "--" + kind, "--title=Doli Local Edit", "--text=" + message]
    else:
        kdialog = _which_absolute("kdialog")
        if kdialog is None:
            return False
        command = [
            str(kdialog),
            "--title",
            "Doli Local Edit",
            "--msgbox" if kind == "info" else "--error",
            message,
        ]
    try:
        result = subprocess.run(
            command,
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=60,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _which_absolute(name: str) -> Path | None:
    from shutil import which

    result = which(name)
    return Path(result).resolve() if result else None
