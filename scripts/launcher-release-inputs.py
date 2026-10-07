#!/usr/bin/env python3
"""Bind a reusable launcher catalog to the sources and toolchain that produced it."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import tempfile


FIXED_INPUTS = (
    "launcher/build.py",
    "launcher/entrypoint.py",
    "launcher/pyproject.toml",
    "launcher/requirements-build.txt",
    "launcher/linux/install.sh",
    "launcher/linux/Dockerfile.build",
    "launcher/windows/build-secure.ps1",
    "scripts/build-linux-launcher.sh",
    "scripts/build-windows-launcher.sh",
)
VERSION_PATTERN = re.compile(r'^version = "([0-9]+\.[0-9]+\.[0-9]+)"$', re.MULTILINE)
RUNTIME_VERSION_PATTERN = re.compile(r'^__version__ = "([0-9]+\.[0-9]+\.[0-9]+)"$', re.MULTILINE)
SHA256_PATTERN = re.compile(r"[a-f0-9]{64}")
NAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


class ReleaseInputError(RuntimeError):
    """A catalog cannot be reused with the current launcher build inputs."""


def _regular_file(source_root: Path, relative: str) -> Path:
    path = source_root / relative
    if not path.is_file() or any(parent.is_symlink() for parent in (path, *path.parents) if parent != source_root):
        raise ReleaseInputError(f"Launcher build input is missing or unsafe: {relative}")
    return path


def launcher_version(source_root: Path) -> str:
    content = _regular_file(source_root, "launcher/pyproject.toml").read_text(encoding="utf-8")
    versions = VERSION_PATTERN.findall(content)
    if len(versions) != 1:
        raise ReleaseInputError("Launcher source version must use the x.y.z numeric format")
    runtime = _regular_file(source_root, "launcher/src/dolilocaledit_launcher/__init__.py")
    runtime_versions = RUNTIME_VERSION_PATTERN.findall(runtime.read_text(encoding="utf-8"))
    if runtime_versions != versions:
        raise ReleaseInputError("Launcher runtime version does not match its project metadata")
    return versions[0]


def _source_hashes(source_root: Path) -> dict[str, str]:
    source_directory = source_root / "launcher/src"
    if not source_directory.is_dir() or source_directory.is_symlink():
        raise ReleaseInputError("Launcher runtime source directory is missing or unsafe")
    sources = set()
    for path in source_directory.rglob("*"):
        if path.is_symlink():
            raise ReleaseInputError("Launcher runtime sources contain a symbolic link")
        if path.is_file() and "__pycache__" not in path.parts:
            sources.add(path.relative_to(source_root).as_posix())
    if not sources:
        raise ReleaseInputError("Launcher runtime sources are missing")
    sources.update(FIXED_INPUTS)
    return {
        relative: hashlib.sha256(_regular_file(source_root, relative).read_bytes()).hexdigest()
        for relative in sorted(sources)
    }


def _artifact_identity(manifest: dict[str, object]) -> list[dict[str, object]]:
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        raise ReleaseInputError("Launcher catalog contains no artifacts")
    selected = []
    identities = set()
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            raise ReleaseInputError("Launcher catalog artifact is invalid")
        platform = artifact.get("platform")
        architecture = artifact.get("architecture")
        filename = artifact.get("filename")
        digest = artifact.get("sha256")
        size = artifact.get("byte_size")
        identity = (platform, architecture)
        if (
            not isinstance(platform, str)
            or platform not in {"windows", "linux"}
            or not isinstance(architecture, str)
            or architecture != "x86_64"
            or identity in identities
            or not isinstance(filename, str)
            or NAME_PATTERN.fullmatch(filename) is None
            or not isinstance(digest, str)
            or SHA256_PATTERN.fullmatch(digest) is None
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 1
            or size > 256 * 1024 * 1024
        ):
            raise ReleaseInputError("Launcher catalog artifact identity is invalid")
        identities.add(identity)
        selected.append({
            key: artifact[key]
            for key in ("platform", "architecture", "filename", "byte_size", "sha256", "url")
            if key in artifact
        })
    return sorted(selected, key=lambda entry: str(entry["platform"]))


def _read_json(path: Path, description: str) -> dict[str, object]:
    if not path.is_file() or path.is_symlink():
        raise ReleaseInputError(f"{description} is missing or unsafe: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseInputError(f"{description} is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise ReleaseInputError(f"{description} is invalid")
    return value


def capture_inputs(source_root: Path, catalog_root: Path) -> dict[str, object]:
    manifest = _read_json(catalog_root / "manifest.json", "Launcher manifest")
    version = launcher_version(source_root)
    if manifest.get("version") != version:
        raise ReleaseInputError("Launcher catalog version does not match launcher source version")
    return {
        "schema": 1,
        "launcher_version": version,
        "inputs": _source_hashes(source_root),
        "artifacts": _artifact_identity(manifest),
    }


def verify_inputs(source_root: Path, catalog_root: Path) -> None:
    recorded = _read_json(catalog_root / "build-inputs.json", "Launcher build-input inventory")
    current = capture_inputs(source_root, catalog_root)
    if recorded.get("schema") != 1 or recorded.get("launcher_version") != current["launcher_version"]:
        raise ReleaseInputError("Launcher build-input inventory version does not match its catalog")
    if recorded.get("artifacts") != current["artifacts"]:
        raise ReleaseInputError("Launcher catalog artifacts do not match their recorded build inputs")
    inputs = recorded.get("inputs")
    if inputs != current["inputs"]:
        known = inputs if isinstance(inputs, dict) else {}
        actual = current["inputs"]
        changed = sorted(set(known) | set(actual))
        changed = [name for name in changed if known.get(name) != actual.get(name)]
        raise ReleaseInputError(
            "Launcher build inputs changed; release a new launcher version before packaging: "
            + ", ".join(changed)
        )


def write_inputs(source_root: Path, catalog_root: Path) -> None:
    recorded = capture_inputs(source_root, catalog_root)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=catalog_root, delete=False) as stream:
        temporary = Path(stream.name)
        json.dump(recorded, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    temporary.replace(catalog_root / "build-inputs.json")
    (catalog_root / "build-inputs.json").chmod(0o644)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--catalog-directory", type=Path, required=True)
    parser.add_argument("--write", action="store_true", help="Record inputs after building a new launcher release")
    options = parser.parse_args()
    try:
        root = options.source_root.resolve(strict=True)
        catalog = options.catalog_directory.resolve(strict=True)
        if options.write:
            write_inputs(root, catalog)
        else:
            verify_inputs(root, catalog)
    except (OSError, ReleaseInputError) as exc:
        parser.exit(1, f"Launcher reuse refused: {exc}\n")
    print("Launcher build inputs match their release catalog")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
