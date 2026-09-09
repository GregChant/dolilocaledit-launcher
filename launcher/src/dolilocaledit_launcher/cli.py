"""Command-line and custom-protocol entry point."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

from .api import DoliLocalEditApi
from . import __version__
from .approval import (
    confirm_origin_trust,
    show_installation_complete,
    show_installation_error,
    show_launcher_check_error,
    show_launcher_error,
)
from .config import default_config_path, default_recovery_root, forget_editor_choice, load_config, trust_origin
from .errors import LauncherError, ProtocolError
from .installation import install_for_current_user, uninstall_for_current_user
from .protocol import inspect_check_uri, inspect_launch_uri, parse_check_uri
from .registration import register_protocol
from .session import EditingSessionRunner, PreparedSession


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dolilocaledit-launcher")
    subparsers = parser.add_subparsers(dest="command", required=True)
    open_parser = subparsers.add_parser("open", help="ouvrir un lien Doli Local Edit")
    open_parser.add_argument("uri")
    trust_parser = subparsers.add_parser("trust", help="approuver une origine Dolibarr HTTPS")
    trust_parser.add_argument("origin")
    subparsers.add_parser("config-path", help="afficher le chemin de configuration")
    forget_parser = subparsers.add_parser("forget-editor", help="oublier le choix local pour une extension")
    forget_parser.add_argument("extension")
    subparsers.add_parser("recoveries", help="afficher les dossiers de reprise présents")
    register_parser = subparsers.add_parser("register", help="enregistrer le protocole pour l’utilisateur courant")
    register_parser.add_argument("--executable", type=Path)
    install_parser = subparsers.add_parser("install", help="installer le lanceur pour l’utilisateur courant")
    install_parser.add_argument("--source", type=Path)
    install_parser.add_argument("--quiet", action="store_true", help="ne pas afficher la confirmation graphique")
    subparsers.add_parser("uninstall", help="désinstaller le lanceur Linux en conservant les reprises")
    subparsers.add_parser("_worker", help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments and (sys.platform == "win32" or sys.platform.startswith("linux")) and getattr(sys, "frozen", False):
        arguments.append("install")
    if arguments and arguments[0].startswith("dolilocaledit://"):
        arguments.insert(0, "open")
    parser = build_parser()
    options = parser.parse_args(arguments)
    document_name: str | None = None
    server_origin: str | None = None
    launcher_check = False
    try:
        if options.command == "config-path":
            print(default_config_path())
            return 0
        if options.command == "forget-editor":
            print(forget_editor_choice(options.extension))
            return 0
        if options.command == "recoveries":
            root = default_recovery_root()
            if root.is_dir():
                for item in sorted(root.iterdir()):
                    if item.is_dir():
                        print(item)
            return 0
        if options.command == "trust":
            print(trust_origin(options.origin))
            return 0
        if options.command == "register":
            result = register_protocol(options.executable)
            if result is not None:
                print(result)
            return 0
        if options.command == "install":
            installed = install_for_current_user(options.source)
            if options.quiet:
                print(installed)
            else:
                show_installation_complete(installed, __version__)
            return 0
        if options.command == "uninstall":
            removed = uninstall_for_current_user()
            _safe_print(f"Doli Local Edit désinstallé : {removed}")
            return 0
        if options.command == "open":
            if options.uri.startswith("dolilocaledit://check?"):
                launcher_check = True
                config = load_config()
                try:
                    target = parse_check_uri(options.uri, config.trusted_origins)
                except ProtocolError as exc:
                    if exc.code != "untrusted_origin":
                        raise
                    target = inspect_check_uri(options.uri)
                    if not confirm_origin_trust(target.origin):
                        raise
                    trust_origin(target.origin)
                    config = load_config()
                    target = parse_check_uri(options.uri, config.trusted_origins)
                platform_name = "windows" if sys.platform == "win32" else "linux"
                DoliLocalEditApi(target.endpoint, target.entity, config.api_timeout_seconds).confirm_launcher_check(
                    target.ticket,
                    __version__,
                    platform_name,
                )
                _safe_print("Test du lanceur confirmé.")
                return 0
            config = load_config()
            runner = EditingSessionRunner(config)
            try:
                prepared = runner.prepare(options.uri)
            except ProtocolError as exc:
                if exc.code != "untrusted_origin":
                    raise
                target = inspect_launch_uri(options.uri)
                if not confirm_origin_trust(target.origin):
                    raise
                trust_origin(target.origin)
                config = load_config()
                runner = EditingSessionRunner(config)
                prepared = runner.prepare(options.uri)
            document_name = prepared.exchange.filename
            server_origin = prepared.origin
            try:
                _spawn_worker(prepared)
            except LauncherError:
                try:
                    DoliLocalEditApi(prepared.endpoint, prepared.entity, config.api_timeout_seconds).cancel(
                        prepared.exchange.access_token,
                        prepared.exchange.lease_version,
                    )
                except LauncherError:
                    pass
                raise
            print("Session locale démarrée.")
            return 0
        if options.command == "_worker":
            raw = sys.stdin.buffer.read(16_385)
            if len(raw) > 16_384:
                raise LauncherError("worker_input_invalid", "Les données du processus local sont trop volumineuses.")
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise LauncherError("worker_input_invalid", "Les données du processus local sont invalides.") from exc
            prepared = PreparedSession.from_payload(payload)
            document_name = prepared.exchange.filename
            server_origin = prepared.origin
            del raw, payload
            result = EditingSessionRunner(load_config()).run_prepared(prepared)
            _safe_print(f"Session {result.status}: {result.filename}")
            return 0
    except LauncherError as exc:
        if options.command == "install" and not getattr(options, "quiet", False):
            show_installation_error(exc.code, str(exc))
        elif launcher_check and (sys.platform == "win32" or sys.platform.startswith("linux")):
            show_launcher_check_error(exc.code, str(exc))
        if (
            (sys.platform == "win32" or sys.platform.startswith("linux"))
            and getattr(sys, "frozen", False)
            and options.command in {"open", "_worker"}
            and not launcher_check
        ):
            if exc.code != "editor_selection_cancelled":
                details = {}
                if exc.document_name or document_name:
                    details["document_name"] = exc.document_name or document_name
                if server_origin is not None:
                    details["server_origin"] = server_origin
                if exc.recovery_directory is not None:
                    details["recovery_directory"] = exc.recovery_directory
                if exc.local_changes is not None:
                    details["local_changes"] = exc.local_changes
                if exc.server_cancelled is not None:
                    details["server_cancelled"] = exc.server_cancelled
                show_launcher_error(
                    exc.code,
                    str(exc),
                    **details,
                )
        _safe_print(f"Doli Local Edit [{exc.code}] : {exc}", error=True)
        return 2
    return 2


def _safe_print(message: object, *, error: bool = False) -> None:
    stream = sys.stderr if error else sys.stdout
    if stream is None:
        return
    try:
        print(message, file=stream)
    except OSError:
        # A PyInstaller --windowed process launched by a protocol handler has
        # no valid console handles even when Python exposes stream objects.
        pass


def _spawn_worker(prepared: PreparedSession) -> None:
    payload = json.dumps(prepared.to_payload(), separators=(",", ":")).encode("utf-8")
    if len(payload) > 16_384:
        raise LauncherError("worker_input_invalid", "Les données du processus local sont trop volumineuses.")
    if getattr(sys, "frozen", False):
        command = [sys.executable, "_worker"]
        # This worker intentionally outlives the protocol-handler process. Since
        # PyInstaller 6.9, a onefile child launched through sys.executable reuses
        # its parent's _MEI directory unless it is marked as a new application
        # instance. Reuse makes the parent cleanup race with the live worker.
        environment = os.environ.copy()
        environment["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    else:
        command = [sys.executable, "-m", "dolilocaledit_launcher", "_worker"]
        environment = None
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=None,
            stderr=None,
            close_fds=True,
            start_new_session=os.name == "posix",
            creationflags=creationflags,
            env=environment,
        )
        if process.stdin is None:
            raise OSError("worker pipe unavailable")
        process.stdin.write(payload)
        process.stdin.close()
    except OSError as exc:
        raise LauncherError("worker_start_failed", "Le processus local sécurisé n’a pas pu démarrer.") from exc
