# Doli Local Edit Launcher

Official desktop launcher for **Doli Local Edit**. It registers the `dolilocaledit://` protocol,
downloads an authorized Dolibarr document, opens it with an application selected on the local
computer, and publishes a validated revision through the module API.

This repository contains the launcher, its tests and reproducible build scripts. The OpenPGP
public verification key is published here; private signing keys and credentials remain with
the publisher.

## Downloads and installation

The current launcher version is **1.1.1**. Download from its
[versioned release](https://github.com/GregChant/dolilocaledit-launcher/releases/tag/v1.1.1):

- Windows x86-64: `DoliLocalEdit-Setup.exe`;
- Linux x86-64: `DoliLocalEdit-linux-x86_64.tar.gz`.

File names stay the same across launcher releases. Windows file/product properties and
`--version` identify the launcher version. URLs keep an immutable versioned release tag.
Historical releases retain their original assets and versioned file names.

Run the signed Windows executable, or extract the Linux archive and run `install.sh`.
Installation uses the current user's profile and requires no administrator rights.
Dolibarr can check the installed version and platform without opening a document.

Windows 1.1.1 adds an entry in Installed apps / Programs and Features. Uninstall there,
or run `DoliLocalEdit-Setup.exe uninstall`; add `--quiet` to suppress dialogs. Uninstallation
keeps local configuration and recovery documents, preserves a protocol association taken over
by another application, and never terminates editing processes. Program files still in use
are retained for deferred cleanup.

Windows upgrades install side by side under an immutable version-and-SHA-256 file name.
The protocol switches to the new file while previous editing processes can finish safely.
Linux upgrades replace the executable atomically so current sessions can keep running.

Intermediate saves keep the server lease. The launcher publishes the final stable revision
when the exact document closes, or when the user explicitly finishes locally. Unavailable
closure observations remain unknown, and recovery copies are preserved when needed.

## Independent module and launcher versions

A module-only update reuses the exact signed launcher bytes, release URL and checksum when
its client requirements remain compatible. Updating the Dolibarr module does not itself
require a launcher update. A new launcher version is reserved for client behavior, security,
build toolchain or compatibility changes. Version 1.1.1 adds Windows uninstallation and
therefore requires a new launcher build.

The module release tooling compares a reviewed build-input inventory before allowing reuse.
It covers the runtime sources, dependency pins, installers and build toolchain. Changed,
added or removed inputs require a new launcher release. The shared inventory helper is
`scripts/launcher-release-inputs.py`; module packaging belongs to the separate module project.

## Verify releases

Windows binaries are Authenticode-signed and timestamped by **Experts Conseils Chanton**.
Check the Digital Signatures tab and the release checksums. Signing establishes publisher
identity and integrity; Windows SmartScreen reputation can still warn about a new binary.

Linux releases include the archive's detached OpenPGP signature, signed `SHA256SUMS`, and
`dolilocaledit-release-key.asc`. Verify the full fingerprint through an independent channel
before trusting the bundled public key:

```text
A51F BBAB 9A50 9277 1768  E839 8C95 07E9 997C 0569
```

Use this repository's immutable versioned URLs. Published assets are not replaced.

## Source development and builds

The launcher has no third-party runtime dependency. From the repository root:

```bash
PYTHONPATH=launcher/src python3 -W error::ResourceWarning -m unittest discover -s launcher/tests -v
./scripts/build-linux-launcher.sh dist/linux
./scripts/build-windows-launcher.sh dist/windows
```

The Linux builder uses a pinned Ubuntu image, compiles the pinned Python runtime and rebuilds
the PyInstaller bootloader with hardening. The Windows builder validates its pinned Python
runtime and Authenticode signature before building in an isolated environment. Official
builds use Python 3.14.7 and PyInstaller 6.22.2 with hash-locked dependencies.

After building a new launcher version, `scripts/package-launchers.py` creates its release
catalog. Sign a prepared Linux catalog from an interactive terminal:

```bash
./scripts/sign-linux.sh --V=1.1.1
```

The version is the launcher's version, independent of the module. The command selects
`build/release-launchers-1.1.1`, exports only the public key to a temporary directory, and
lets GnuPG request authentication. Configuration may override
`DLE_LINUX_SIGNING_KEY_FINGERPRINT`, `DLE_LINUX_SIGNING_PUBLIC_KEY` or
`DLE_LINUX_RELEASE_CATALOG`. Private key material is never exported.

See [launcher/README.md](launcher/README.md) for local configuration and client details.

## License

Copyright (C) 2026 Experts Conseils Chanton.

Source and published binaries are licensed under [GPL-3.0-or-later](COPYING).
The signing key, publisher identity and trademarks are not granted by that license.
