import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from urllib.parse import urlencode

from dolilocaledit_launcher.api import ExchangeResult, UploadResult
from dolilocaledit_launcher.config import LauncherConfig
from dolilocaledit_launcher.editor import EditorChoice, EditorHandle
from dolilocaledit_launcher.errors import ApiError, ExternalTemplateApprovalRequired, LauncherError
from dolilocaledit_launcher.session import EditingSessionRunner, PreparedSession


class FinishedProcess:
    def poll(self) -> int:
        return 0


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def monotonic(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.value += seconds


class FakeApi:
    original = b"original\n"

    def __init__(self, fail_complete: bool = False) -> None:
        self.fail_complete = fail_complete
        self.events: list[str] = []

    def exchange(self, ticket: str) -> ExchangeResult:
        self.events.append("exchange")
        return ExchangeResult(
            "A" * 43,
            "2026-09-03T12:00:00Z",
            "session-uuid",
            "sample.txt",
            hashlib.sha256(self.original).hexdigest(),
            len(self.original),
            1,
            "2026-09-03T01:00:00Z",
            30,
        )

    def download(self, token: str, destination: Path, expected_sha256: str, expected_size: int) -> None:
        self.events.append("download")
        destination.write_bytes(self.original)

    def heartbeat(self, token: str, lease_version: int) -> tuple[int, str]:
        self.events.append("heartbeat")
        return lease_version + 1, "2026-09-03T01:01:00Z"

    def upload(
        self,
        token: str,
        source: Path,
        base_sha256: str,
        lease_version: int,
        external_template_approval: str | None = None,
    ) -> UploadResult:
        self.events.append("upload")
        content = source.read_bytes()
        return UploadResult(hashlib.sha256(content).hexdigest(), len(content), "text/plain", lease_version)

    def complete(self, token: str, lease_version: int, current_sha256: str) -> None:
        self.events.append("complete")
        if self.fail_complete:
            raise ApiError("network", "failure")

    def cancel(self, token: str, lease_version: int) -> None:
        self.events.append("cancel")


def launch_uri() -> str:
    return "dolilocaledit://open?" + urlencode({
        "endpoint": "https://erp.example/custom/dolilocaledit/public/api.php",
        "entity": "1",
        "ticket": "T" * 43,
    })


class SessionTest(unittest.TestCase):
    def runner(
        self,
        root: Path,
        api: FakeApi,
        clock: FakeClock,
        external_template_approver=lambda _target, _scope: False,
    ) -> EditingSessionRunner:
        config = LauncherConfig(("https://erp.example",), {}, 0.2, 0.5, 5)

        def factory(endpoint: str, entity: int, timeout: float) -> FakeApi:
            return api

        def editor(document: Path, command: tuple[str, ...] | None) -> EditorHandle:
            document.write_bytes(b"edited\n")
            return EditorHandle(FinishedProcess(), True)

        return EditingSessionRunner(
            config,
            root,
            factory,
            editor,
            clock.monotonic,
            clock.sleep,
            editor_selector=lambda _document: None,
            external_template_approver=external_template_approver,
        )

    def test_confirms_changed_template_then_retries_the_same_snapshot(self) -> None:
        class TemplateApi(FakeApi):
            def upload(
                self,
                token: str,
                source: Path,
                base_sha256: str,
                lease_version: int,
                external_template_approval: str | None = None,
            ) -> UploadResult:
                self.events.append("upload:" + (external_template_approval or "challenge"))
                if external_template_approval is None:
                    raise ExternalTemplateApprovalRequired(
                        "file:///C:/Templates/local.dotx", "local", "c" * 64
                    )
                content = source.read_bytes()
                return UploadResult(hashlib.sha256(content).hexdigest(), len(content), "application/zip", lease_version)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "recovery"
            api = TemplateApi()
            prompts: list[tuple[str, str]] = []
            result = self.runner(
                root,
                api,
                FakeClock(),
                lambda target, scope: prompts.append((target, scope)) or True,
            ).run(launch_uri())
            self.assertEqual(result.status, "completed")
            self.assertEqual(prompts, [("file:///C:/Templates/local.dotx", "local")])
            self.assertEqual(
                api.events,
                ["exchange", "download", "upload:challenge", "upload:" + "c" * 64, "complete"],
            )

    def test_declined_template_change_releases_session_and_keeps_local_copy(self) -> None:
        class TemplateApi(FakeApi):
            def upload(
                self,
                token: str,
                source: Path,
                base_sha256: str,
                lease_version: int,
                external_template_approval: str | None = None,
            ) -> UploadResult:
                self.events.append("upload")
                raise ExternalTemplateApprovalRequired(
                    "https://templates.example/remote.dotx", "remote", "d" * 64
                )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "recovery"
            api = TemplateApi()
            with self.assertRaises(ApiError) as context:
                self.runner(root, api, FakeClock()).run(launch_uri())
            self.assertEqual(context.exception.code, "external_template_declined")
            self.assertTrue(context.exception.server_cancelled)
            self.assertIsNotNone(context.exception.recovery_directory)
            self.assertEqual(api.events, ["exchange", "download", "upload", "cancel"])

    def test_uploads_one_stable_save_and_removes_confirmed_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "recovery"
            api = FakeApi()
            result = self.runner(root, api, FakeClock()).run(launch_uri())
            self.assertEqual(result.status, "completed")
            self.assertEqual(api.events, ["exchange", "download", "upload", "complete"])
            self.assertEqual(list(root.iterdir()), [])

    def test_preserves_recovery_until_completion_is_confirmed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "recovery"
            api = FakeApi(fail_complete=True)
            with self.assertRaises(ApiError):
                self.runner(root, api, FakeClock()).run(launch_uri())
            recoveries = list(root.iterdir())
            self.assertEqual(len(recoveries), 1)
            manifest = json.loads((recoveries[0] / "recovery.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "recovery_required")
            self.assertEqual((recoveries[0] / "sample.txt").read_bytes(), b"edited\n")

    def test_untracked_default_editor_keeps_published_working_copy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "recovery"
            api = FakeApi()
            clock = FakeClock()
            config = LauncherConfig(("https://erp.example",), {}, 0.2, 0.5, 5)

            def factory(endpoint: str, entity: int, timeout: float) -> FakeApi:
                return api

            def editor(document: Path, command: tuple[str, ...] | None) -> EditorHandle:
                document.write_bytes(b"edited\n")
                return EditorHandle(None, False)

            result = EditingSessionRunner(
                config,
                root,
                factory,
                editor,
                clock.monotonic,
                clock.sleep,
                editor_selector=lambda _document: None,
            ).run(launch_uri())
            self.assertEqual(result.status, "completed_recovery")
            recovery = next(root.iterdir())
            manifest = json.loads((recovery / "recovery.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "published_recovery")
            self.assertEqual((recovery / "sample.txt").read_bytes(), b"edited\n")

    def test_automatic_editor_choice_can_be_remembered_without_trusting_its_exit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "recovery"
            api = FakeApi()
            clock = FakeClock()
            config = LauncherConfig(("https://erp.example",), {}, 0.2, 0.5, 5)
            command = (str((Path(temporary) / "office.exe").resolve()), "{file}")
            saved: list[tuple[str, tuple[str, ...]]] = []

            def factory(endpoint: str, entity: int, timeout: float) -> FakeApi:
                return api

            def editor(document: Path, selected: tuple[str, ...] | None) -> EditorHandle:
                self.assertEqual(selected, command)
                document.write_bytes(b"edited\n")
                return EditorHandle(FinishedProcess(), True)

            result = EditingSessionRunner(
                config,
                root,
                factory,
                editor,
                clock.monotonic,
                clock.sleep,
                editor_selector=lambda _document: EditorChoice(command, True),
                editor_choice_saver=lambda extension, selected: saved.append((extension, selected)) or Path(temporary),
            ).run(launch_uri())
            self.assertEqual(result.status, "completed_recovery")
            self.assertEqual(saved, [(".txt", command)])
            self.assertEqual(api.events, ["exchange", "download", "upload", "complete"])

    def test_system_default_choice_can_be_remembered_without_reasking(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "recovery"
            api = FakeApi()
            clock = FakeClock()
            config = LauncherConfig(("https://erp.example",), {}, 0.2, 0.5, 5)
            saved: list[tuple[str, tuple[str, ...] | None]] = []

            def factory(endpoint: str, entity: int, timeout: float) -> FakeApi:
                return api

            def editor(document: Path, selected: tuple[str, ...] | None) -> EditorHandle:
                self.assertIsNone(selected)
                document.write_bytes(b"edited\n")
                return EditorHandle(None, False)

            result = EditingSessionRunner(
                config,
                root,
                factory,
                editor,
                clock.monotonic,
                clock.sleep,
                editor_selector=lambda _document: EditorChoice(None, True),
                editor_choice_saver=lambda extension, selected: saved.append((extension, selected)) or Path(temporary),
            ).run(launch_uri())
            self.assertEqual(result.status, "completed_recovery")
            self.assertEqual(saved, [(".txt", None)])

    def test_unchanged_untracked_session_expires_but_keeps_the_open_document(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "recovery"
            api = FakeApi()
            clock = FakeClock()
            config = LauncherConfig(("https://erp.example",), {}, 50_000, 60_000, 5)

            def factory(endpoint: str, entity: int, timeout: float) -> FakeApi:
                return api

            result = EditingSessionRunner(
                config,
                root,
                factory,
                lambda _document, _command: EditorHandle(None, False),
                clock.monotonic,
                clock.sleep,
                editor_selector=lambda _document: None,
            ).run(launch_uri())

            self.assertEqual(result.status, "expired_unchanged")
            self.assertEqual(result.filename, "sample.txt")
            self.assertEqual(api.events, ["exchange", "download", "cancel"])
            recovery = next(root.iterdir())
            document = recovery / "sample.txt"
            self.assertEqual(document.read_bytes(), api.original)
            manifest = json.loads((recovery / "recovery.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "expired_unchanged")
            self.assertTrue(manifest["editor_may_be_open"])
            # A system-associated editor can still save after the worker exits.
            document.write_bytes(b"saved after the session expired\n")
            self.assertEqual(document.read_bytes(), b"saved after the session expired\n")

    def test_heartbeat_failure_keeps_unchanged_document_in_a_running_tracked_editor(self) -> None:
        class RunningProcess:
            def poll(self) -> None:
                return None

        class OfflineApi(FakeApi):
            def heartbeat(self, token: str, lease_version: int) -> tuple[int, str]:
                raise ApiError("network", "failure")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "recovery"
            api = OfflineApi()
            clock = FakeClock()
            runner = self.runner(root, api, clock)
            runner.editor_opener = lambda _document, _command: EditorHandle(RunningProcess(), True)
            with self.assertRaises(ApiError) as context:
                runner.run(launch_uri())
            error = context.exception
            self.assertFalse(error.local_changes)
            self.assertTrue(error.server_cancelled)
            self.assertIsNotNone(error.recovery_directory)
            recovery = Path(error.recovery_directory)
            self.assertEqual((recovery / "sample.txt").read_bytes(), api.original)

    def test_save_that_temporarily_removes_the_file_is_published_after_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "recovery"
            api = FakeApi()
            clock = FakeClock()
            runner = self.runner(root, api, clock)
            documents: list[Path] = []

            def editor(document: Path, _command: tuple[str, ...] | None) -> EditorHandle:
                documents.append(document)
                document.unlink()
                return EditorHandle(None, False)

            def save_after_gap(seconds: float) -> None:
                clock.sleep(seconds)
                if clock.value >= 1 and not documents[0].exists():
                    documents[0].write_bytes(b"saved replacement\n")

            runner.editor_opener = editor
            runner.sleep = save_after_gap
            result = runner.run(launch_uri())
            self.assertEqual(result.status, "completed_recovery")
            self.assertEqual(api.events, ["exchange", "download", "upload", "complete"])
            self.assertEqual(documents[0].read_bytes(), b"saved replacement\n")

    def test_permanently_missing_document_stops_after_a_bounded_grace_period(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "recovery"
            api = FakeApi()
            clock = FakeClock()
            runner = self.runner(root, api, clock)

            def editor(document: Path, _command: tuple[str, ...] | None) -> EditorHandle:
                document.rename(document.with_suffix(".backup"))
                return EditorHandle(FinishedProcess(), True)

            runner.editor_opener = editor
            with self.assertRaises(LauncherError) as context:
                runner.run(launch_uri())
            self.assertEqual(context.exception.code, "local_io_failed")
            self.assertGreaterEqual(clock.value, 30)
            self.assertLess(clock.value, 31)
            self.assertIn("heartbeat", api.events)
            self.assertEqual(api.events[-1], "cancel")
            self.assertEqual((next(root.iterdir()) / "sample.backup").read_bytes(), api.original)

    def test_timeout_with_saved_changes_keeps_recovery_and_error_context(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "recovery"
            api = FakeApi()
            clock = FakeClock()
            config = LauncherConfig(("https://erp.example",), {}, 50_000, 60_000, 5)

            def factory(endpoint: str, entity: int, timeout: float) -> FakeApi:
                return api

            def editor(document: Path, _command: tuple[str, ...] | None) -> EditorHandle:
                document.write_bytes(b"edited near timeout\n")
                return EditorHandle(None, False)

            runner = EditingSessionRunner(
                config,
                root,
                factory,
                editor,
                clock.monotonic,
                clock.sleep,
                editor_selector=lambda _document: None,
            )
            with self.assertRaises(ApiError) as context:
                runner.run(launch_uri())

            error = context.exception
            self.assertEqual(error.code, "session_timeout")
            self.assertEqual(error.document_name, "sample.txt")
            self.assertTrue(error.local_changes)
            self.assertTrue(error.server_cancelled)
            self.assertIsNotNone(error.recovery_directory)
            assert error.recovery_directory is not None
            self.assertTrue(Path(error.recovery_directory).is_dir())
            self.assertEqual(api.events, ["exchange", "download", "cancel"])

    def test_worker_payload_is_validated_and_rechecked_against_trust(self) -> None:
        exchange = FakeApi().exchange("T" * 43)
        prepared = PreparedSession(
            "https://erp.example/custom/dolilocaledit/public/api.php",
            "https://erp.example",
            1,
            exchange,
        )
        restored = PreparedSession.from_payload(prepared.to_payload())
        restored.validate(("https://erp.example",))
        with self.assertRaises(LauncherError):
            restored.validate(())


if __name__ == "__main__":
    unittest.main()
