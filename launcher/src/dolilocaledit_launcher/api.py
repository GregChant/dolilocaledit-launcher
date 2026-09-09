"""Bearer-only HTTP client for the Doli Local Edit server API."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import ssl
import stat
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, ProxyHandler, Request, build_opener

from .errors import ApiError, ExternalTemplateApprovalRequired
from .protocol import validate_endpoint


_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_TOKEN = re.compile(r"^[A-Za-z0-9_-]{43}$")
_MAX_JSON_BYTES = 1_048_576
_MAX_DOCUMENT_BYTES = 1_073_741_824


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req: Request, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


@dataclass(frozen=True)
class ExchangeResult:
    access_token: str
    expires_at: str
    session_uuid: str
    filename: str
    base_sha256: str
    byte_size: int
    lease_version: int
    lease_expires_at: str
    heartbeat_seconds: int


@dataclass(frozen=True)
class UploadResult:
    sha256: str
    byte_size: int
    mime_type: str
    lease_version: int


class DoliLocalEditApi:
    def __init__(self, endpoint: str, entity: int, timeout_seconds: float = 30.0) -> None:
        self.endpoint = validate_endpoint(endpoint)[0]
        self.entity = entity
        self.timeout_seconds = timeout_seconds
        self._ssl_context = ssl.create_default_context()
        handlers: list[Any] = [_NoRedirect(), HTTPSHandler(context=self._ssl_context)]
        # A loopback development endpoint must never be redirected through an
        # HTTP_PROXY inherited from the browser or desktop session. HTTPS keeps
        # the operating system proxy support while retaining certificate checks.
        if urlsplit(self.endpoint).scheme == "http":
            handlers.append(ProxyHandler({}))
        self._opener = build_opener(*handlers)

    def exchange(self, ticket: str) -> ExchangeResult:
        payload = self._request_json("POST", "exchange", {"entity": self.entity, "ticket": ticket})
        result = ExchangeResult(
            access_token=_string(payload, "access_token", 43),
            expires_at=_string(payload, "expires_at", 64),
            session_uuid=_string(payload, "session_uuid", 64),
            filename=_string(payload, "filename", 255),
            base_sha256=_string(payload, "base_sha256", 64),
            byte_size=_positive_or_zero(payload, "byte_size"),
            lease_version=_positive(payload, "lease_version"),
            lease_expires_at=_string(payload, "lease_expires_at", 64),
            heartbeat_seconds=_positive(payload, "heartbeat_seconds"),
        )
        if not _TOKEN.fullmatch(result.access_token) or not _SHA256.fullmatch(result.base_sha256):
            raise ApiError("invalid_response", "La réponse d’échange Dolibarr est invalide.")
        if result.heartbeat_seconds > 3600 or result.byte_size > _MAX_DOCUMENT_BYTES:
            raise ApiError("invalid_response", "Les limites annoncées par Dolibarr sont invalides.")
        return result

    def download(self, access_token: str, destination: Path, expected_sha256: str, expected_size: int) -> None:
        request = Request(
            self._action_url("content"),
            method="GET",
            headers=self._headers(access_token),
        )
        try:
            with self._opener.open(request, timeout=self.timeout_seconds) as response:
                if (response.headers.get_content_type() or "").lower() != "application/octet-stream":
                    raise ApiError("download_mismatch", "Le type de réponse du document est invalide.")
                length = _header_int(response.headers.get("Content-Length"))
                etag = (response.headers.get("ETag") or "").strip('"')
                if length != expected_size or not _SHA256.fullmatch(etag) or etag != expected_sha256:
                    raise ApiError("download_mismatch", "Les métadonnées du document sont incohérentes.")
                digest = hashlib.sha256()
                written = 0
                with destination.open("xb") as stream:
                    try:
                        destination.chmod(0o600)
                    except OSError:
                        pass
                    while True:
                        chunk = response.read(65_536)
                        if not chunk:
                            break
                        written += len(chunk)
                        if written > expected_size:
                            raise ApiError("download_mismatch", "Le document reçu dépasse la taille annoncée.")
                        digest.update(chunk)
                        stream.write(chunk)
                    stream.flush()
                    os.fsync(stream.fileno())
                if written != expected_size or digest.hexdigest() != expected_sha256:
                    raise ApiError("download_mismatch", "L’empreinte du document reçu est invalide.")
                _apply_windows_mark_of_the_web(destination, self.endpoint)
        except ApiError:
            _safe_unlink(destination)
            raise
        except (HTTPError, URLError, OSError) as exc:
            _safe_unlink(destination)
            translated = self._translate_transport(exc, "download_failed", "Le téléchargement a échoué.")
            if isinstance(exc, HTTPError):
                exc.close()
            raise translated from exc

    def heartbeat(self, access_token: str, lease_version: int) -> tuple[int, str]:
        payload = self._request_json(
            "POST",
            "heartbeat",
            {"entity": self.entity, "lease_version": lease_version},
            access_token,
        )
        return _positive(payload, "lease_version"), _string(payload, "lease_expires_at", 64)

    def upload(
        self,
        access_token: str,
        source: Path,
        base_sha256: str,
        lease_version: int,
        external_template_approval: str | None = None,
    ) -> UploadResult:
        if external_template_approval is not None and not _SHA256.fullmatch(external_template_approval):
            raise ApiError("upload_invalid", "La confirmation du modèle Word est invalide.")
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(source, flags)
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode):
            os.close(descriptor)
            raise ApiError("upload_invalid", "L’instantané local est invalide.")
        size = details.st_size
        with os.fdopen(descriptor, "rb", closefd=True) as stream:
            headers = {
                **self._headers(access_token),
                "Content-Type": "application/octet-stream",
                "Content-Length": str(size),
                "If-Match": f'"{base_sha256}"',
                "X-DoliLocalEdit-Lease-Version": str(lease_version),
            }
            if external_template_approval is not None:
                headers["X-DoliLocalEdit-External-Template-Approval"] = external_template_approval
            request = Request(
                self._action_url("content"),
                data=_chunked_file(stream),
                method="PUT",
                headers=headers,
            )
            payload = self._open_json(request)
        result = UploadResult(
            sha256=_string(payload, "sha256", 64),
            byte_size=_positive_or_zero(payload, "byte_size"),
            mime_type=_string(payload, "mime_type", 255),
            lease_version=_positive(payload, "lease_version"),
        )
        if not _SHA256.fullmatch(result.sha256) or result.byte_size != size or result.lease_version != lease_version:
            raise ApiError("invalid_response", "La confirmation de dépôt Dolibarr est invalide.")
        return result

    def complete(self, access_token: str, lease_version: int, current_sha256: str) -> None:
        payload = self._request_json(
            "POST",
            "complete",
            {"entity": self.entity, "lease_version": lease_version, "current_sha256": current_sha256},
            access_token,
        )
        if payload.get("status") != "completed":
            raise ApiError("invalid_response", "La fin de session n’a pas été confirmée.")

    def cancel(self, access_token: str, lease_version: int) -> None:
        payload = self._request_json(
            "POST",
            "cancel",
            {"entity": self.entity, "lease_version": lease_version},
            access_token,
        )
        if payload.get("status") != "cancelled":
            raise ApiError("invalid_response", "L’annulation n’a pas été confirmée.")

    def _request_json(
        self,
        method: str,
        action: str,
        payload: dict[str, Any],
        access_token: str | None = None,
    ) -> dict[str, Any]:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        request = Request(
            self._action_url(action),
            data=body,
            method=method,
            headers={**self._headers(access_token), "Content-Type": "application/json", "Content-Length": str(len(body))},
        )
        return self._open_json(request)

    def _open_json(self, request: Request) -> dict[str, Any]:
        try:
            with self._opener.open(request, timeout=self.timeout_seconds) as response:
                if (response.headers.get_content_type() or "").lower() != "application/json":
                    raise ApiError("invalid_response", "Le type de réponse Dolibarr est invalide.")
                raw = response.read(_MAX_JSON_BYTES + 1)
                if len(raw) > _MAX_JSON_BYTES:
                    raise ApiError("invalid_response", "La réponse Dolibarr est trop volumineuse.")
        except ApiError:
            raise
        except (HTTPError, URLError, OSError) as exc:
            challenge = self._external_template_challenge(exc)
            translated = self._translate_transport(exc, "api_failed", "Dolibarr a refusé ou interrompu la requête.")
            if isinstance(exc, HTTPError):
                exc.close()
            if challenge is not None:
                raise challenge from exc
            raise translated from exc
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ApiError("invalid_response", "La réponse Dolibarr est invalide.") from exc
        if not isinstance(payload, dict):
            raise ApiError("invalid_response", "La réponse Dolibarr est invalide.")
        return payload

    def _action_url(self, action: str) -> str:
        parsed = urlsplit(self.endpoint)
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode({"action": action, "entity": self.entity}), ""))

    @staticmethod
    def _external_template_challenge(exc: BaseException) -> ExternalTemplateApprovalRequired | None:
        if (
            not isinstance(exc, HTTPError)
            or exc.code != 422
            or (exc.headers.get_content_type() or "").lower() != "application/json"
        ):
            return None
        try:
            raw = exc.read(_MAX_JSON_BYTES + 1)
            if len(raw) > _MAX_JSON_BYTES:
                return None
            payload = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict) or payload.get("error") != "external_template_changed":
            return None
        target = payload.get("external_template_target")
        scope = payload.get("external_template_scope")
        approval = payload.get("external_template_approval")
        if (
            not isinstance(target, str)
            or not 1 <= len(target) <= 2048
            or any(ord(character) < 32 or ord(character) == 127 for character in target)
            or scope not in {"local", "remote"}
            or (scope == "local" and not target.startswith("file:///"))
            or (scope == "remote" and not target.startswith("https://"))
            or not isinstance(approval, str)
            or not _SHA256.fullmatch(approval)
        ):
            return None
        return ExternalTemplateApprovalRequired(target, scope, approval)

    @staticmethod
    def _headers(access_token: str | None) -> dict[str, str]:
        headers = {
            "Accept": "application/json, application/octet-stream;q=0.9",
            "Cache-Control": "no-store",
            "User-Agent": "DoliLocalEdit-Launcher/0.1",
        }
        if access_token is not None:
            if not _TOKEN.fullmatch(access_token):
                raise ApiError("invalid_credential", "Le jeton de session est invalide.")
            headers["Authorization"] = f"Bearer {access_token}"
        return headers

    @staticmethod
    def _translate_transport(exc: BaseException, code: str, message: str) -> ApiError:
        status = exc.code if isinstance(exc, HTTPError) else None
        if status in {401, 403}:
            return ApiError("invalid_credential", "La session locale n’est plus autorisée.", status)
        if status == 409:
            return ApiError("document_conflict", "Le document ou sa location a changé.", status)
        if status == 413:
            return ApiError("upload_too_large", "Le document dépasse la taille autorisée.", status)
        if status == 429:
            return ApiError("rate_limited", "Trop de requêtes ont été envoyées à Dolibarr.", status)
        return ApiError(code, message, status)


def _chunked_file(stream: Any, size: int = 65_536) -> Iterable[bytes]:
    while True:
        chunk = stream.read(size)
        if not chunk:
            return
        yield chunk


def _string(payload: dict[str, Any], name: str, maximum: int) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value or len(value) > maximum or any(ord(char) < 32 for char in value):
        raise ApiError("invalid_response", "La réponse Dolibarr est incomplète.")
    return value


def _positive(payload: dict[str, Any], name: str) -> int:
    value = payload.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ApiError("invalid_response", "La réponse Dolibarr est incomplète.")
    return value


def _positive_or_zero(payload: dict[str, Any], name: str) -> int:
    value = payload.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ApiError("invalid_response", "La réponse Dolibarr est incomplète.")
    return value


def _header_int(value: str | None) -> int | None:
    return int(value) if value is not None and value.isascii() and value.isdigit() else None


def _apply_windows_mark_of_the_web(path: Path, endpoint: str) -> None:
    """Keep Office/PDF protected-view provenance on files downloaded by the launcher."""
    if os.name != "nt":
        return
    marker = f"{path}:Zone.Identifier"
    try:
        with open(marker, "x", encoding="ascii", newline="") as stream:
            stream.write(f"[ZoneTransfer]\r\nZoneId=3\r\nHostUrl={endpoint}\r\n")
    except OSError as exc:
        raise ApiError(
            "download_security_failed",
            "Windows n’a pas pu protéger la provenance du document téléchargé.",
        ) from exc


def _safe_unlink(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass
