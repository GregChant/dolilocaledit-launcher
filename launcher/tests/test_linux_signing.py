import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


@unittest.skipUnless(shutil.which("gpg") and shutil.which("sha256sum"), "GnuPG is required")
class LinuxReleaseSigningTest(unittest.TestCase):
    def test_release_files_are_signed_and_verified_with_the_exact_key(self) -> None:
        repository = Path(__file__).resolve().parents[2]
        script = repository / "scripts" / "sign-linux-release.sh"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            gpg_home = root / "gpg"
            gpg_home.mkdir(mode=0o700)
            environment = {**os.environ, "GNUPGHOME": str(gpg_home)}
            generated = subprocess.run(
                [
                    "gpg",
                    "--batch",
                    "--pinentry-mode",
                    "loopback",
                    "--passphrase",
                    "",
                    "--quick-generate-key",
                    "Doli Local Edit Test Signing <release-test@example.invalid>",
                    "ed25519",
                    "sign",
                    "1d",
                ],
                check=False,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.assertEqual(generated.returncode, 0, generated.stderr.decode("utf-8", "replace"))
            listing = subprocess.run(
                ["gpg", "--batch", "--with-colons", "--fingerprint", "--list-secret-keys"],
                check=True,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            ).stdout
            fingerprint = next(
                line.split(":")[9]
                for line in listing.splitlines()
                if line.startswith("fpr:")
            )
            exported = subprocess.run(
                ["gpg", "--batch", "--armor", "--export", fingerprint],
                check=True,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            ).stdout
            public_key = root / "release-key.asc"
            public_key.write_bytes(exported)

            catalog = root / "catalog"
            files = catalog / "files"
            files.mkdir(parents=True)
            windows = files / "DoliLocalEdit-Setup-1.0.3.exe"
            linux = files / "DoliLocalEdit-linux-x86_64-1.0.3.tar.gz"
            windows.write_bytes(b"signed windows fixture")
            linux.write_bytes(b"linux fixture")
            result = subprocess.run(
                [str(script), "1.0.3", str(catalog)],
                check=False,
                env={
                    **environment,
                    "DLE_LINUX_SIGNING_KEY_FINGERPRINT": fingerprint,
                    "DLE_LINUX_SIGNING_PUBLIC_KEY": str(public_key),
                },
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(fingerprint, result.stdout)

            linux_signature = linux.with_name(linux.name + ".asc")
            checksum_file = catalog / "SHA256SUMS"
            checksum_signature = catalog / "SHA256SUMS.asc"
            self.assertTrue(linux_signature.is_file())
            self.assertTrue(checksum_signature.is_file())
            self.assertEqual(
                (catalog / "dolilocaledit-release-key.asc").read_bytes(),
                exported,
            )
            checksum_lines = checksum_file.read_text(encoding="ascii").splitlines()
            self.assertEqual(len(checksum_lines), 2)
            self.assertIn(hashlib.sha256(windows.read_bytes()).hexdigest(), checksum_lines[0])
            self.assertIn(hashlib.sha256(linux.read_bytes()).hexdigest(), checksum_lines[1])
            verified = subprocess.run(
                ["gpg", "--batch", "--verify", str(linux_signature), str(linux)],
                check=False,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.assertEqual(verified.returncode, 0, verified.stderr.decode("utf-8", "replace"))

            # The generic command exports the public key and delegates both signatures.
            generic = repository / "scripts" / "sign-linux.sh"
            generic_environment = {
                **environment,
                "DLE_LINUX_SIGNING_KEY_FINGERPRINT": fingerprint,
                "DLE_LINUX_RELEASE_CATALOG": str(catalog),
                "TMPDIR": str(root),
            }
            generic_environment.pop("DLE_LINUX_SIGNING_PUBLIC_KEY", None)
            result = subprocess.run(
                [str(generic), "--V=1.0.3"],
                cwd=root,
                check=False,
                env=generic_environment,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(fingerprint, result.stdout)
            self.assertEqual((catalog / "dolilocaledit-release-key.asc").read_bytes(), exported)
            self.assertEqual(list(root.glob("dolilocaledit-public-signing-key.*")), [])
            self.assertEqual((catalog / "SHA256SUMS").read_text(encoding="ascii").splitlines(), checksum_lines)

            linux.write_bytes(b"tampered linux fixture")
            rejected = subprocess.run(
                ["gpg", "--batch", "--verify", str(linux_signature), str(linux)],
                check=False,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.assertNotEqual(rejected.returncode, 0)


class GenericSigningArgumentsTest(unittest.TestCase):
    def test_invalid_versions_are_rejected_before_signing(self) -> None:
        script = Path(__file__).resolve().parents[2] / "scripts" / "sign-linux.sh"
        for arguments in (
            [], ["--V="], ["--V=../1.1.0"], ["--V=1.1.0-rc1"],
            ["--V=1.1.0", "--V=1.2.0"], ["--V=", "--V=1.1.0"], ["--unknown"],
        ):
            with self.subTest(arguments=arguments):
                result = subprocess.run([str(script), *arguments], capture_output=True, text=True)
                self.assertEqual(result.returncode, 64, result.stderr)

    def test_missing_catalog_is_rejected_before_authentication(self) -> None:
        script = Path(__file__).resolve().parents[2] / "scripts" / "sign-linux.sh"
        with tempfile.TemporaryDirectory() as temporary:
            result = subprocess.run(
                [str(script), "--V=1.1.0"],
                env={**os.environ, "DLE_LINUX_RELEASE_CATALOG": str(Path(temporary) / "missing")},
                capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 65, result.stderr)
            self.assertIn("Release catalog not found", result.stderr)

    def test_help_is_available_without_authentication(self) -> None:
        script = Path(__file__).resolve().parents[2] / "scripts" / "sign-linux.sh"
        result = subprocess.run([str(script), "--help"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--V=x.y.z", result.stdout)


if __name__ == "__main__":
    unittest.main()
