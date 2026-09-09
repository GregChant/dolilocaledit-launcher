from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from dolilocaledit_launcher.editor import (
    EditorCandidate,
    EditorChoice,
    _choose_linux_editor,
    choose_editor,
    discover_linux_editors,
    discover_windows_editors,
    open_editor,
)


class EditorTest(unittest.TestCase):
    def test_local_mapping_uses_argument_array_without_shell(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            executable = root / "editor"
            executable.write_bytes(b"")
            document = root / "a name.txt"
            document.write_text("content", encoding="utf-8")
            with patch("dolilocaledit_launcher.editor.subprocess.Popen") as popen:
                handle = open_editor(document, (str(executable), "--document", "{file}"))
            self.assertTrue(handle.reliable_exit)
            arguments, keywords = popen.call_args
            self.assertEqual(arguments[0], [str(executable), "--document", str(document)])
            self.assertIs(keywords["shell"], False)

    def test_windows_discovers_office_and_libreoffice_for_docx(self) -> None:
        executables = {
            "WINWORD.EXE": Path(r"C:\Program Files\Microsoft Office\WINWORD.EXE"),
            "soffice.exe": Path(r"C:\Program Files\LibreOffice\soffice.exe"),
        }
        with patch(
            "dolilocaledit_launcher.editor._find_windows_executable",
            side_effect=lambda name: executables.get(name),
        ):
            candidates = discover_windows_editors(".docx")
        self.assertEqual([candidate.label for candidate in candidates], ["Microsoft Word", "LibreOffice Writer"])
        self.assertEqual(candidates[1].command[-2:], ("--writer", "{file}"))

    def test_windows_asks_for_supported_files_and_keeps_unknown_extensions_on_system_default(self) -> None:
        candidates = (
            EditorCandidate("Microsoft Word", (r"C:\Office\WINWORD.EXE", "{file}")),
            EditorCandidate("LibreOffice Writer", (r"C:\LibreOffice\soffice.exe", "--writer", "{file}")),
        )
        expected = EditorChoice(candidates[1].command, True)
        with (
            patch("dolilocaledit_launcher.editor.sys.platform", "win32"),
            patch("dolilocaledit_launcher.editor.discover_windows_editors", return_value=candidates),
            patch("dolilocaledit_launcher.editor._choose_windows_editor", return_value=expected) as chooser,
        ):
            self.assertEqual(choose_editor(Path("sample.docx")), expected)
        chooser.assert_called_once_with(candidates, ".docx")

        with (
            patch("dolilocaledit_launcher.editor.sys.platform", "win32"),
            patch("dolilocaledit_launcher.editor.discover_windows_editors", return_value=candidates[:1]),
            patch("dolilocaledit_launcher.editor._choose_windows_editor", return_value=expected) as chooser,
        ):
            self.assertEqual(choose_editor(Path("sample.docx")), expected)
        chooser.assert_called_once_with(candidates[:1], ".docx")

        with (
            patch("dolilocaledit_launcher.editor.sys.platform", "win32"),
            patch("dolilocaledit_launcher.editor._choose_windows_editor") as chooser,
        ):
            self.assertIsNone(choose_editor(Path("sample.unknown")))
        chooser.assert_not_called()

    def test_linux_discovers_only_existing_executable_editors(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            libreoffice = root / "libreoffice"
            onlyoffice = root / "desktopeditors"
            libreoffice.write_bytes(b"")
            onlyoffice.write_bytes(b"")
            libreoffice.chmod(0o700)
            onlyoffice.chmod(0o700)
            executables = {
                "libreoffice": str(libreoffice),
                "desktopeditors": str(onlyoffice),
            }
            with patch(
                "dolilocaledit_launcher.editor.shutil.which",
                side_effect=lambda name: executables.get(name),
            ):
                candidates = discover_linux_editors(".docx")
        self.assertEqual(
            [candidate.label for candidate in candidates],
            ["LibreOffice Writer", "ONLYOFFICE Desktop Editors"],
        )
        self.assertEqual(candidates[0].command[-2:], ("--writer", "{file}"))

    def test_linux_zenity_choice_is_local_and_optional(self) -> None:
        candidate = EditorCandidate("LibreOffice Writer", ("/usr/bin/libreoffice", "--writer", "{file}"))
        results = [
            subprocess.CompletedProcess([], 0, "0\n", ""),
            subprocess.CompletedProcess([], 1, "", ""),
        ]
        with (
            patch(
                "dolilocaledit_launcher.editor._linux_dialog_frontend",
                return_value=("zenity", Path("/usr/bin/zenity")),
            ),
            patch("dolilocaledit_launcher.editor._run_linux_dialog", side_effect=results) as run,
        ):
            choice = _choose_linux_editor((candidate,), ".docx")
        self.assertEqual(choice, EditorChoice(candidate.command, False))
        self.assertIn("--list", run.call_args_list[0].args[0])
        self.assertIn("--question", run.call_args_list[1].args[0])

    def test_linux_headless_session_uses_the_system_association(self) -> None:
        with (
            patch("dolilocaledit_launcher.editor.sys.platform", "linux"),
            patch.dict("dolilocaledit_launcher.editor.os.environ", {}, clear=True),
            patch("dolilocaledit_launcher.editor._choose_linux_editor") as chooser,
        ):
            self.assertIsNone(choose_editor(Path("sample.pdf")))
        chooser.assert_not_called()


if __name__ == "__main__":
    unittest.main()
