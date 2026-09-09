# Doli Local Edit Launcher

Official desktop launcher for **Doli Local Edit**. It registers the `dolilocaledit://` protocol,
downloads one authorized Dolibarr document, opens it with an application selected on the local
computer, and publishes a validated revision through the module API.

This public repository is intentionally limited to the launcher, its tests and the reproducible
build scripts. It does not contain the private Dolibarr module, customer data, private signing
keys or credentials. The OpenPGP public verification key is intentionally published.

## Downloads

Use the [versioned GitHub releases](https://github.com/GregChant/dolilocaledit-launcher/releases):

- Windows x86-64: `DoliLocalEdit-Setup-<version>.exe`;
- Linux x86-64: `DoliLocalEdit-linux-x86_64-<version>.tar.gz`.

The current stable release is **1.0.2**. On Windows, run the signed setup executable; it installs
the launcher for the current user without administrator rights. On Linux, extract the archive and
run `install.sh`; it installs under `~/.local` without administrator rights.

Interactive installation now reports an explicit success or failure result. Dolibarr can then
run a dedicated launcher check that reports the installed version and platform without
downloading or opening a document.

The Windows executable is Authenticode-signed and timestamped by **Experts Conseils Chanton**.
Verify the Digital Signatures tab before installation. Linux releases include the archive's
detached OpenPGP signature, signed `SHA256SUMS`, and `dolilocaledit-release-key.asc`. Verify the
full fingerprint through an independent channel before trusting the bundled public key.

Never download a launcher from an unversioned URL or a repository other than this one. Published
release assets are not replaced: a change requires a new version.

## Source development

The launcher has no third-party runtime dependency. From the repository root:

```bash
PYTHONPATH=launcher/src python3 -m unittest discover -s launcher/tests -v
./scripts/build-linux-launcher.sh dist/linux
./scripts/build-windows-launcher.sh dist/windows
```

The Linux builder uses a pinned Ubuntu image, compiles the pinned Python runtime and rebuilds the
PyInstaller bootloader with hardening. The Windows builder runs from WSL2 and validates the pinned
Python runtime and its Authenticode signature before the isolated build. Signing the final Windows
file requires the publisher's private certificate and is deliberately not automated in public CI.
`scripts/sign-linux-release.sh` signs the Linux archive and checksum list with an exact OpenPGP
fingerprint already available in the publisher's protected GnuPG keyring; it never reads or
exports private key material.

The official Linux signing fingerprint is:

```text
A51F BBAB 9A50 9277 1768  E839 8C95 07E9 997C 0569
```

More operational details are in [launcher/README.md](launcher/README.md).

## License

Copyright (C) 2026 Experts Conseils Chanton.

The launcher source and published binaries are licensed under GPL-3.0-or-later. The signing key,
publisher identity and trademarks are not granted by that license.
