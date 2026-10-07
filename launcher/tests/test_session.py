import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from urllib.parse import urlencode

from dolilocaledit_launcher.api import ExchangeResult, UploadResult
from dolilocaledit_launcher.config import LauncherConfig
from dolilocaledit_launcher.editor import EditorChoice, EditorHandle
from dolilocaledit_launcher.document_monitor import DocumentState
from dolilocaledit_launcher.errors import ApiError, ExternalTemplateApprovalRequired, LauncherError
from dolilocaledit_launcher.session import EditingSessionRunner, PreparedSession


class FakeMonitor:
    def __init__(self, state=lambda: DocumentState.UNKNOWN) -> None:
        self.state = state
        self.closed = False

    def poll(self) -> DocumentState:
        return self.state()

    def close(self) -> None:
        self.closed = True


class FakeFinish:
    def __init__(self, requested=lambda: True) -> None:
        self.is_requested = requested
        self.closed = False

    def requested(self) -> bool:
        return self.is_requested()

    def close(self) -> None:
        self.closed = True


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
        self.uploaded_bytes = content
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
            monitor_factory=lambda _document, _command: FakeMonitor(lambda: DocumentState.CLOSED),
            finish_factory=lambda _document: FakeFinish(),
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
                monitor_factory=lambda _document, _command: FakeMonitor(),
                finish_factory=lambda _document: FakeFinish(),
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
                monitor_factory=lambda _document, _command: FakeMonitor(),
                finish_factory=lambda _document: FakeFinish(),
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
                monitor_factory=lambda _document, _command: FakeMonitor(),
                finish_factory=lambda _document: FakeFinish(),
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
                monitor_factory=lambda _document, _command: FakeMonitor(),
                finish_factory=lambda _document: FakeFinish(),
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
            runner.monitor_factory = lambda _document, _command: FakeMonitor(lambda: DocumentState.OPEN)
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
            runner.monitor_factory = lambda _document, _command: FakeMonitor(
                lambda: DocumentState.CLOSED if clock.value >= 2 else DocumentState.OPEN
            )
            result = runner.run(launch_uri())
            self.assertEqual(result.status, "completed")
            self.assertEqual(api.events, ["exchange", "download", "upload", "complete"])
            self.assertEqual(api.uploaded_bytes, b"saved replacement\n")

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
                monitor_factory=lambda _document, _command: FakeMonitor(),
                finish_factory=lambda _document: FakeFinish(),
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

    def test_confirmed_close_without_changes_cancels_after_stability(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "recovery"
            api, clock = FakeApi(), FakeClock()
            runner = self.runner(root, api, clock)
            monitor = FakeMonitor(lambda: DocumentState.OPEN if clock.value < 2 else DocumentState.CLOSED)
            runner.monitor_factory = lambda _document, _command: monitor
            runner.editor_opener = lambda _document, _command: EditorHandle(None, False)
            result = runner.run(launch_uri())
            self.assertEqual(result.status, "cancelled")
            self.assertEqual(api.events, ["exchange", "download", "cancel"])
            self.assertGreaterEqual(clock.value, 2.5)
            self.assertEqual(list(root.iterdir()), [])
            self.assertTrue(monitor.closed)

    def test_open_document_keeps_lease_across_multiple_saves_then_publishes_final_save(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "recovery"
            api, clock = FakeApi(), FakeClock()
            runner = self.runner(root, api, clock)
            documents: list[Path] = []
            saved = set()

            def editor(document: Path, _command: tuple[str, ...] | None) -> EditorHandle:
                documents.append(document)
                document.write_bytes(b"first save\n")
                # Bootstrap exits while the document remains in the reused suite.
                return EditorHandle(FinishedProcess(), True)

            def sleep(seconds: float) -> None:
                clock.sleep(seconds)
                for deadline, content in ((10, b"second save\n"), (35, b"final save\n")):
                    if clock.value >= deadline and deadline not in saved:
                        documents[0].write_bytes(content)
                        saved.add(deadline)
                if clock.value < 40:
                    self.assertNotIn("upload", api.events)
                    self.assertNotIn("cancel", api.events)
                    self.assertNotIn("complete", api.events)

            runner.editor_opener = editor
            runner.sleep = sleep
            runner.monitor_factory = lambda _document, _command: FakeMonitor(
                lambda: DocumentState.OPEN if clock.value < 40 else DocumentState.CLOSED
            )
            result = runner.run(launch_uri())
            self.assertEqual(result.status, "completed")
            self.assertEqual(api.events, ["exchange", "download", "heartbeat", "upload", "complete"])
            self.assertEqual(api.uploaded_bytes, b"final save\n")
            self.assertGreaterEqual(clock.value, 40.5)

    def test_cancelled_close_resets_stability_before_eventual_close(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "recovery"
            api, clock = FakeApi(), FakeClock()
            runner = self.runner(root, api, clock)
            runner.editor_opener = lambda _document, _command: EditorHandle(None, False)

            def state() -> DocumentState:
                if 1 <= clock.value < 1.4 or clock.value >= 3:
                    return DocumentState.CLOSED
                self.assertNotIn("cancel", api.events)
                return DocumentState.OPEN

            runner.monitor_factory = lambda _document, _command: FakeMonitor(state)
            result = runner.run(launch_uri())
            self.assertEqual(result.status, "cancelled")
            self.assertGreaterEqual(clock.value, 3.5)

    def test_last_save_after_close_detection_is_included_even_when_initially_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "recovery"
            api, clock = FakeApi(), FakeClock()
            runner = self.runner(root, api, clock)
            documents: list[Path] = []
            written = False

            def editor(document: Path, _command: tuple[str, ...] | None) -> EditorHandle:
                documents.append(document)
                return EditorHandle(None, False)

            def sleep(seconds: float) -> None:
                nonlocal written
                clock.sleep(seconds)
                if clock.value >= 1.2 and not written:
                    documents[0].write_bytes(b"last delayed save\n")
                    written = True

            runner.editor_opener = editor
            runner.sleep = sleep
            runner.monitor_factory = lambda _document, _command: FakeMonitor(
                lambda: DocumentState.OPEN if clock.value < 1 else DocumentState.CLOSED
            )
            result = runner.run(launch_uri())
            self.assertEqual(result.status, "completed")
            self.assertEqual(api.uploaded_bytes, b"last delayed save\n")
            self.assertNotIn("cancel", api.events)

    def test_unknown_editor_needs_explicit_finish_and_keeps_working_copy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "recovery"
            api, clock = FakeApi(), FakeClock()
            runner = self.runner(root, api, clock)
            runner.monitor_factory = lambda _document, _command: FakeMonitor()
            documents: list[Path] = []
            created: list[float] = []
            control = FakeFinish(lambda: clock.value >= 32)

            def editor(document: Path, _command: tuple[str, ...] | None) -> EditorHandle:
                documents.append(document)
                document.write_bytes(b"edited\n")
                return EditorHandle(None, False)

            def finish(_document: Path) -> FakeFinish:
                created.append(clock.value)
                return control

            def sleep(seconds: float) -> None:
                clock.sleep(seconds)
                if clock.value < 32:
                    self.assertNotIn("upload", api.events)
                    self.assertNotIn("cancel", api.events)

            runner.editor_opener = editor
            runner.finish_factory = finish
            runner.sleep = sleep
            result = runner.run(launch_uri())
            self.assertEqual(result.status, "completed_recovery")
            self.assertEqual(api.events, ["exchange", "download", "heartbeat", "upload", "complete"])
            self.assertGreaterEqual(created[0], 8)
            self.assertTrue(control.closed)
            self.assertEqual(documents[0].read_bytes(), b"edited\n")

    def test_unknown_unchanged_explicit_finish_keeps_the_working_copy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "recovery"
            api, clock = FakeApi(), FakeClock()
            runner = self.runner(root, api, clock)
            runner.monitor_factory = lambda _document, _command: FakeMonitor()
            runner.editor_opener = lambda _document, _command: EditorHandle(None, False)
            result = runner.run(launch_uri())
            self.assertEqual(result.status, "cancelled")
            self.assertEqual(api.events, ["exchange", "download", "cancel"])
            recovery = next(root.iterdir())
            self.assertEqual((recovery / "sample.txt").read_bytes(), api.original)
            manifest = json.loads((recovery / "recovery.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "cancelled_recovery")

    def test_unknown_finish_control_closes_once_document_becomes_observable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "recovery"
            api, clock = FakeApi(), FakeClock()
            runner = self.runner(root, api, clock)
            control = FakeFinish(lambda: False)
            runner.editor_opener = lambda _document, _command: EditorHandle(None, False)
            runner.finish_factory = lambda _document: control
            runner.monitor_factory = lambda _document, _command: FakeMonitor(
                lambda: DocumentState.UNKNOWN if clock.value < 10 else (
                    DocumentState.OPEN if clock.value < 12 else DocumentState.CLOSED
                )
            )
            result = runner.run(launch_uri())
            self.assertEqual(result.status, "cancelled")
            self.assertTrue(control.closed)

    def test_upload_network_failure_preserves_recovery_and_does_not_complete_or_cancel(self) -> None:
        class OfflineUpload(FakeApi):
            def upload(self, *args, **kwargs) -> UploadResult:
                self.events.append("upload")
                raise ApiError("network", "failure")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "recovery"
            api = OfflineUpload()
            runner = self.runner(root, api, FakeClock())
            with self.assertRaises(ApiError) as context:
                runner.run(launch_uri())
            self.assertEqual(api.events, ["exchange", "download", "upload"])
            self.assertFalse(context.exception.server_cancelled)
            self.assertEqual((next(root.iterdir()) / "sample.txt").read_bytes(), b"edited\n")

    def test_unknown_gui_bootstrap_exit_never_releases_without_explicit_finish(self) -> None:
        for executable in ("WINWORD.EXE", "soffice.exe", "Acrobat.exe", "desktopeditors"):
            for initially_observable in (False, True):
                with self.subTest(executable=executable, initially_observable=initially_observable):
                    with tempfile.TemporaryDirectory() as temporary:
                        root = Path(temporary) / "recovery"
                        api, clock = FakeApi(), FakeClock()
                        runner = self.runner(root, api, clock)
                        runner.config = LauncherConfig(
                            ("https://erp.example",), {".txt": ("/local/" + executable, "{file}")},
                            0.2, 0.5, 5,
                        )
                        runner.monitor_factory = lambda _document, _command: FakeMonitor(
                            lambda: DocumentState.OPEN if initially_observable and clock.value < 1 else DocumentState.UNKNOWN
                        )
                        runner.finish_factory = lambda _document: FakeFinish(lambda: clock.value >= 40)

                        def sleep(seconds: float) -> None:
                            clock.sleep(seconds)
                            if clock.value < 40:
                                self.assertNotIn("upload", api.events)
                                self.assertNotIn("cancel", api.events)

                        runner.sleep = sleep
                        result = runner.run(launch_uri())
                        self.assertEqual(result.status, "completed_recovery")
                        self.assertIn("heartbeat", api.events)
                        self.assertEqual((next(root.iterdir()) / "sample.txt").read_bytes(), b"edited\n")

    def test_only_foreground_terminal_process_exit_can_finish_an_unknown_document(self) -> None:
        for command in (("/usr/bin/vim.basic", "{file}"), ("/usr/bin/vi", "{file}"), ("/usr/bin/nano", "{file}")):
            with self.subTest(command=command), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary) / "recovery"
                api, clock = FakeApi(), FakeClock()
                runner = self.runner(root, api, clock)
                runner.config = LauncherConfig(("https://erp.example",), {".txt": command}, 0.2, 0.5, 5)
                runner.monitor_factory = lambda _document, _command: FakeMonitor()
                runner.finish_factory = lambda _document: self.fail("Foreground terminal exit must be observable")
                result = runner.run(launch_uri())
                self.assertEqual(result.status, "completed")
                self.assertEqual(api.events, ["exchange", "download", "upload", "complete"])

    def test_terminal_remote_mode_and_prior_document_observation_require_explicit_finish(self) -> None:
        for command, observed_open in (
            (("/usr/bin/vim", "--remote", "{file}"), False),
            (("/usr/bin/vim", "-g", "{file}"), False),
            (("/usr/bin/nvim", "--server=local", "{file}"), False),
            (("/usr/bin/vim", "{file}"), True),
        ):
            with self.subTest(command=command, observed_open=observed_open), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary) / "recovery"
                api, clock = FakeApi(), FakeClock()
                runner = self.runner(root, api, clock)
                runner.config = LauncherConfig(("https://erp.example",), {".txt": command}, 0.2, 0.5, 5)
                runner.monitor_factory = lambda _document, _command: FakeMonitor(
                    lambda: DocumentState.OPEN if observed_open and clock.value < 1 else DocumentState.UNKNOWN
                )
                runner.finish_factory = lambda _document: FakeFinish(lambda: clock.value >= 40)

                def sleep(seconds: float) -> None:
                    clock.sleep(seconds)
                    if clock.value < 40:
                        self.assertNotIn("upload", api.events)

                runner.sleep = sleep
                result = runner.run(launch_uri())
                self.assertEqual(result.status, "completed_recovery")
                self.assertIn("heartbeat", api.events)

    def test_delayed_save_during_cancel_keeps_the_changed_document(self) -> None:
        class LateSaveApi(FakeApi):
            document: Path

            def cancel(self, token: str, lease_version: int) -> None:
                super().cancel(token, lease_version)
                self.document.write_bytes(b"late save during cancel response\n")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "recovery"
            api, clock = LateSaveApi(), FakeClock()
            runner = self.runner(root, api, clock)
            runner.monitor_factory = lambda _document, _command: FakeMonitor(lambda: DocumentState.CLOSED)

            def editor(document: Path, _command: tuple[str, ...] | None) -> EditorHandle:
                api.document = document
                return EditorHandle(None, False)

            runner.editor_opener = editor
            result = runner.run(launch_uri())
            self.assertEqual(result.status, "cancelled")
            self.assertEqual(api.document.read_bytes(), b"late save during cancel response\n")
            manifest = json.loads((api.document.parent / "recovery.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "cancelled_recovery")
            self.assertTrue(manifest["modified"])

    def test_reopened_document_during_completion_keeps_its_destination(self) -> None:
        reopened = False

        class ReopenApi(FakeApi):
            def complete(self, token: str, lease_version: int, current_sha256: str) -> None:
                nonlocal reopened
                super().complete(token, lease_version, current_sha256)
                reopened = True

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "recovery"
            api, clock = ReopenApi(), FakeClock()
            runner = self.runner(root, api, clock)
            runner.monitor_factory = lambda _document, _command: FakeMonitor(
                lambda: DocumentState.OPEN if reopened else DocumentState.CLOSED
            )
            result = runner.run(launch_uri())
            self.assertEqual(result.status, "completed_recovery")
            document = next(root.iterdir()) / "sample.txt"
            self.assertEqual(document.read_bytes(), b"edited\n")
            document.write_bytes(b"saved after reopen\n")
            self.assertEqual(document.read_bytes(), b"saved after reopen\n")

    def test_expiry_does_not_discard_a_late_save_during_cancel(self) -> None:
        class LateSaveApi(FakeApi):
            document: Path

            def cancel(self, token: str, lease_version: int) -> None:
                super().cancel(token, lease_version)
                self.document.write_bytes(b"late final save during expiry\n")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "recovery"
            api, clock = LateSaveApi(), FakeClock()
            runner = self.runner(root, api, clock)
            runner.config = LauncherConfig(("https://erp.example",), {}, 50_000, 60_000, 5)

            def editor(document: Path, _command: tuple[str, ...] | None) -> EditorHandle:
                api.document = document
                return EditorHandle(None, False)

            runner.editor_opener = editor
            result = runner.run(launch_uri())
            self.assertEqual(result.status, "expired_unchanged")
            self.assertEqual(api.document.read_bytes(), b"late final save during expiry\n")
            manifest = json.loads((api.document.parent / "recovery.json").read_text(encoding="utf-8"))
            self.assertTrue(manifest["modified"])

    def test_error_cleanup_rechecks_unchanged_copy_after_cancellation(self) -> None:
        class LateSaveApi(FakeApi):
            document: Path

            def heartbeat(self, token: str, lease_version: int) -> tuple[int, str]:
                raise ApiError("network", "failure")

            def cancel(self, token: str, lease_version: int) -> None:
                super().cancel(token, lease_version)
                self.document.write_bytes(b"late final save during error cleanup\n")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "recovery"
            api, clock = LateSaveApi(), FakeClock()
            runner = self.runner(root, api, clock)
            runner.config = LauncherConfig(("https://erp.example",), {}, 0.2, 60_000, 5)

            def editor(document: Path, _command: tuple[str, ...] | None) -> EditorHandle:
                api.document = document
                return EditorHandle(None, False)

            runner.editor_opener = editor
            with self.assertRaises(ApiError) as context:
                runner.run(launch_uri())
            self.assertTrue(context.exception.local_changes)
            self.assertIsNotNone(context.exception.recovery_directory)
            self.assertEqual(api.document.read_bytes(), b"late final save during error cleanup\n")

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
