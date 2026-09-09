#!/usr/bin/env python3
"""Create the embedded launcher catalog and optionally append it to a module ZIP."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
from pathlib import Path
import shutil
import stat
import tarfile
import tempfile
from urllib.parse import quote, urlsplit
import zipfile


MAX_ARTIFACT_BYTES = 256 * 1024 * 1024
OFFICIAL_RELEASE_ROOT = "https://github.com/GregChant/dolilocaledit-launcher/releases/download"


def _regular_file(path: Path) -> Path:
    resolved = path.expanduser().resolve(strict=True)
    details = resolved.stat()
    if not stat.S_ISREG(details.st_mode) or path.is_symlink():
        raise ValueError(f"Not a regular artifact: {path}")
    if details.st_size < 1 or details.st_size > MAX_ARTIFACT_BYTES:
        raise ValueError(f"Artifact size is invalid: {path}")
    return resolved


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _copy_windows(source: Path, files: Path, version: str) -> dict[str, object]:
    target = files / f"DoliLocalEdit-Setup-{version}.exe"
    shutil.copyfile(_regular_file(source), target)
    target.chmod(0o644)
    return _entry(
        "windows",
        target,
        target.name,
        "application/vnd.microsoft.portable-executable",
    )


def _package_linux(source: Path, installer: Path, files: Path, version: str) -> dict[str, object]:
    executable = _regular_file(source)
    install_script = _regular_file(installer)
    target = files / f"DoliLocalEdit-linux-x86_64-{version}.tar.gz"
    with target.open("wb") as raw_stream:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw_stream, mtime=0) as gzip_stream:
            with tarfile.open(fileobj=gzip_stream, mode="w") as archive:
                for source_path, name, mode in (
                    (executable, "DoliLocalEdit/dolilocaledit-launcher", 0o755),
                    (install_script, "DoliLocalEdit/install.sh", 0o755),
                ):
                    details = source_path.stat()
                    entry = tarfile.TarInfo(name)
                    entry.size = details.st_size
                    entry.mode = mode
                    entry.uid = 0
                    entry.gid = 0
                    entry.uname = "root"
                    entry.gname = "root"
                    entry.mtime = 0
                    with source_path.open("rb") as content:
                        archive.addfile(entry, content)
    target.chmod(0o644)
    return _entry("linux", target, target.name, "application/gzip")


def _entry(platform: str, path: Path, download_name: str, media_type: str) -> dict[str, object]:
    details = path.stat()
    if details.st_size < 1 or details.st_size > MAX_ARTIFACT_BYTES:
        raise ValueError(f"Packaged artifact size is invalid: {path}")
    return {
        "platform": platform,
        "architecture": "x86_64",
        "filename": path.name,
        "download_name": download_name,
        "media_type": media_type,
        "byte_size": details.st_size,
        "sha256": _sha256(path),
    }


def _append_catalog(module_zip: Path, catalog: Path, include_files: bool) -> None:
    archive_path = module_zip.resolve(strict=True)
    sources = [(catalog / "manifest.json", "manifest.json", 0o644)]
    if include_files:
        sources.extend(
            (path, f"files/{path.name}", 0o644)
            for path in sorted((catalog / "files").iterdir())
            if path.is_file()
        )
    with zipfile.ZipFile(archive_path, "a", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for source, relative, mode in sources:
            info = zipfile.ZipInfo(f"dolilocaledit/resources/launchers/{relative}")
            info.date_time = (1980, 1, 1, 0, 0, 0)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (stat.S_IFREG | mode) << 16
            with source.open("rb") as input_stream, archive.open(info, "w") as output_stream:
                shutil.copyfileobj(input_stream, output_stream, length=1024 * 1024)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", required=True)
    parser.add_argument("--catalog-directory", required=True, type=Path)
    parser.add_argument("--module-zip", type=Path)
    parser.add_argument("--windows-executable", type=Path)
    parser.add_argument("--linux-executable", type=Path)
    parser.add_argument("--external-download-base-url")
    parser.add_argument(
        "--linux-installer",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "launcher" / "linux" / "install.sh",
    )
    options = parser.parse_args()
    allowed = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
    if not options.version or any(character not in allowed for character in options.version):
        parser.error("invalid version")
    if options.windows_executable is None and options.linux_executable is None:
        parser.error("at least one launcher executable is required")
    external_base_url = None
    if options.external_download_base_url is not None:
        external_base_url = options.external_download_base_url.rstrip("/")
        expected_base_url = f"{OFFICIAL_RELEASE_ROOT}/v{quote(options.version, safe='')}"
        parsed_base_url = urlsplit(external_base_url)
        if (
            external_base_url != expected_base_url
            or parsed_base_url.scheme != "https"
            or parsed_base_url.netloc != "github.com"
            or parsed_base_url.query
            or parsed_base_url.fragment
        ):
            parser.error("external download base URL is not the immutable official release URL")

    destination = options.catalog_directory.resolve()
    destination.mkdir(mode=0o755, parents=True, exist_ok=True)
    files = destination / "files"
    files.mkdir(mode=0o755, parents=True, exist_ok=True)
    for generated_name in (
        f"DoliLocalEdit-Setup-{options.version}.exe",
        f"DoliLocalEdit-linux-x86_64-{options.version}.tar.gz",
    ):
        generated = files / generated_name
        if generated.is_file() and not generated.is_symlink():
            generated.unlink()
    entries: list[dict[str, object]] = []
    if options.windows_executable is not None:
        entries.append(_copy_windows(options.windows_executable, files, options.version))
    if options.linux_executable is not None:
        entries.append(_package_linux(options.linux_executable, options.linux_installer, files, options.version))
    if external_base_url is not None:
        for entry in entries:
            entry["url"] = external_base_url + "/" + quote(str(entry["filename"]), safe="")
    manifest = {
        "schema": 2 if external_base_url is not None else 1,
        "version": options.version,
        "artifacts": entries,
    }
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=destination, delete=False) as stream:
        temporary = Path(stream.name)
        json.dump(manifest, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        stream.flush()
    temporary.replace(destination / "manifest.json")
    if options.module_zip is not None:
        _append_catalog(options.module_zip, destination, include_files=external_base_url is None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
