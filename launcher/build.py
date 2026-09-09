"""Reproducible local PyInstaller entry point; code signing is a separate release step."""

from __future__ import annotations

import argparse
from importlib import metadata
import os
from pathlib import Path
import platform
import re
import shutil
import site
import subprocess
import sys
import tempfile


EXPECTED_PYINSTALLER = "6.22.2"
EXPECTED_PYTHON = (3, 14, 7)
EXPECTED_DISTRIBUTIONS = {
    "altgraph": "0.17.5",
    "packaging": "26.3",
    "pyinstaller": EXPECTED_PYINSTALLER,
    "pyinstaller-hooks-contrib": "2026.7",
    "setuptools": "84.0.0",
}
WINDOWS_DISTRIBUTIONS = {
    "pefile": "2024.8.26",
    "pywin32-ctypes": "0.2.3",
}
WINDOWS_COMPANY_NAME = "Experts Conseils Chanton"
WINDOWS_PRODUCT_NAME = "Doli Local Edit"
WINDOWS_FILE_DESCRIPTION = "Doli Local Edit - Secure Dolibarr document launcher"
_PROJECT_VERSION_PATTERN = re.compile(
    r'^version = "([0-9]+)\.([0-9]+)\.([0-9]+)"$',
    re.MULTILINE,
)


def _normalized_distribution_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def verify_build_environment() -> str | None:
    if sys.version_info[:3] != EXPECTED_PYTHON:
        return "Builds require the audited Python 3.14.7 runtime."
    if sys.prefix == sys.base_prefix or site.ENABLE_USER_SITE:
        return "Builds require an isolated virtual environment with user-site packages disabled."
    expected = dict(EXPECTED_DISTRIBUTIONS)
    if platform.system() == "Windows":
        expected.update(WINDOWS_DISTRIBUTIONS)
    installed: dict[str, str] = {}
    for distribution in metadata.distributions():
        name = distribution.metadata.get("Name")
        if not name:
            continue
        installed[_normalized_distribution_name(name)] = distribution.version
    for name, version in expected.items():
        if installed.get(name) != version:
            return f"Unexpected or missing build dependency: {name}=={version}."
    allowed = set(expected) | {"pip"}
    unexpected = sorted(set(installed) - allowed)
    if unexpected:
        return "Unexpected packages in the build environment: " + ", ".join(unexpected)
    return None


def project_version(repository: Path) -> tuple[str, tuple[int, int, int, int]]:
    content = (repository / "launcher" / "pyproject.toml").read_text(encoding="utf-8")
    matches = _PROJECT_VERSION_PATTERN.findall(content)
    if len(matches) != 1:
        raise RuntimeError("The launcher project version is invalid.")
    numeric = tuple(int(component) for component in matches[0])
    version = ".".join(str(component) for component in numeric)
    return version, (numeric[0], numeric[1], numeric[2], 0)


def windows_version_resource(version: str, numeric: tuple[int, int, int, int]) -> str:
    """Return the deterministic PyInstaller version resource for Windows Explorer."""
    return f"""VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={numeric!r},
    prodvers={numeric!r},
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable(
        '040904B0',
        [
          StringStruct('CompanyName', '{WINDOWS_COMPANY_NAME}'),
          StringStruct('FileDescription', '{WINDOWS_FILE_DESCRIPTION}'),
          StringStruct('FileVersion', '{version}'),
          StringStruct('InternalName', 'DoliLocalEditLauncher'),
          StringStruct('LegalCopyright', 'Copyright (C) 2026 {WINDOWS_COMPANY_NAME}'),
          StringStruct('OriginalFilename', 'dolilocaledit-launcher.exe'),
          StringStruct('ProductName', '{WINDOWS_PRODUCT_NAME}'),
          StringStruct('ProductVersion', '{version}')
        ]
      )
    ]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-directory", required=True, type=Path)
    parser.add_argument("--work-directory", type=Path)
    options = parser.parse_args()
    environment_error = verify_build_environment()
    if environment_error is not None:
        print(environment_error, file=sys.stderr)
        print(
            "Create a clean virtual environment and install launcher/requirements-build.txt "
            "with --require-hashes.",
            file=sys.stderr,
        )
        return 65
    repository = Path(__file__).resolve().parent.parent
    output = options.output_directory.resolve()
    output.mkdir(mode=0o755, parents=True, exist_ok=True)
    work_root = (
        options.work_directory.resolve()
        if options.work_directory is not None
        else repository / "build" / "pyinstaller" / platform.system().lower()
    )
    work_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    build_environment = os.environ.copy()
    for variable in tuple(build_environment):
        if variable in {"PYTHONHOME", "PYTHONPATH"} or variable.startswith(("_PYI", "PYINSTALLER_")):
            build_environment.pop(variable, None)
    build_environment.update(
        {
            "PYTHONHASHSEED": "0",
            "PYTHONNOUSERSITE": "1",
            "SOURCE_DATE_EPOCH": "0",
        }
    )
    with tempfile.TemporaryDirectory(prefix="isolated-", dir=work_root) as work_directory:
        work = Path(work_directory)
        command = [
            sys.executable,
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--clean",
            "--noupx",
            "--onefile",
            "--name",
            "dolilocaledit-launcher",
            "--paths",
            str(repository / "launcher" / "src"),
            "--distpath",
            str(output),
            "--workpath",
            str(work / "work"),
            "--specpath",
            str(work / "spec"),
        ]
        if platform.system() == "Windows":
            version, numeric_version = project_version(repository)
            version_file = work / "windows-version-info.txt"
            version_file.write_text(
                windows_version_resource(version, numeric_version),
                encoding="utf-8",
            )
            command.extend(["--windowed", "--version-file", str(version_file)])
        command.append(str(repository / "launcher" / "entrypoint.py"))
        result = subprocess.run(command, check=False, env=build_environment).returncode
    if result == 0 and platform.system() == "Windows":
        shutil.copy2(repository / "launcher" / "windows" / "install.cmd", output / "install.cmd")
    if result == 0 and platform.system() == "Linux":
        installer = output / "install.sh"
        shutil.copy2(repository / "launcher" / "linux" / "install.sh", installer)
        installer.chmod(0o755)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
