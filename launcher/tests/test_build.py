from importlib import util
from pathlib import Path
from unittest import mock
import unittest


SPEC = util.spec_from_file_location(
    "dolilocaledit_build",
    Path(__file__).resolve().parents[1] / "build.py",
)
assert SPEC is not None and SPEC.loader is not None
BUILD = util.module_from_spec(SPEC)
SPEC.loader.exec_module(BUILD)


class _Distribution:
    def __init__(self, name: str, version: str) -> None:
        self.metadata = {"Name": name}
        self.version = version


class SecureBuildEnvironmentTest(unittest.TestCase):
    def _distributions(self, extra: tuple[str, str] | None = None) -> list[_Distribution]:
        versions = dict(BUILD.EXPECTED_DISTRIBUTIONS)
        versions["pip"] = "26.2.1"
        if extra is not None:
            versions[extra[0]] = extra[1]
        return [_Distribution(name, version) for name, version in versions.items()]

    def test_clean_audited_linux_environment_is_accepted(self) -> None:
        with (
            mock.patch.object(BUILD.sys, "version_info", (3, 14, 7)),
            mock.patch.object(BUILD.sys, "prefix", "/venv"),
            mock.patch.object(BUILD.sys, "base_prefix", "/python"),
            mock.patch.object(BUILD.site, "ENABLE_USER_SITE", False),
            mock.patch.object(BUILD.platform, "system", return_value="Linux"),
            mock.patch.object(BUILD.metadata, "distributions", return_value=self._distributions()),
        ):
            self.assertIsNone(BUILD.verify_build_environment())

    def test_unexpected_package_is_rejected(self) -> None:
        with (
            mock.patch.object(BUILD.sys, "version_info", (3, 14, 7)),
            mock.patch.object(BUILD.sys, "prefix", "/venv"),
            mock.patch.object(BUILD.sys, "base_prefix", "/python"),
            mock.patch.object(BUILD.site, "ENABLE_USER_SITE", False),
            mock.patch.object(BUILD.platform, "system", return_value="Linux"),
            mock.patch.object(
                BUILD.metadata,
                "distributions",
                return_value=self._distributions(("unreviewed-hook", "1.0")),
            ),
        ):
            self.assertIn("Unexpected packages", BUILD.verify_build_environment() or "")

    def test_wrong_runtime_is_rejected_before_package_inspection(self) -> None:
        with mock.patch.object(BUILD.sys, "version_info", (3, 14, 6)):
            self.assertIn("Python 3.14.7", BUILD.verify_build_environment() or "")

    def test_windows_resource_contains_version_and_publisher_details(self) -> None:
        repository = Path(__file__).resolve().parents[2]
        version, numeric = BUILD.project_version(repository)
        resource = BUILD.windows_version_resource(version, numeric)
        self.assertEqual(version, "1.1.0")
        self.assertEqual(numeric, (1, 1, 0, 0))
        self.assertIn("StringStruct('CompanyName', 'Experts Conseils Chanton')", resource)
        self.assertIn("StringStruct('ProductName', 'Doli Local Edit')", resource)
        self.assertIn("StringStruct('FileVersion', '1.1.0')", resource)
        self.assertIn("StringStruct('ProductVersion', '1.1.0')", resource)
        self.assertIn("Copyright (C) 2026 Experts Conseils Chanton", resource)


if __name__ == "__main__":
    unittest.main()
