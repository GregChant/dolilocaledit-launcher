import importlib.util
import importlib.machinery
import json
import os
from pathlib import Path
import py_compile
import shutil
import tempfile
import unittest


REPOSITORY = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location(
    "launcher_release_inputs_tests", REPOSITORY / "scripts/launcher-release-inputs.py"
)
assert _SPEC is not None and _SPEC.loader is not None
INPUTS = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(INPUTS)
LAUNCHER_VERSION = INPUTS.launcher_version(REPOSITORY)


class LauncherReuseTest(unittest.TestCase):
    def _fixture(self, directory: Path) -> tuple[Path, Path]:
        source = directory / "source"
        for relative in INPUTS.FIXED_INPUTS:
            target = source / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(REPOSITORY / relative, target)
        shutil.copytree(
            REPOSITORY / "launcher/src",
            source / "launcher/src",
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
        )
        catalog = directory / "catalog"
        catalog.mkdir()
        manifest = {
            "schema": 2,
            "version": LAUNCHER_VERSION,
            "artifacts": [{
                "platform": "windows",
                "architecture": "x86_64",
                "filename": "DoliLocalEdit-Setup.exe",
                "download_name": "DoliLocalEdit-Setup.exe",
                "byte_size": 32,
                "sha256": "a" * 64,
                "url": f"https://github.com/GregChant/dolilocaledit-launcher/releases/download/v{LAUNCHER_VERSION}/DoliLocalEdit-Setup.exe",
            }],
        }
        (catalog / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        INPUTS.write_inputs(source, catalog)
        return source, catalog

    def test_reuse_rejects_changed_added_and_removed_build_inputs(self) -> None:
        for change in ("runtime", "toolchain", "added_python", "added_native", "removed_python"):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as temporary:
                source, catalog = self._fixture(Path(temporary))
                INPUTS.verify_inputs(source, catalog)
                if change == "runtime":
                    path = source / "launcher/src/dolilocaledit_launcher/api.py"
                    path.write_text(path.read_text() + "\n# Modified behavior\n")
                elif change == "toolchain":
                    path = source / "launcher/requirements-build.txt"
                    path.write_text(path.read_text() + "\n# Updated build dependency\n")
                elif change == "added_python":
                    (source / "launcher/src/added.py").write_text("new_behavior = True\n")
                elif change == "added_native":
                    (source / "launcher/src/added.pyd").write_bytes(b"native import fixture")
                else:
                    (source / "launcher/src/dolilocaledit_launcher/api.py").unlink()
                with self.assertRaisesRegex(INPUTS.ReleaseInputError, "build inputs changed"):
                    INPUTS.verify_inputs(source, catalog)

    def test_reuse_rejects_substituted_artifact_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source, catalog = self._fixture(Path(temporary))
            path = catalog / "manifest.json"
            manifest = json.loads(path.read_text())
            manifest["artifacts"][0]["sha256"] = "b" * 64
            path.write_text(json.dumps(manifest))
            with self.assertRaisesRegex(INPUTS.ReleaseInputError, "artifacts do not match"):
                INPUTS.verify_inputs(source, catalog)

    def test_reuse_rejects_added_sourceless_runtime_bytecode(self) -> None:
        for suffix in (".pyc", ".pyo"):
            with self.subTest(suffix=suffix), tempfile.TemporaryDirectory() as temporary:
                source, catalog = self._fixture(Path(temporary))
                external_source = Path(temporary) / "sourceless_fixture.py"
                external_source.write_text("FIXTURE_VALUE = True\n")
                bytecode = source / "launcher/src" / ("json" + suffix)
                py_compile.compile(str(external_source), cfile=str(bytecode), doraise=True)
                if suffix == ".pyc":
                    spec = importlib.machinery.PathFinder.find_spec("json", [str(bytecode.parent)])
                    self.assertIsNotNone(spec)
                    self.assertIsInstance(spec.loader, importlib.machinery.SourcelessFileLoader)
                    self.assertEqual(Path(spec.origin), bytecode)
                with self.assertRaisesRegex(INPUTS.ReleaseInputError, "build inputs changed.*json"):
                    INPUTS.verify_inputs(source, catalog)

    def test_runtime_and_metadata_versions_must_match(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source, catalog = self._fixture(Path(temporary))
            project = source / "launcher/pyproject.toml"
            project.write_text(project.read_text().replace(f'version = "{LAUNCHER_VERSION}"', 'version = "9.9.9"'))
            with self.assertRaisesRegex(INPUTS.ReleaseInputError, "runtime version does not match"):
                INPUTS.capture_inputs(source, catalog)

    def test_reuse_ignores_generated_caches_and_preserves_artifact_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source, catalog = self._fixture(Path(temporary))
            cache = source / "launcher/src/dolilocaledit_launcher/__pycache__"
            cache.mkdir()
            (cache / "api.cpython-314.pyc").write_bytes(b"temporary Python cache")
            path = catalog / "manifest.json"
            manifest = json.loads(path.read_text())
            manifest["artifacts"][0]["download_name"] = "AnotherDisplayName.exe"
            path.write_text(json.dumps(manifest))
            INPUTS.verify_inputs(source, catalog)

    def test_reuse_refuses_runtime_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source, catalog = self._fixture(Path(temporary))
            runtime = source / "launcher/src/dolilocaledit_launcher"
            try:
                (runtime / "alias.py").symlink_to(runtime / "api.py")
            except OSError:
                if os.name == "nt":
                    self.skipTest("Creating symlinks requires Windows Developer Mode or privilege")
                raise
            with self.assertRaisesRegex(INPUTS.ReleaseInputError, "symbolic link"):
                INPUTS.verify_inputs(source, catalog)


if __name__ == "__main__":
    unittest.main()
