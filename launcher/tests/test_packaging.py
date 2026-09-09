import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile


class PackagingTest(unittest.TestCase):
    def test_catalog_archives_launchers_and_embeds_verified_metadata(self) -> None:
        repository = Path(__file__).resolve().parents[2]
        script = repository / "scripts" / "package-launchers.py"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            linux = root / "launcher-linux"
            windows = root / "launcher.exe"
            installer = root / "install.sh"
            linux.write_bytes(b"linux executable")
            windows.write_bytes(b"windows executable")
            installer.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            module_zip = root / "module.zip"
            with zipfile.ZipFile(module_zip, "w") as archive:
                archive.writestr("dolilocaledit/README.md", "fixture")
            catalog = root / "catalog"
            result = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "--version",
                    "0.1.0-test",
                    "--catalog-directory",
                    str(catalog),
                    "--module-zip",
                    str(module_zip),
                    "--windows-executable",
                    str(windows),
                    "--linux-executable",
                    str(linux),
                    "--linux-installer",
                    str(installer),
                ],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            manifest = json.loads((catalog / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["schema"], 1)
            self.assertEqual({entry["platform"] for entry in manifest["artifacts"]}, {"windows", "linux"})
            for entry in manifest["artifacts"]:
                artifact = catalog / "files" / entry["filename"]
                self.assertEqual(entry["byte_size"], artifact.stat().st_size)
                self.assertEqual(entry["sha256"], hashlib.sha256(artifact.read_bytes()).hexdigest())
            linux_entry = next(entry for entry in manifest["artifacts"] if entry["platform"] == "linux")
            with tarfile.open(catalog / "files" / linux_entry["filename"], "r:gz") as archive:
                self.assertEqual(
                    archive.getnames(),
                    ["DoliLocalEdit/dolilocaledit-launcher", "DoliLocalEdit/install.sh"],
                )
                self.assertEqual(archive.getmember("DoliLocalEdit/install.sh").mode, 0o755)
            with zipfile.ZipFile(module_zip) as archive:
                names = set(archive.namelist())
                self.assertIn("dolilocaledit/resources/launchers/manifest.json", names)
                self.assertIn(
                    "dolilocaledit/resources/launchers/files/" + linux_entry["filename"],
                    names,
                )

    def test_external_catalog_keeps_release_binaries_out_of_module_zip(self) -> None:
        repository = Path(__file__).resolve().parents[2]
        script = repository / "scripts" / "package-launchers.py"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            windows = root / "launcher.exe"
            linux = root / "launcher-linux"
            installer = root / "install.sh"
            windows.write_bytes(b"signed windows fixture")
            linux.write_bytes(b"linux fixture")
            installer.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            module_zip = root / "module.zip"
            with zipfile.ZipFile(module_zip, "w") as archive:
                archive.writestr("dolilocaledit/README.md", "fixture")
            catalog = root / "catalog"
            result = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "--version",
                    "0.1.9",
                    "--catalog-directory",
                    str(catalog),
                    "--module-zip",
                    str(module_zip),
                    "--windows-executable",
                    str(windows),
                    "--linux-executable",
                    str(linux),
                    "--linux-installer",
                    str(installer),
                    "--external-download-base-url",
                    "https://github.com/GregChant/dolilocaledit-launcher/releases/download/v0.1.9",
                ],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            manifest = json.loads((catalog / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["schema"], 2)
            self.assertEqual(len(manifest["artifacts"]), 2)
            for entry in manifest["artifacts"]:
                self.assertEqual(
                    entry["url"],
                    "https://github.com/GregChant/dolilocaledit-launcher/releases/download/v0.1.9/"
                    + entry["filename"],
                )
                self.assertTrue((catalog / "files" / entry["filename"]).is_file())
            with zipfile.ZipFile(module_zip) as archive:
                names = set(archive.namelist())
            self.assertIn("dolilocaledit/resources/launchers/manifest.json", names)
            self.assertFalse(any(name.startswith("dolilocaledit/resources/launchers/files/") for name in names))


if __name__ == "__main__":
    unittest.main()
