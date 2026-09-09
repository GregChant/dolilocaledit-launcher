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
            windows = files / "DoliLocalEdit-Setup-1.0.0.exe"
            linux = files / "DoliLocalEdit-linux-x86_64-1.0.0.tar.gz"
            windows.write_bytes(b"signed windows fixture")
            linux.write_bytes(b"linux fixture")
            result = subprocess.run(
                [str(script), "1.0.0", str(catalog)],
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

            linux.write_bytes(b"tampered linux fixture")
            rejected = subprocess.run(
                ["gpg", "--batch", "--verify", str(linux_signature), str(linux)],
                check=False,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.assertNotEqual(rejected.returncode, 0)


if __name__ == "__main__":
    unittest.main()
