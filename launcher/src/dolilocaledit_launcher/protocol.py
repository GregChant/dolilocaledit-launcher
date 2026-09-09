"""Strict parsing of short-lived Doli Local Edit launch URIs."""

from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import re
from urllib.parse import parse_qs, urlsplit, urlunsplit

from .errors import ProtocolError


_TICKET = re.compile(r"^[A-Za-z0-9_-]{43}$")
_API_SUFFIX = "/dolilocaledit/public/api.php"
_DNS_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_PATH_SEGMENT = re.compile(r"^[A-Za-z0-9._~-]+$")


@dataclass(frozen=True)
class LaunchTarget:
    endpoint: str
    origin: str
    entity: int
    ticket: str


def normalize_origin(value: str) -> str:
    """Return a canonical HTTPS origin, or loopback HTTP origin."""
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ProtocolError("invalid_origin", "L’origine Dolibarr est invalide.") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ProtocolError("invalid_origin", "L’origine Dolibarr est invalide.")
    hostname = _canonical_hostname(parsed.hostname)
    if parsed.scheme == "http" and not _is_loopback(hostname):
        raise ProtocolError("insecure_origin", "HTTPS est obligatoire hors boucle locale.")
    if ":" in hostname:
        host = f"[{hostname}]"
    else:
        host = hostname
    default_port = 443 if parsed.scheme == "https" else 80
    authority = host if port in {None, default_port} else f"{host}:{port}"
    return f"{parsed.scheme}://{authority}"


def inspect_launch_uri(uri: str) -> LaunchTarget:
    """Validate a launch URI without deciding whether its origin is trusted."""
    if not isinstance(uri, str) or len(uri) > 4096:
        raise ProtocolError("invalid_uri", "Le lien de lancement est invalide.")
    try:
        parsed = urlsplit(uri)
    except ValueError as exc:
        raise ProtocolError("invalid_uri", "Le lien de lancement est invalide.") from exc
    if (
        parsed.scheme != "dolilocaledit"
        or parsed.netloc != "open"
        or parsed.path not in {"", "/"}
        or parsed.fragment
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ProtocolError("invalid_uri", "Le lien de lancement est invalide.")
    try:
        parameters = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
    except ValueError as exc:
        raise ProtocolError("invalid_uri", "Les paramètres du lien sont invalides.") from exc
    if set(parameters) != {"endpoint", "entity", "ticket"} or any(len(values) != 1 for values in parameters.values()):
        raise ProtocolError("invalid_uri", "Les paramètres du lien sont invalides.")
    ticket = parameters["ticket"][0]
    entity_text = parameters["entity"][0]
    if not _TICKET.fullmatch(ticket) or not entity_text.isascii() or not entity_text.isdigit():
        raise ProtocolError("invalid_uri", "Les paramètres du lien sont invalides.")
    entity = int(entity_text)
    if entity < 1 or entity > 2_147_483_647:
        raise ProtocolError("invalid_uri", "L’entité Dolibarr est invalide.")
    endpoint, origin = validate_endpoint(parameters["endpoint"][0])
    return LaunchTarget(endpoint=endpoint, origin=origin, entity=entity, ticket=ticket)


def parse_launch_uri(uri: str, trusted_origins: tuple[str, ...]) -> LaunchTarget:
    """Parse a launch URI without ever logging or persisting its ticket."""
    target = inspect_launch_uri(uri)
    normalized_trust = tuple(normalize_origin(item) for item in trusted_origins)
    if target.origin not in normalized_trust:
        raise ProtocolError(
            "untrusted_origin",
            "Ce serveur Dolibarr n’est pas dans la liste locale des origines approuvées.",
        )
    return target


def validate_endpoint(value: str) -> tuple[str, str]:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ProtocolError("invalid_endpoint", "Le point d’accès Dolibarr est invalide.") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or not parsed.path.endswith(_API_SUFFIX)
    ):
        raise ProtocolError("invalid_endpoint", "Le point d’accès Dolibarr est invalide.")
    hostname = _canonical_hostname(parsed.hostname)
    path = _validated_endpoint_path(parsed.path)
    if parsed.scheme == "http" and not _is_loopback(hostname):
        raise ProtocolError("insecure_endpoint", "HTTPS est obligatoire hors boucle locale.")
    if ":" in hostname:
        host = f"[{hostname}]"
    else:
        host = hostname
    default_port = 443 if parsed.scheme == "https" else 80
    authority = host if port in {None, default_port} else f"{host}:{port}"
    origin = f"{parsed.scheme}://{authority}"
    endpoint = urlunsplit((parsed.scheme, authority, path, "", ""))
    return endpoint, origin


def _is_loopback(hostname: str) -> bool:
    if hostname == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def _canonical_hostname(value: str) -> str:
    """Return an unambiguous ASCII host suitable for approval and TLS."""
    hostname = value.rstrip(".").lower()
    if not hostname or "%" in hostname:
        raise ProtocolError("invalid_endpoint", "Le nom d’hôte Dolibarr est invalide.")
    try:
        return ipaddress.ip_address(hostname).compressed.lower()
    except ValueError:
        pass
    try:
        hostname = hostname.encode("idna").decode("ascii").lower()
    except (UnicodeError, UnicodeDecodeError) as exc:
        raise ProtocolError("invalid_endpoint", "Le nom d’hôte Dolibarr est invalide.") from exc
    labels = hostname.split(".")
    if len(hostname) > 253 or any(not _DNS_LABEL.fullmatch(label) for label in labels):
        raise ProtocolError("invalid_endpoint", "Le nom d’hôte Dolibarr est invalide.")
    return hostname


def _validated_endpoint_path(value: str) -> str:
    """Reject URL path ambiguities before urllib or a reverse proxy sees them."""
    if not value.isascii() or len(value) > 2048 or "%" in value or "\\" in value:
        raise ProtocolError("invalid_endpoint", "Le chemin du point d’accès Dolibarr est invalide.")
    parts = value.split("/")
    if (
        not value.startswith("/")
        or any(part in {"", ".", ".."} or not _PATH_SEGMENT.fullmatch(part) for part in parts[1:])
        or not value.endswith(_API_SUFFIX)
    ):
        raise ProtocolError("invalid_endpoint", "Le chemin du point d’accès Dolibarr est invalide.")
    return value
