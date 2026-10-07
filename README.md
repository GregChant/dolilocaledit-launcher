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

The current stable release is **1.1.0**. On Windows, run the signed setup executable; it installs
the launcher for the current user without administrator rights. On Linux, extract the archive and
run `install.sh`; it installs under `~/.local` without administrator rights.

Interactive installation now reports an explicit success or failure result. Dolibarr can then
run a dedicated launcher check that reports the installed version and platform without
downloading or opening a document.

Windows upgrades are installed side by side under an immutable version-and-SHA-256 filename. The
protocol registration switches to the new file immediately, while an older process is never
terminated and may finish protecting its current editing session. Retained files are cleaned on
a later launch once Windows releases them. The launcher also accepts both browser forms
`dolilocaledit://check?…` and `dolilocaledit://check/?…`.

Version 1.0.4 preserves recovery copies whenever an editor is still running or its closure cannot
be observed, including after an unchanged session expires or a network failure. Editors that
replace their working file receive a 30-second grace period while heartbeats continue. Linux
upgrades replace the executable atomically so existing editing sessions can keep running.
Protocol workers use independent PyInstaller instances to avoid temporary-directory cleanup
races.

Version 1.0.5 keeps the server lease through intermediate saves and publishes the final stable
revision only after the exact document closes or the user explicitly finishes locally. Windows
Office document tracking supports reused application instances; recognized LibreOffice commands
observe the document owner marker. If closure is unknown, the local completion control asks the
user to save and close first and preserves the working copy after publication.

Version 1.1.0 rebuilds the launcher for the matching Doli Local Edit module release.
The editing workflow is unchanged. The module now provides document revision history,
downloads and restoration as a new revision, with a daily Dolibarr scheduled purge that
keeps revisions for 90 days, at least the latest ten revisions and all pinned revisions.
Update both the module and the desktop launcher to 1.1.0.

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

From an interactive terminal, sign a prepared versioned release catalog with:

```bash
./scripts/sign-linux.sh --V=1.1.0
```

The command uses `build/release-launchers-1.1.0`, exports only the public verification
key to a temporary directory and prompts through GnuPG when authentication is needed.
Optional environment settings select another complete signing-key fingerprint, public
verification-key file or catalog: `DLE_LINUX_SIGNING_KEY_FINGERPRINT`,
`DLE_LINUX_SIGNING_PUBLIC_KEY` and `DLE_LINUX_RELEASE_CATALOG`.

The official Linux signing fingerprint is:

```text
A51F BBAB 9A50 9277 1768  E839 8C95 07E9 997C 0569
```

More operational details are in [launcher/README.md](launcher/README.md).

## License

Copyright (C) 2026 Experts Conseils Chanton.

The launcher source and published binaries are licensed under [GPL-3.0-or-later](COPYING). The signing key,
publisher identity and trademarks are not granted by that license.
