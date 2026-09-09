"""Stable launcher errors that never contain credentials."""

from __future__ import annotations


class LauncherError(RuntimeError):
    """Expected, user-facing launcher failure."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.document_name: str | None = None
        self.recovery_directory: str | None = None
        self.local_changes: bool | None = None
        self.server_cancelled: bool | None = None
        super().__init__(message)

    def add_document_context(
        self,
        document_name: str,
        *,
        recovery_directory: str | None = None,
        local_changes: bool | None = None,
        server_cancelled: bool | None = None,
    ) -> "LauncherError":
        """Attach local-only diagnostic details without changing the stable error code."""
        self.document_name = document_name
        self.recovery_directory = recovery_directory
        self.local_changes = local_changes
        self.server_cancelled = server_cancelled
        return self


class ProtocolError(LauncherError):
    pass


class ConfigurationError(LauncherError):
    pass


class ApiError(LauncherError):
    def __init__(self, code: str, message: str, status: int | None = None) -> None:
        self.status = status
        super().__init__(code, message)


class ExternalTemplateApprovalRequired(ApiError):
    """A safe server challenge for one exact external-template upload."""

    def __init__(self, target: str, scope: str, approval: str) -> None:
        self.target = target
        self.scope = scope
        self.approval = approval
        super().__init__(
            "external_template_changed",
            "Le modèle Word lié au document a changé et nécessite votre confirmation.",
            422,
        )


class WorkspaceError(LauncherError):
    pass


class EditorError(LauncherError):
    pass
