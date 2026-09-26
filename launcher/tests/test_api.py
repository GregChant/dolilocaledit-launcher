import hashlib
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from dolilocaledit_launcher import api as api_module
from dolilocaledit_launcher.api import DoliLocalEditApi
from dolilocaledit_launcher.errors import ApiError, ExternalTemplateApprovalRequired


class ApiHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    original = b"original\n"
    uploaded = b""
    authorization = ""
    redirect_download = False
    redirect_reached = False
    request_template_approval = False
    template_approval = ""
    include_download_length = True
    download_etag_mode = "strong"
    interrupt_chunked_download = False
    interrupt_chunked_json = False

    def log_message(self, format: str, *args: object) -> None:
        return

    def do_POST(self) -> None:
        action = parse_qs(urlsplit(self.path).query).get("action", [""])[0]
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        if action == "exchange":
            self.respond({
                "access_token": "A" * 43,
                "expires_at": "2026-09-03T12:00:00Z",
                "session_uuid": "session-uuid",
                "filename": "sample.txt",
                "base_sha256": hashlib.sha256(self.original).hexdigest(),
                "byte_size": len(self.original),
                "lease_version": 1,
                "lease_expires_at": "2026-09-03T01:00:00Z",
                "heartbeat_seconds": 30,
            })
            return
        if action == "launcher_check":
            if (
                body.get("ticket") == "T" * 43
                and body.get("launcher_version") == "1.0.3"
                and body.get("platform") == "linux"
            ):
                self.respond({"status": "confirmed"})
            else:
                self.respond({"error": "invalid_credential"}, 401)
            return
        self.__class__.authorization = self.headers.get("Authorization", "")
        if action == "heartbeat":
            self.respond({"lease_version": body["lease_version"] + 1, "lease_expires_at": "2026-09-03T01:01:00Z"})
        elif action == "complete":
            self.respond({"status": "completed"})
        elif action == "cancel":
            self.respond({"status": "cancelled"})
        else:
            self.send_error(404)

    def do_GET(self) -> None:
        self.__class__.authorization = self.headers.get("Authorization", "")
        if urlsplit(self.path).path == "/redirect-target":
            self.__class__.redirect_reached = True
            self.send_error(500)
            return
        if self.redirect_download:
            self.send_response(302)
            self.send_header("Location", "/redirect-target")
            self.end_headers()
            return
        digest = hashlib.sha256(self.original).hexdigest()
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        if self.include_download_length:
            self.send_header("Content-Length", str(len(self.original)))
        else:
            self.send_header("Transfer-Encoding", "chunked")
        if self.download_etag_mode == "strong":
            self.send_header("ETag", f'"{digest}"')
        elif self.download_etag_mode == "weak":
            self.send_header("ETag", f'W/"{digest}"')
        elif self.download_etag_mode == "wrong":
            self.send_header("ETag", '"' + "0" * 64 + '"')
        self.end_headers()
        if self.interrupt_chunked_download:
            self.wfile.write(f"{len(self.original):X}\r\n".encode("ascii"))
            self.wfile.write(self.original[:3])
            self.close_connection = True
            return
        if self.include_download_length:
            self.wfile.write(self.original)
        else:
            self.wfile.write(f"{len(self.original):X}\r\n".encode("ascii"))
            self.wfile.write(self.original + b"\r\n0\r\n\r\n")

    def do_PUT(self) -> None:
        self.__class__.authorization = self.headers.get("Authorization", "")
        length = int(self.headers["Content-Length"])
        self.__class__.uploaded = self.rfile.read(length)
        self.__class__.template_approval = self.headers.get(
            "X-DoliLocalEdit-External-Template-Approval", ""
        )
        if self.request_template_approval and not self.template_approval:
            self.respond({
                "error": "external_template_changed",
                "external_template_target": "file:///C:/Templates/local.dotx",
                "external_template_scope": "local",
                "external_template_approval": "b" * 64,
            }, 422)
            return
        self.respond({
            "sha256": hashlib.sha256(self.uploaded).hexdigest(),
            "byte_size": len(self.uploaded),
            "mime_type": "text/plain",
            "lease_version": int(self.headers["X-DoliLocalEdit-Lease-Version"]),
        })

    def respond(self, payload: dict[str, object], status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        if self.interrupt_chunked_json:
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            self.wfile.write(f"{len(body):X}\r\n".encode("ascii"))
            self.wfile.write(body[:3])
            self.close_connection = True
            return
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), ApiHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def test_full_bearer_api_contract(self) -> None:
        endpoint = f"http://127.0.0.1:{self.server.server_port}/custom/dolilocaledit/public/api.php"
        api = DoliLocalEditApi(endpoint, 1, 5)
        exchange = api.exchange("T" * 43)
        with tempfile.TemporaryDirectory() as temporary:
            document = Path(temporary) / "download"
            api.download(exchange.access_token, document, exchange.base_sha256, exchange.byte_size)
            self.assertEqual(document.read_bytes(), ApiHandler.original)
            document.write_bytes(b"edited\n")
            lease_version, _ = api.heartbeat(exchange.access_token, exchange.lease_version)
            uploaded = api.upload(exchange.access_token, document, exchange.base_sha256, lease_version)
            self.assertEqual(ApiHandler.uploaded, b"edited\n")
            api.complete(exchange.access_token, lease_version, uploaded.sha256)
        self.assertEqual(ApiHandler.authorization, "Bearer " + "A" * 43)

    def test_launcher_check_posts_short_ticket_without_bearer(self) -> None:
        endpoint = f"http://127.0.0.1:{self.server.server_port}/custom/dolilocaledit/public/api.php"
        DoliLocalEditApi(endpoint, 1, 5).confirm_launcher_check("T" * 43, "1.0.3", "linux")
        with self.assertRaises(ApiError):
            DoliLocalEditApi(endpoint, 1, 5).confirm_launcher_check("short", "1.0.3", "linux")

    def test_external_template_change_requires_and_forwards_exact_approval(self) -> None:
        endpoint = f"http://127.0.0.1:{self.server.server_port}/custom/dolilocaledit/public/api.php"
        api = DoliLocalEditApi(endpoint, 1, 5)
        ApiHandler.request_template_approval = True
        ApiHandler.template_approval = ""
        try:
            with tempfile.TemporaryDirectory() as temporary:
                document = Path(temporary) / "changed.docx"
                document.write_bytes(b"changed template\n")
                with self.assertRaises(ExternalTemplateApprovalRequired) as context:
                    api.upload("A" * 43, document, "a" * 64, 3)
                challenge = context.exception
                self.assertEqual(challenge.target, "file:///C:/Templates/local.dotx")
                self.assertEqual(challenge.scope, "local")
                self.assertEqual(challenge.approval, "b" * 64)
                api.upload("A" * 43, document, "a" * 64, 3, challenge.approval)
            self.assertEqual(ApiHandler.template_approval, "b" * 64)
        finally:
            ApiHandler.request_template_approval = False

    def test_download_accepts_chunked_response_without_advisory_metadata(self) -> None:
        endpoint = f"http://127.0.0.1:{self.server.server_port}/custom/dolilocaledit/public/api.php"
        api = DoliLocalEditApi(endpoint, 1, 5)
        digest = hashlib.sha256(ApiHandler.original).hexdigest()
        ApiHandler.include_download_length = False
        ApiHandler.download_etag_mode = "missing"
        try:
            with tempfile.TemporaryDirectory() as temporary:
                document = Path(temporary) / "download"
                api.download("A" * 43, document, digest, len(ApiHandler.original))
                self.assertEqual(document.read_bytes(), ApiHandler.original)
        finally:
            ApiHandler.include_download_length = True
            ApiHandler.download_etag_mode = "strong"

    def test_download_accepts_weak_etag_after_exact_content_verification(self) -> None:
        endpoint = f"http://127.0.0.1:{self.server.server_port}/custom/dolilocaledit/public/api.php"
        api = DoliLocalEditApi(endpoint, 1, 5)
        digest = hashlib.sha256(ApiHandler.original).hexdigest()
        ApiHandler.download_etag_mode = "weak"
        try:
            with tempfile.TemporaryDirectory() as temporary:
                document = Path(temporary) / "download"
                api.download("A" * 43, document, digest, len(ApiHandler.original))
                self.assertEqual(document.read_bytes(), ApiHandler.original)
        finally:
            ApiHandler.download_etag_mode = "strong"

    def test_interrupted_chunked_download_has_a_visible_error_and_removes_partial_file(self) -> None:
        endpoint = f"http://127.0.0.1:{self.server.server_port}/custom/dolilocaledit/public/api.php"
        api = DoliLocalEditApi(endpoint, 1, 5)
        digest = hashlib.sha256(ApiHandler.original).hexdigest()
        with (
            patch.object(ApiHandler, "include_download_length", False),
            patch.object(ApiHandler, "interrupt_chunked_download", True),
            tempfile.TemporaryDirectory() as temporary,
        ):
            document = Path(temporary) / "download"
            with self.assertRaises(ApiError) as context:
                api.download("A" * 43, document, digest, len(ApiHandler.original))
            self.assertEqual(context.exception.code, "download_failed")
            self.assertFalse(document.exists())

    def test_interrupted_chunked_confirmation_has_a_recoverable_api_error(self) -> None:
        endpoint = f"http://127.0.0.1:{self.server.server_port}/custom/dolilocaledit/public/api.php"
        with patch.object(ApiHandler, "interrupt_chunked_json", True):
            with self.assertRaises(ApiError) as context:
                DoliLocalEditApi(endpoint, 1, 5).complete("A" * 43, 1, "a" * 64)
        self.assertEqual(context.exception.code, "api_failed")

    def test_download_still_rejects_a_conflicting_etag(self) -> None:
        endpoint = f"http://127.0.0.1:{self.server.server_port}/custom/dolilocaledit/public/api.php"
        api = DoliLocalEditApi(endpoint, 1, 5)
        digest = hashlib.sha256(ApiHandler.original).hexdigest()
        ApiHandler.download_etag_mode = "wrong"
        try:
            with tempfile.TemporaryDirectory() as temporary:
                document = Path(temporary) / "download"
                with self.assertRaises(ApiError) as context:
                    api.download("A" * 43, document, digest, len(ApiHandler.original))
                self.assertEqual(context.exception.code, "download_mismatch")
                self.assertFalse(document.exists())
        finally:
            ApiHandler.download_etag_mode = "strong"

    def test_refuses_redirect_instead_of_forwarding_bearer(self) -> None:
        endpoint = f"http://127.0.0.1:{self.server.server_port}/custom/dolilocaledit/public/api.php"
        api = DoliLocalEditApi(endpoint, 1, 5)
        ApiHandler.redirect_download = True
        ApiHandler.redirect_reached = False
        try:
            with tempfile.TemporaryDirectory() as temporary:
                with self.assertRaises(ApiError) as context:
                    api.download("A" * 43, Path(temporary) / "download", "0" * 64, 1)
            self.assertEqual(context.exception.status, 302)
            self.assertFalse(ApiHandler.redirect_reached)
        finally:
            ApiHandler.redirect_download = False

    def test_loopback_api_ignores_inherited_http_proxy(self) -> None:
        endpoint = f"http://127.0.0.1:{self.server.server_port}/custom/dolilocaledit/public/api.php"
        with patch.dict(
            os.environ,
            {"HTTP_PROXY": "http://127.0.0.1:9", "http_proxy": "http://127.0.0.1:9", "NO_PROXY": "", "no_proxy": ""},
            clear=False,
        ):
            exchange = DoliLocalEditApi(endpoint, 1, 5).exchange("T" * 43)
        self.assertEqual(exchange.access_token, "A" * 43)

    def test_windows_download_gets_internet_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            document = Path(temporary) / "document.docx"
            document.write_bytes(b"fixture")
            with patch.object(api_module.os, "name", "nt"):
                api_module._apply_windows_mark_of_the_web(
                    document,
                    "https://erp.example/custom/dolilocaledit/public/api.php",
                )
            marker = Path(f"{document}:Zone.Identifier")
            self.assertEqual(
                marker.read_text(encoding="ascii"),
                "[ZoneTransfer]\nZoneId=3\nHostUrl=https://erp.example/custom/dolilocaledit/public/api.php\n",
            )


if __name__ == "__main__":
    unittest.main()
