import os
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest.mock import Mock, patch

from dolilocaledit_launcher.finish import LocalFinishControl, finish_message


class FinishTest(unittest.TestCase):
    def bare_control(self) -> LocalFinishControl:
        control = object.__new__(LocalFinishControl)
        control.message = "Document local enregistré et fermé"
        control._requested = threading.Event()
        control._stopping = threading.Event()
        control._process = None
        control._window = None
        return control

    def test_finish_message_contains_only_sanitized_local_filename(self) -> None:
        message = finish_message(Path("/private/recovery/name\nwith\tcontrols.txt"))
        self.assertIn("namewithcontrols.txt", message)
        self.assertNotIn("/private", message)
        self.assertIn("Enregistrez le document, puis fermez-le", message)
        self.assertIn("Terminer l’édition", message)

    def test_linux_explicit_finish_is_the_only_successful_dialog_result(self) -> None:
        control = self.bare_control()
        process = Mock()
        process.wait.return_value = 0
        with patch.dict(os.environ, {"DISPLAY": ":1"}), patch(
            "dolilocaledit_launcher.finish._which_absolute", return_value=Path("/usr/bin/zenity")
        ), patch("dolilocaledit_launcher.finish.subprocess.Popen", return_value=process) as spawn:
            control._run_linux()
        self.assertTrue(control.requested())
        arguments = spawn.call_args.args[0]
        self.assertIn("--ok-label=Terminer l’édition", arguments)
        self.assertIn("--cancel-label=Continuer l’édition", arguments)
        self.assertFalse(spawn.call_args.kwargs["shell"])

    def test_linux_continue_or_dialog_failure_does_not_request_completion(self) -> None:
        for result in (1, -15, 255):
            with self.subTest(result=result):
                control = self.bare_control()
                process = Mock()
                process.wait.return_value = result
                with patch.dict(os.environ, {"DISPLAY": ":1"}), patch(
                    "dolilocaledit_launcher.finish._which_absolute", return_value=Path("/usr/bin/zenity")
                ), patch("dolilocaledit_launcher.finish.subprocess.Popen", return_value=process), patch.object(
                    control._stopping, "wait", return_value=True
                ):
                    control._run_linux()
                self.assertFalse(control.requested())

    def test_no_desktop_never_means_document_completed(self) -> None:
        control = self.bare_control()
        with patch.dict(os.environ, {}, clear=True), patch(
            "dolilocaledit_launcher.finish.subprocess.Popen"
        ) as spawn:
            control._run_linux()
        spawn.assert_not_called()
        self.assertFalse(control.requested())

    def test_close_terminates_the_dialog_without_requesting_finish(self) -> None:
        control = self.bare_control()
        process = Mock()
        process.poll.return_value = None
        control._process = process
        control.close()
        self.assertFalse(control.requested())
        self.assertTrue(control._stopping.is_set())
        process.terminate.assert_called_once()
        process.wait.assert_called_once_with(timeout=1)

    @unittest.skipUnless(sys.platform == "win32", "Requires the native Windows desktop")
    def test_native_windows_checkbox_and_finish_are_required(self) -> None:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        send = user32.SendMessageW
        send.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        send.restype = ctypes.c_ssize_t
        user32.GetDlgItem.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.GetDlgItem.restype = wintypes.HWND
        control = LocalFinishControl("Synthetic native finish control test. Aucun document réel.")
        try:
            deadline = time.monotonic() + 5
            while control._window is None and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertIsNotNone(control._window)
            window = control._window
            send(window, 0x0111, 1, 0)
            self.assertFalse(control.requested())
            send(window, 0x0010, 0, 0)
            self.assertFalse(control.requested())
            checkbox = user32.GetDlgItem(window, 2)
            self.assertTrue(checkbox)
            send(checkbox, 0x00F1, 1, 0)  # BM_SETCHECK
            send(window, 0x0111, 1, 0)
            self.assertTrue(control.requested())
        finally:
            control.close()
            control._thread.join(timeout=2)
        self.assertFalse(control._thread.is_alive())


if __name__ == "__main__":
    unittest.main()
