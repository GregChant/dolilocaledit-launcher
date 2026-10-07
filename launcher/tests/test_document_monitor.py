import io
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from dolilocaledit_launcher.document_monitor import (
    DocumentState,
    LibreOfficeDocumentMonitor,
    UnknownDocumentMonitor,
    WindowsOfficeDocumentMonitor,
    _start_office_helper,
    create_document_monitor,
)


class Clock:
    value = 0.0

    def monotonic(self) -> float:
        return self.value


class DocumentMonitorTest(unittest.TestCase):
    def owner(self, monitor: LibreOfficeDocumentMonitor) -> None:
        monitor.owner_file.write_bytes(b"Synthetic Owner,synthetic,localhost,01.01.2026 12:00,file:///synthetic;")

    def test_libreoffice_never_closes_without_observing_an_owner(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            document = Path(temporary) / "sample.odt"
            document.write_bytes(b"synthetic")
            clock = Clock()
            monitor = LibreOfficeDocumentMonitor(document, clock.monotonic)
            self.assertEqual(monitor.poll(), DocumentState.UNKNOWN)
            clock.value = 1000
            self.assertEqual(monitor.poll(), DocumentState.UNKNOWN)

    def test_libreoffice_closes_only_after_owner_and_document_are_stably_released(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            document = Path(temporary) / "sample.odt"
            document.write_bytes(b"synthetic")
            clock = Clock()
            monitor = LibreOfficeDocumentMonitor(document, clock.monotonic)
            self.owner(monitor)
            self.assertEqual(monitor.poll(), DocumentState.OPEN)
            monitor.owner_file.unlink()
            self.assertEqual(monitor.poll(), DocumentState.UNKNOWN)
            clock.value = 2.9
            self.assertEqual(monitor.poll(), DocumentState.UNKNOWN)
            clock.value = 3.0
            self.assertEqual(monitor.poll(), DocumentState.CLOSED)
            self.owner(monitor)
            self.assertEqual(monitor.poll(), DocumentState.OPEN)

    def test_libreoffice_save_replacement_restarts_grace_and_reopening_keeps_open(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            document = Path(temporary) / "sample.odt"
            document.write_bytes(b"synthetic")
            clock = Clock()
            monitor = LibreOfficeDocumentMonitor(document, clock.monotonic)
            self.owner(monitor)
            self.assertEqual(monitor.poll(), DocumentState.OPEN)
            monitor.owner_file.unlink()
            document.unlink()
            self.assertEqual(monitor.poll(), DocumentState.UNKNOWN)
            clock.value = 5
            self.assertEqual(monitor.poll(), DocumentState.UNKNOWN)
            document.write_bytes(b"replacement synthetic")
            self.assertEqual(monitor.poll(), DocumentState.UNKNOWN)
            clock.value = 7
            document.write_bytes(b"last replacement synthetic")
            self.assertEqual(monitor.poll(), DocumentState.UNKNOWN)
            clock.value = 9
            self.owner(monitor)
            self.assertEqual(monitor.poll(), DocumentState.OPEN)
            clock.value = 100
            self.assertEqual(monitor.poll(), DocumentState.OPEN)

    def test_preexisting_owner_does_not_establish_an_open_document(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            document = Path(temporary) / "sample.odt"
            document.write_bytes(b"synthetic")
            owner = document.with_name(".~lock." + document.name + "#")
            owner.write_bytes(b"Synthetic,synthetic,localhost,date,profile;")
            clock = Clock()
            monitor = LibreOfficeDocumentMonitor(document, clock.monotonic)
            self.assertEqual(monitor.poll(), DocumentState.UNKNOWN)
            owner.unlink()
            clock.value = 100
            self.assertEqual(monitor.poll(), DocumentState.UNKNOWN)

    def test_malformed_or_unreadable_owner_never_closes_a_previously_open_document(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            document = Path(temporary) / "sample.odt"
            document.write_bytes(b"synthetic")
            clock = Clock()
            monitor = LibreOfficeDocumentMonitor(document, clock.monotonic)
            monitor.owner_file.write_bytes(b"partial owner")
            self.assertEqual(monitor.poll(), DocumentState.UNKNOWN)
            self.owner(monitor)
            self.assertEqual(monitor.poll(), DocumentState.OPEN)
            monitor.owner_file.write_bytes(b"partial replacement")
            clock.value = 100
            self.assertEqual(monitor.poll(), DocumentState.UNKNOWN)
            with patch.object(Path, "lstat", side_effect=PermissionError("synthetic")):
                self.assertEqual(monitor.poll(), DocumentState.UNKNOWN)

    def test_symlink_owner_never_counts_as_a_close(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            document = root / "sample.odt"
            document.write_bytes(b"synthetic")
            monitor = LibreOfficeDocumentMonitor(document)
            self.owner(monitor)
            self.assertEqual(monitor.poll(), DocumentState.OPEN)
            monitor.owner_file.unlink()
            try:
                monitor.owner_file.symlink_to(document)
            except OSError:
                self.skipTest("Symbolic links are unavailable for this user.")
            self.assertEqual(monitor.poll(), DocumentState.UNKNOWN)

    def test_native_state_requires_open_and_never_uses_helper_exit_as_close(self) -> None:
        monitor = WindowsOfficeDocumentMonitor(Path("synthetic.docx"), (), lambda *_: None)
        monitor._accept_state("CLOSED")
        self.assertEqual(monitor.poll(), DocumentState.UNKNOWN)
        monitor._accept_state("OPEN")
        self.assertEqual(monitor.poll(), DocumentState.OPEN)
        monitor._accept_state("UNKNOWN")
        self.assertEqual(monitor.poll(), DocumentState.UNKNOWN)
        monitor._accept_state("CLOSED")
        self.assertEqual(monitor.poll(), DocumentState.CLOSED)
        monitor._accept_state("OPEN")
        self.assertEqual(monitor.poll(), DocumentState.OPEN)
        monitor.close()
        monitor.close()

    def test_native_eof_or_invalid_state_is_unknown_even_after_a_close_observation(self) -> None:
        for words, expected in (
            ("OPEN\n", DocumentState.UNKNOWN),
            ("OPEN\nunexpected diagnostic\n", DocumentState.UNKNOWN),
            ("CLOSED\n", DocumentState.UNKNOWN),
            ("OPEN\nCLOSED\n", DocumentState.UNKNOWN),
        ):
            with self.subTest(words=words):
                helper = Mock()
                helper.stdout = io.StringIO(words)
                helper.poll.return_value = 0
                monitor = WindowsOfficeDocumentMonitor(Path("synthetic.docx"), (), lambda *_: helper)
                assert monitor._reader is not None
                monitor._reader.join(timeout=1)
                self.assertEqual(monitor.poll(), expected)
                monitor.close()

    def test_stalled_helper_cannot_keep_an_open_or_closed_state_fresh(self) -> None:
        clock = Clock()
        monitor = WindowsOfficeDocumentMonitor(Path("synthetic.docx"), (), lambda *_: None, clock.monotonic)
        monitor._accept_state("OPEN")
        self.assertEqual(monitor.poll(), DocumentState.OPEN)
        clock.value = 1.1
        self.assertEqual(monitor.poll(), DocumentState.UNKNOWN)
        monitor._accept_state("CLOSED")
        self.assertEqual(monitor.poll(), DocumentState.CLOSED)
        clock.value = 2.2
        self.assertEqual(monitor.poll(), DocumentState.UNKNOWN)
        monitor._accept_state("OPEN")
        self.assertEqual(monitor.poll(), DocumentState.OPEN)

    def test_close_stops_only_the_owned_helper_and_is_idempotent(self) -> None:
        helper = Mock()
        helper.stdout = None
        helper.poll.return_value = None
        monitor = WindowsOfficeDocumentMonitor(Path("synthetic.docx"), (), lambda *_: helper)
        monitor.close()
        monitor.close()
        helper.terminate.assert_called_once_with()
        helper.wait.assert_called_once_with(timeout=2)

    def test_unknown_editor_and_missing_native_support_stay_unknown(self) -> None:
        with patch("dolilocaledit_launcher.document_monitor.sys.platform", "linux"):
            self.assertIsInstance(create_document_monitor(Path("synthetic.docx")), UnknownDocumentMonitor)
        with patch("dolilocaledit_launcher.document_monitor.sys.platform", "win32"):
            unknown = create_document_monitor(Path("synthetic.docx"), ("C:/synthetic/other.exe", "{file}"))
            self.assertIsInstance(unknown, UnknownDocumentMonitor)
            with patch("dolilocaledit_launcher.document_monitor._start_office_helper", return_value=None):
                office = create_document_monitor(Path("synthetic.docx"), ("C:/Office/WINWORD.EXE", "{file}"))
                self.assertIsInstance(office, WindowsOfficeDocumentMonitor)
                self.assertEqual(office.poll(), DocumentState.UNKNOWN)

    def test_factory_qualifies_only_explicit_libreoffice_commands(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            document = Path(temporary) / "synthetic.odt"
            monitor = create_document_monitor(document, ("/usr/bin/libreoffice", "--writer", "{file}"))
            self.assertIsInstance(monitor, LibreOfficeDocumentMonitor)

    def test_helper_uses_no_shell_and_sends_local_path_only_through_standard_input(self) -> None:
        process = Mock()
        process.stdin = io.StringIO()
        captured: list[str] = []
        process.stdin.close = lambda: captured.append(process.stdin.getvalue())
        document = Path("C:/private/synthetic document.docx")
        with (
            patch.object(Path, "is_file", return_value=True),
            patch("dolilocaledit_launcher.document_monitor.subprocess.Popen", return_value=process) as popen,
        ):
            self.assertIs(_start_office_helper(document, ("Word.Application",)), process)
        args, options = popen.call_args
        self.assertFalse(options["shell"])
        self.assertNotIn(str(document), " ".join(args[0]))
        self.assertIn("synthetic document.docx", captured[0])
        self.assertNotIn("access_token", captured[0])
        self.assertNotIn("ticket", captured[0])

    def test_unavailable_helper_does_not_fail_the_editing_session(self) -> None:
        with patch.object(Path, "is_file", return_value=False):
            self.assertIsNone(_start_office_helper(Path("synthetic.docx"), ("Word.Application",)))
        with (
            patch.object(Path, "is_file", return_value=True),
            patch("dolilocaledit_launcher.document_monitor.subprocess.Popen", side_effect=OSError("synthetic")),
        ):
            self.assertIsNone(_start_office_helper(Path("synthetic.docx"), ("Word.Application",)))


if __name__ == "__main__":
    unittest.main()
