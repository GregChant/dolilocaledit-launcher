from contextlib import redirect_stderr, redirect_stdout
from io import BytesIO
from io import StringIO
import hashlib
import json
import unittest
from unittest.mock import MagicMock, patch

from dolilocaledit_launcher.api import ExchangeResult
from dolilocaledit_launcher.cli import _spawn_worker, main
from dolilocaledit_launcher.errors import ApiError, ProtocolError
from dolilocaledit_launcher.protocol import LaunchTarget
from dolilocaledit_launcher.session import PreparedSession


class BrokenStream:
    def write(self, _value: str) -> int:
        raise OSError(22, "Invalid argument")

    def flush(self) -> None:
        raise OSError(22, "Invalid argument")


class CliTest(unittest.TestCase):
    def test_launcher_check_reports_version_and_platform_without_opening_document(self) -> None:
        config = MagicMock()
        config.trusted_origins = ("https://erp.example",)
        config.api_timeout_seconds = 10
        target = LaunchTarget(
            "https://erp.example/custom/dolilocaledit/public/api.php",
            "https://erp.example",
            1,
            "T" * 43,
        )
        api = MagicMock()
        with (
            patch("dolilocaledit_launcher.cli.load_config", return_value=config),
            patch("dolilocaledit_launcher.cli.parse_check_uri", return_value=target),
            patch("dolilocaledit_launcher.cli.DoliLocalEditApi", return_value=api),
            patch("dolilocaledit_launcher.cli.sys.platform", "linux"),
        ):
            with redirect_stdout(StringIO()):
                result = main(["open", "dolilocaledit://check/?redacted"])
        self.assertEqual(result, 0)
        api.confirm_launcher_check.assert_called_once_with("T" * 43, "1.0.4", "linux")

    def test_installation_failure_is_shown_explicitly(self) -> None:
        error = ApiError("installation_failed", "La copie du lanceur a échoué.")
        with (
            patch("dolilocaledit_launcher.cli.install_for_current_user", side_effect=error),
            patch("dolilocaledit_launcher.cli.show_installation_error") as show_error,
        ):
            with redirect_stderr(StringIO()):
                result = main(["install"])
        self.assertEqual(result, 2)
        show_error.assert_called_once_with("installation_failed", "La copie du lanceur a échoué.")

    def test_first_open_requires_explicit_origin_approval(self) -> None:
        runner = MagicMock()
        runner.prepare.side_effect = ProtocolError("untrusted_origin", "approval required")
        target = LaunchTarget(
            "https://erp.example/custom/dolilocaledit/public/api.php",
            "https://erp.example",
            1,
            "T" * 43,
        )
        with (
            patch("dolilocaledit_launcher.cli.load_config"),
            patch("dolilocaledit_launcher.cli.EditingSessionRunner", return_value=runner),
            patch("dolilocaledit_launcher.cli.inspect_launch_uri", return_value=target),
            patch("dolilocaledit_launcher.cli.confirm_origin_trust", return_value=False),
            patch("dolilocaledit_launcher.cli.trust_origin") as trust,
        ):
            with redirect_stderr(StringIO()):
                result = main(["open", "dolilocaledit://open?redacted"])
        self.assertEqual(result, 2)
        trust.assert_not_called()

    def test_approved_origin_is_persisted_before_exchange(self) -> None:
        untrusted = MagicMock()
        untrusted.prepare.side_effect = ProtocolError("untrusted_origin", "approval required")
        trusted = MagicMock()
        prepared = MagicMock()
        prepared.exchange.filename = "sample.txt"
        trusted.prepare.return_value = prepared
        target = LaunchTarget(
            "https://erp.example/custom/dolilocaledit/public/api.php",
            "https://erp.example",
            1,
            "T" * 43,
        )
        with (
            patch("dolilocaledit_launcher.cli.load_config", side_effect=[object(), object()]),
            patch("dolilocaledit_launcher.cli.EditingSessionRunner", side_effect=[untrusted, trusted]),
            patch("dolilocaledit_launcher.cli.inspect_launch_uri", return_value=target),
            patch("dolilocaledit_launcher.cli.confirm_origin_trust", return_value=True),
            patch("dolilocaledit_launcher.cli.trust_origin") as trust,
            patch("dolilocaledit_launcher.cli._spawn_worker") as spawn,
        ):
            with redirect_stdout(StringIO()):
                result = main(["open", "dolilocaledit://open?redacted"])
        self.assertEqual(result, 0)
        trust.assert_called_once_with("https://erp.example")
        spawn.assert_called_once_with(prepared)

    def test_frozen_windows_open_failure_is_shown_graphically(self) -> None:
        runner = MagicMock()
        runner.prepare.side_effect = ApiError("invalid_credential", "Session refusée.")
        with (
            patch("dolilocaledit_launcher.cli.load_config"),
            patch("dolilocaledit_launcher.cli.EditingSessionRunner", return_value=runner),
            patch("dolilocaledit_launcher.cli.sys.platform", "win32"),
            patch("dolilocaledit_launcher.cli.sys.frozen", True, create=True),
            patch("dolilocaledit_launcher.cli.sys.stderr", BrokenStream()),
            patch("dolilocaledit_launcher.cli.show_launcher_error") as show_error,
        ):
            result = main(["open", "dolilocaledit://open?redacted"])
        self.assertEqual(result, 2)
        show_error.assert_called_once_with("invalid_credential", "Session refusée.")

    def test_frozen_worker_error_includes_document_and_recovery_context(self) -> None:
        exchange = ExchangeResult(
            "A" * 43,
            "2026-09-05T12:00:00Z",
            "session-uuid",
            "contrat.docx",
            hashlib.sha256(b"original").hexdigest(),
            len(b"original"),
            1,
            "2026-09-05T01:00:00Z",
            30,
        )
        prepared = PreparedSession(
            "https://erp.example/custom/dolilocaledit/public/api.php",
            "https://erp.example",
            1,
            exchange,
        )
        standard_input = MagicMock()
        standard_input.buffer = BytesIO(json.dumps(prepared.to_payload()).encode("utf-8"))
        error = ApiError("document_conflict", "Le document a changé.").add_document_context(
            "contrat.docx",
            recovery_directory=r"C:\recovery\1234",
            local_changes=True,
            server_cancelled=True,
        )
        runner = MagicMock()
        runner.run_prepared.side_effect = error
        with (
            patch("dolilocaledit_launcher.cli.load_config"),
            patch("dolilocaledit_launcher.cli.EditingSessionRunner", return_value=runner),
            patch("dolilocaledit_launcher.cli.sys.platform", "win32"),
            patch("dolilocaledit_launcher.cli.sys.frozen", True, create=True),
            patch("dolilocaledit_launcher.cli.sys.stdin", standard_input),
            patch("dolilocaledit_launcher.cli.sys.stderr", BrokenStream()),
            patch("dolilocaledit_launcher.cli.show_launcher_error") as show_error,
        ):
            result = main(["_worker"])

        self.assertEqual(result, 2)
        show_error.assert_called_once_with(
            "document_conflict",
            "Le document a changé.",
            document_name="contrat.docx",
            server_origin="https://erp.example",
            recovery_directory=r"C:\recovery\1234",
            local_changes=True,
            server_cancelled=True,
        )

    def test_frozen_linux_open_failure_is_shown_graphically(self) -> None:
        runner = MagicMock()
        runner.prepare.side_effect = ApiError("invalid_credential", "Session refusée.")
        with (
            patch("dolilocaledit_launcher.cli.load_config"),
            patch("dolilocaledit_launcher.cli.EditingSessionRunner", return_value=runner),
            patch("dolilocaledit_launcher.cli.sys.platform", "linux"),
            patch("dolilocaledit_launcher.cli.sys.frozen", True, create=True),
            patch("dolilocaledit_launcher.cli.show_launcher_error") as show_error,
        ):
            with redirect_stderr(StringIO()):
                result = main(["open", "dolilocaledit://open?redacted"])
        self.assertEqual(result, 2)
        show_error.assert_called_once_with("invalid_credential", "Session refusée.")

    def test_frozen_worker_gets_an_independent_pyinstaller_environment(self) -> None:
        prepared = MagicMock()
        prepared.to_payload.return_value = {"schema": 1}
        process = MagicMock()
        process.stdin = BytesIO()
        with (
            patch("dolilocaledit_launcher.cli.sys.frozen", True, create=True),
            patch("dolilocaledit_launcher.cli.sys.executable", r"C:\DoliLocalEdit\launcher.exe"),
            patch.dict("dolilocaledit_launcher.cli.os.environ", {"DLE_TEST_ENV": "kept"}),
            patch("dolilocaledit_launcher.cli.subprocess.Popen", return_value=process) as popen,
        ):
            _spawn_worker(prepared)
        environment = popen.call_args.kwargs["env"]
        self.assertEqual(environment["PYINSTALLER_RESET_ENVIRONMENT"], "1")
        self.assertEqual(environment["DLE_TEST_ENV"], "kept")
        self.assertEqual(popen.call_args.args[0], [r"C:\DoliLocalEdit\launcher.exe", "_worker"])
        self.assertTrue(process.stdin.closed)

    def test_forget_editor_resets_a_local_extension(self) -> None:
        with patch("dolilocaledit_launcher.cli.forget_editor_choice") as forget:
            forget.return_value = r"C:\Users\test\AppData\Local\DoliLocalEdit\config.json"
            with redirect_stdout(StringIO()):
                result = main(["forget-editor", ".DOCX"])
        self.assertEqual(result, 0)
        forget.assert_called_once_with(".DOCX")


if __name__ == "__main__":
    unittest.main()
