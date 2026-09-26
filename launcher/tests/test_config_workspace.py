import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from dolilocaledit_launcher.config import forget_editor_choice, load_config, remember_editor_choice, trust_origin
from dolilocaledit_launcher.errors import ConfigurationError
from dolilocaledit_launcher.workspace import RecoveryWorkspace, file_sha256, safe_local_filename


class ConfigWorkspaceTest(unittest.TestCase):
    def test_trust_file_is_private_and_round_trips(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "settings" / "config.json"
            trust_origin("https://ERP.EXAMPLE:443", path)
            self.assertEqual(load_config(path).trusted_origins, ("https://erp.example",))
            if os.name == "posix":
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_rejects_relative_editor_and_open_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.json"
            path.write_text(json.dumps({"editors": {".txt": ["editor", "{file}"]}}), encoding="utf-8")
            path.chmod(0o600)
            with self.assertRaises(ConfigurationError) as context:
                load_config(path)
            self.assertEqual(context.exception.code, "editor_not_absolute")
            path.write_text("{}", encoding="utf-8")
            path.chmod(0o644)
            if os.name == "posix":
                with self.assertRaises(ConfigurationError) as context:
                    load_config(path)
                self.assertEqual(context.exception.code, "config_permissions")

            absolute = str((Path(temporary) / "editor.exe").resolve())
            path.write_text(json.dumps({
                "editors": {".txt": [absolute, "{file}"]},
                "system_default_editor_extensions": [".txt"],
            }), encoding="utf-8")
            path.chmod(0o600)
            with self.assertRaises(ConfigurationError) as context:
                load_config(path)
            self.assertEqual(context.exception.code, "config_invalid")

    def test_remembered_automatic_editor_is_local_and_untracked(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "config.json"
            executable = root / "office.exe"
            executable.write_bytes(b"")
            trust_origin("https://erp.example", path)
            remember_editor_choice(".docx", (str(executable.resolve()), "{file}"), path)
            config = load_config(path)
            self.assertEqual(config.editor_for(".DOCX"), (str(executable.resolve()), "{file}"))
            self.assertFalse(config.editor_exit_is_reliable(".docx"))
            self.assertEqual(config.trusted_origins, ("https://erp.example",))

            forget_editor_choice(".DOCX", path)
            reset = load_config(path)
            self.assertIsNone(reset.editor_for(".docx"))
            self.assertTrue(reset.editor_exit_is_reliable(".docx"))
            self.assertEqual(reset.trusted_origins, ("https://erp.example",))

            remember_editor_choice(".docx", None, path)
            system_default = load_config(path)
            self.assertFalse(system_default.should_ask_for_editor(".docx"))
            self.assertFalse(system_default.editor_exit_is_reliable(".docx"))
            self.assertIsNone(system_default.editor_for(".docx"))

            forget_editor_choice(".docx", path)
            self.assertTrue(load_config(path).should_ask_for_editor(".docx"))

    def test_workspace_keeps_private_stable_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "recovery"
            workspace = RecoveryWorkspace.create(root, "échéancier?.xlsx", "https://erp.example", 1, "uuid")
            downloaded = workspace.download_path()
            downloaded.write_bytes(b"original")
            downloaded.chmod(0o600)
            original_hash = file_sha256(downloaded)
            workspace.finish_download(downloaded, original_hash)
            self.assertEqual(workspace.document.name, "document.xlsx")
            workspace.document.write_bytes(b"edited")
            snapshot = workspace.stable_snapshot()
            self.assertIsNotNone(snapshot)
            assert snapshot is not None
            self.assertEqual(snapshot[0].read_bytes(), b"edited")
            self.assertEqual(snapshot[1], file_sha256(workspace.document))
            if os.name == "posix":
                self.assertEqual(workspace.directory.stat().st_mode & 0o777, 0o700)
            workspace.discard()
            self.assertFalse(workspace.directory.exists())

    def test_safe_filename_never_keeps_path_components(self) -> None:
        self.assertEqual(safe_local_filename("../../report.docx"), "document.docx")
        self.assertEqual(safe_local_filename("report.txt"), "report.txt")

    def test_safe_filename_avoids_windows_devices_even_with_extensions(self) -> None:
        for filename in ("CON.txt", "nul.docx", "AUX.xlsx", "COM1.pdf", "LPT9.txt", "COM¹.txt", "CON .txt"):
            with self.subTest(filename=filename):
                self.assertTrue(safe_local_filename(filename).startswith("document."))
        self.assertEqual(safe_local_filename("COM10.txt"), "COM10.txt")
        self.assertEqual(safe_local_filename("CONTRACT.docx"), "CONTRACT.docx")

    def test_download_cannot_overwrite_its_recovery_manifest(self) -> None:
        for filename in ("recovery.json", "Recovery.JSON", "recovery.tmp"):
            with self.subTest(filename=filename), tempfile.TemporaryDirectory() as temporary:
                workspace = RecoveryWorkspace.create(Path(temporary), filename, "https://erp.example", 1, "uuid")
                download = workspace.download_path()
                download.write_bytes(b"downloaded document")
                workspace.finish_download(download, file_sha256(download))
                self.assertEqual(workspace.document.read_bytes(), b"downloaded document")
                manifest = json.loads(workspace.manifest.read_text(encoding="utf-8"))
                self.assertEqual(manifest["status"], "editing")
                self.assertEqual(manifest["filename"], workspace.document.name)

    def test_snapshot_retries_when_the_editor_removes_the_source_during_copy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = RecoveryWorkspace.create(Path(temporary), "sample.txt", "https://erp.example", 1, "uuid")
            workspace.document.write_bytes(b"save in progress")
            with patch("dolilocaledit_launcher.workspace.os.fsync", side_effect=lambda _fd: workspace.document.unlink()):
                self.assertIsNone(workspace.stable_snapshot())
            self.assertFalse((workspace.directory / ".upload").exists())
            self.assertIsNone(workspace.stable_snapshot())
            workspace.document.write_bytes(b"complete replacement")
            snapshot, digest = workspace.stable_snapshot()
            self.assertEqual(snapshot.read_bytes(), b"complete replacement")
            self.assertEqual(digest, file_sha256(workspace.document))

    def test_abrupt_launcher_exit_leaves_a_readable_secret_free_recovery(self) -> None:
        code = """
import hashlib
import os
from pathlib import Path
import sys
from dolilocaledit_launcher.workspace import RecoveryWorkspace
root = Path(sys.argv[1])
workspace = RecoveryWorkspace.create(root, 'crash.txt', 'https://erp.example', 1, 'session-uuid')
download = workspace.download_path()
download.write_bytes(b'base')
workspace.finish_download(download, hashlib.sha256(b'base').hexdigest())
workspace.document.write_bytes(b'unsaved-to-server')
os._exit(23)
"""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "recovery"
            environment = os.environ.copy()
            source_root = str(Path(__file__).resolve().parents[1] / "src")
            environment["PYTHONPATH"] = source_root + (
                os.pathsep + environment["PYTHONPATH"] if environment.get("PYTHONPATH") else ""
            )
            result = subprocess.run(
                [sys.executable, "-c", code, str(root)],
                check=False,
                env=environment,
            )
            self.assertEqual(result.returncode, 23)
            recovery = next(root.iterdir())
            self.assertEqual((recovery / "crash.txt").read_bytes(), b"unsaved-to-server")
            manifest = (recovery / "recovery.json").read_text(encoding="utf-8")
            self.assertIn('"status": "editing"', manifest)
            self.assertNotIn("ticket", manifest.lower())
            self.assertNotIn("access_token", manifest.lower())


if __name__ == "__main__":
    unittest.main()
