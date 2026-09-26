"""One credential-in-memory local editing session."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import time
from typing import Any, Callable, Protocol

from .api import DoliLocalEditApi, ExchangeResult, UploadResult
from .config import LauncherConfig, default_recovery_root, remember_editor_choice
from .editor import EditorChoice, EditorHandle, choose_editor, open_editor
from .approval import confirm_external_template_change
from .errors import ApiError, ExternalTemplateApprovalRequired, LauncherError, WorkspaceError
from .protocol import normalize_origin, parse_launch_uri, validate_endpoint
from .workspace import RecoveryWorkspace, file_sha256, file_signature


class ApiLike(Protocol):
    def exchange(self, ticket: str) -> ExchangeResult: ...
    def download(self, access_token: str, destination: Path, expected_sha256: str, expected_size: int) -> None: ...
    def heartbeat(self, access_token: str, lease_version: int) -> tuple[int, str]: ...
    def upload(
        self,
        access_token: str,
        source: Path,
        base_sha256: str,
        lease_version: int,
        external_template_approval: str | None = None,
    ) -> UploadResult: ...
    def complete(self, access_token: str, lease_version: int, current_sha256: str) -> None: ...
    def cancel(self, access_token: str, lease_version: int) -> None: ...


@dataclass(frozen=True)
class SessionResult:
    status: str
    filename: str


@dataclass(frozen=True)
class RecoveryOutcome:
    retained_directory: Path | None
    modified_on_disk: bool
    server_cancelled: bool


@dataclass(frozen=True)
class PreparedSession:
    endpoint: str
    origin: str
    entity: int
    exchange: ExchangeResult

    def to_payload(self) -> dict[str, object]:
        return {
            "schema": 1,
            "endpoint": self.endpoint,
            "origin": self.origin,
            "entity": self.entity,
            "exchange": {
                "access_token": self.exchange.access_token,
                "expires_at": self.exchange.expires_at,
                "session_uuid": self.exchange.session_uuid,
                "filename": self.exchange.filename,
                "base_sha256": self.exchange.base_sha256,
                "byte_size": self.exchange.byte_size,
                "lease_version": self.exchange.lease_version,
                "lease_expires_at": self.exchange.lease_expires_at,
                "heartbeat_seconds": self.exchange.heartbeat_seconds,
            },
        }

    @classmethod
    def from_payload(cls, payload: Any) -> "PreparedSession":
        if not isinstance(payload, dict) or set(payload) != {"schema", "endpoint", "origin", "entity", "exchange"}:
            raise LauncherError("worker_input_invalid", "Les données du processus local sont invalides.")
        if payload["schema"] != 1 or not isinstance(payload["endpoint"], str) or not isinstance(payload["origin"], str):
            raise LauncherError("worker_input_invalid", "Les données du processus local sont invalides.")
        entity = payload["entity"]
        values = payload["exchange"]
        expected_keys = {
            "access_token", "expires_at", "session_uuid", "filename", "base_sha256", "byte_size",
            "lease_version", "lease_expires_at", "heartbeat_seconds",
        }
        if (
            isinstance(entity, bool)
            or not isinstance(entity, int)
            or not 1 <= entity <= 2_147_483_647
            or not isinstance(values, dict)
            or set(values) != expected_keys
        ):
            raise LauncherError("worker_input_invalid", "Les données du processus local sont invalides.")
        try:
            exchange = ExchangeResult(**values)
        except TypeError as exc:
            raise LauncherError("worker_input_invalid", "Les données du processus local sont invalides.") from exc
        prepared = cls(payload["endpoint"], payload["origin"], entity, exchange)
        prepared.validate(None)
        return prepared

    def validate(self, trusted_origins: tuple[str, ...] | None) -> None:
        endpoint, endpoint_origin = validate_endpoint(self.endpoint)
        origin = normalize_origin(self.origin)
        exchange = self.exchange
        if (
            endpoint != self.endpoint
            or endpoint_origin != origin
            or (
                trusted_origins is not None
                and origin not in tuple(normalize_origin(item) for item in trusted_origins)
            )
            or isinstance(self.entity, bool)
            or not 1 <= self.entity <= 2_147_483_647
            or re.fullmatch(r"[A-Za-z0-9_-]{43}", exchange.access_token) is None
            or re.fullmatch(r"[a-f0-9]{64}", exchange.base_sha256) is None
            or not _safe_short_string(exchange.filename, 255)
            or not _safe_short_string(exchange.session_uuid, 64)
            or not _safe_short_string(exchange.expires_at, 64)
            or not _safe_short_string(exchange.lease_expires_at, 64)
            or isinstance(exchange.byte_size, bool)
            or not isinstance(exchange.byte_size, int)
            or not 0 <= exchange.byte_size <= 1_073_741_824
            or isinstance(exchange.lease_version, bool)
            or not isinstance(exchange.lease_version, int)
            or exchange.lease_version <= 0
            or isinstance(exchange.heartbeat_seconds, bool)
            or not isinstance(exchange.heartbeat_seconds, int)
            or not 1 <= exchange.heartbeat_seconds <= 3600
        ):
            raise LauncherError("worker_input_invalid", "Les données du processus local sont invalides.")


class EditingSessionRunner:
    def __init__(
        self,
        config: LauncherConfig,
        recovery_root: Path | None = None,
        api_factory: Callable[[str, int, float], ApiLike] = DoliLocalEditApi,
        editor_opener: Callable[[Path, tuple[str, ...] | None], EditorHandle] = open_editor,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        editor_selector: Callable[[Path], EditorChoice | None] = choose_editor,
        editor_choice_saver: Callable[[str, tuple[str, ...] | None], Path] = remember_editor_choice,
        external_template_approver: Callable[[str, str], bool] = confirm_external_template_change,
    ) -> None:
        self.config = config
        self.recovery_root = recovery_root or default_recovery_root()
        self.api_factory = api_factory
        self.editor_opener = editor_opener
        self.monotonic = monotonic
        self.sleep = sleep
        self.editor_selector = editor_selector
        self.editor_choice_saver = editor_choice_saver
        self.external_template_approver = external_template_approver

    def run(self, uri: str) -> SessionResult:
        return self.run_prepared(self.prepare(uri))

    def prepare(self, uri: str) -> PreparedSession:
        target = parse_launch_uri(uri, self.config.trusted_origins)
        api = self.api_factory(target.endpoint, target.entity, self.config.api_timeout_seconds)
        exchange = api.exchange(target.ticket)
        # The launch ticket and URI remain process-memory-only and are no longer needed.
        prepared = PreparedSession(target.endpoint, target.origin, target.entity, exchange)
        prepared.validate(self.config.trusted_origins)
        return prepared

    def run_prepared(self, prepared: PreparedSession) -> SessionResult:
        prepared.validate(self.config.trusted_origins)
        api = self.api_factory(prepared.endpoint, prepared.entity, self.config.api_timeout_seconds)
        exchange = prepared.exchange
        access_token = exchange.access_token
        lease_version = exchange.lease_version
        workspace = RecoveryWorkspace.create(
            self.recovery_root,
            exchange.filename,
            prepared.origin,
            prepared.entity,
            exchange.session_uuid,
        )
        upload_started = False
        preserve_open_editor = False
        try:
            temporary = workspace.download_path()
            api.download(access_token, temporary, exchange.base_sha256, exchange.byte_size)
            workspace.finish_download(temporary, exchange.base_sha256)
            initial_signature = file_signature(workspace.document)
            extension = workspace.document.suffix.lower()
            editor_command = self.config.editor_for(extension)
            selected_choice = None
            if self.config.should_ask_for_editor(extension):
                selected_choice = self.editor_selector(workspace.document)
                if selected_choice is not None:
                    editor_command = selected_choice.command
                    if selected_choice.remember:
                        self.editor_choice_saver(extension, editor_command)
            editor = self.editor_opener(workspace.document, editor_command)
            if selected_choice is not None or not self.config.editor_exit_is_reliable(extension):
                # GUI suites frequently delegate to an already running process.
                # An automatically discovered command is therefore watched as
                # an untracked association, even if its short bootstrap exits.
                editor = EditorHandle(editor.process, False)
            preserve_open_editor = True
            candidate_signature: tuple[int, int, int] | None = None
            candidate_since = 0.0
            missing_since: float | None = None
            now = self.monotonic()
            next_heartbeat = now + max(1, exchange.heartbeat_seconds)
            hard_deadline = now + 43_140

            while True:
                now = self.monotonic()
                editor_finished = (
                    editor.reliable_exit
                    and editor.process is not None
                    and editor.process.poll() is not None
                )
                preserve_open_editor = not editor_finished
                try:
                    current_signature = file_signature(workspace.document)
                except FileNotFoundError:
                    # Some editors remove the previous file before moving the
                    # saved revision into place. Keep the lease during that gap.
                    current_signature = None
                if now >= hard_deadline:
                    if current_signature == initial_signature:
                        self._expire_unchanged(workspace, api, access_token, lease_version, preserve_open_editor)
                        return SessionResult("expired_unchanged", workspace.document.name)
                    raise ApiError("session_timeout", "La durée maximale de la session locale est atteinte.")
                if now >= next_heartbeat:
                    lease_version, lease_expiry = api.heartbeat(access_token, lease_version)
                    workspace.update_status("editing", lease_expires_at=lease_expiry)
                    next_heartbeat = now + max(1, exchange.heartbeat_seconds)

                if current_signature is None:
                    candidate_signature = None
                    if missing_since is None:
                        missing_since = now
                    elif now - missing_since >= 30:
                        raise WorkspaceError("local_io_failed", "Le document local reste introuvable après la sauvegarde.")
                    self.sleep(self.config.poll_seconds)
                    continue
                missing_since = None
                changed = current_signature != initial_signature
                if changed:
                    if candidate_signature != current_signature:
                        candidate_signature = current_signature
                        candidate_since = now
                    elif (
                        now - candidate_since >= self.config.stable_seconds
                        and (not editor.reliable_exit or editor_finished)
                    ):
                        prepared = workspace.stable_snapshot()
                        if prepared is None:
                            candidate_signature = None
                            self.sleep(self.config.poll_seconds)
                            continue
                        snapshot, snapshot_sha256 = prepared
                        if snapshot_sha256 == exchange.base_sha256:
                            initial_signature = current_signature
                            candidate_signature = None
                        else:
                            upload_started = True
                            workspace.update_status("uploading", sha256=snapshot_sha256)
                            try:
                                uploaded = api.upload(
                                    access_token,
                                    snapshot,
                                    exchange.base_sha256,
                                    lease_version,
                                )
                            except ExternalTemplateApprovalRequired as challenge:
                                # This structured response is emitted before any server mutation.
                                upload_started = False
                                if not self.external_template_approver(challenge.target, challenge.scope):
                                    raise ApiError(
                                        "external_template_declined",
                                        "La publication a été annulée car le nouveau modèle Word n’a pas été approuvé.",
                                    ) from challenge
                                upload_started = True
                                try:
                                    uploaded = api.upload(
                                        access_token,
                                        snapshot,
                                        exchange.base_sha256,
                                        lease_version,
                                        challenge.approval,
                                    )
                                except ExternalTemplateApprovalRequired as retry_challenge:
                                    upload_started = False
                                    raise ApiError(
                                        "external_template_changed",
                                        "Dolibarr n’a pas accepté la confirmation du nouveau modèle Word.",
                                    ) from retry_challenge
                            if uploaded.sha256 != snapshot_sha256:
                                raise ApiError("upload_mismatch", "L’empreinte publiée ne correspond pas au fichier local.")
                            workspace.update_status("awaiting_completion", sha256=uploaded.sha256)
                            api.complete(access_token, lease_version, uploaded.sha256)
                            name = workspace.document.name
                            if editor_finished and file_sha256(workspace.document) == uploaded.sha256:
                                workspace.discard()
                                return SessionResult("completed", name)
                            workspace.update_status(
                                "published_recovery",
                                sha256=uploaded.sha256,
                                editor_exit_untracked=not editor.reliable_exit,
                            )
                            return SessionResult("completed_recovery", name)
                else:
                    candidate_signature = None

                if editor_finished and not changed:
                    api.cancel(access_token, lease_version)
                    name = workspace.document.name
                    workspace.discard()
                    return SessionResult("cancelled", name)
                self.sleep(self.config.poll_seconds)
        except KeyboardInterrupt as exc:
            outcome = self._recover_or_discard(
                workspace, api, access_token, lease_version, exchange.base_sha256, upload_started, preserve_open_editor
            )
            error = LauncherError("cancelled", "La session locale a été interrompue.")
            self._add_error_context(error, workspace, outcome)
            raise error from exc
        except LauncherError as exc:
            outcome = self._recover_or_discard(
                workspace, api, access_token, lease_version, exchange.base_sha256, upload_started, preserve_open_editor
            )
            self._add_error_context(exc, workspace, outcome)
            raise
        except OSError as exc:
            outcome = self._recover_or_discard(
                workspace, api, access_token, lease_version, exchange.base_sha256, upload_started, preserve_open_editor
            )
            error = WorkspaceError("local_io_failed", "Le document local est inaccessible.")
            self._add_error_context(error, workspace, outcome)
            raise error from exc

    @staticmethod
    def _expire_unchanged(
        workspace: RecoveryWorkspace,
        api: ApiLike,
        access_token: str,
        lease_version: int,
        preserve_open_editor: bool,
    ) -> None:
        """End quietly while preserving any copy that may still receive an editor save."""
        cancelled = False
        try:
            api.cancel(access_token, lease_version)
            cancelled = True
        except LauncherError:
            # The lease remains bounded server-side and expires without user action.
            pass
        if not preserve_open_editor:
            try:
                workspace.discard()
                return
            except (LauncherError, OSError):
                # An editor may keep the downloaded file locked after exit.
                pass
        try:
            workspace.update_status(
                "expired_unchanged",
                server_cancelled=cancelled,
                modified=False,
                editor_may_be_open=preserve_open_editor,
            )
        except OSError:
            pass

    @staticmethod
    def _recover_or_discard(
        workspace: RecoveryWorkspace,
        api: ApiLike,
        access_token: str,
        lease_version: int,
        base_sha256: str,
        upload_started: bool,
        preserve_open_editor: bool,
    ) -> RecoveryOutcome:
        modified = True
        try:
            modified = file_sha256(workspace.document) != base_sha256
        except (OSError, WorkspaceError):
            pass
        cancelled = False
        if not upload_started:
            try:
                api.cancel(access_token, lease_version)
                cancelled = True
            except Exception:
                pass
        if not modified and cancelled and not preserve_open_editor:
            try:
                workspace.discard()
            except (LauncherError, OSError):
                pass
            else:
                return RecoveryOutcome(None, False, True)
        try:
            workspace.update_status(
                "recovery_required",
                server_cancelled=cancelled,
                modified=modified,
            )
        except OSError:
            pass
        retained = workspace.directory if workspace.directory.is_dir() else None
        return RecoveryOutcome(retained, modified, cancelled)

    @staticmethod
    def _add_error_context(
        error: LauncherError,
        workspace: RecoveryWorkspace,
        outcome: RecoveryOutcome,
    ) -> None:
        error.add_document_context(
            workspace.document.name,
            recovery_directory=str(outcome.retained_directory) if outcome.retained_directory is not None else None,
            local_changes=outcome.modified_on_disk,
            server_cancelled=outcome.server_cancelled,
        )


def _safe_short_string(value: object, maximum: int) -> bool:
    return isinstance(value, str) and 0 < len(value) <= maximum and not any(ord(char) < 32 for char in value)
