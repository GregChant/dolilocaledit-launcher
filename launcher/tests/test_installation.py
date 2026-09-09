from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from dolilocaledit_launcher.errors import ConfigurationError
from dolilocaledit_launcher.installation import install_for_current_user, uninstall_for_current_user


class InstallationTest(unittest.TestCase):
    def test_windows_install_copies_then_registers_per_user(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "download" / "dolilocaledit-launcher.exe"
            source.parent.mkdir()
            source.write_bytes(b"frozen-launcher")
            with (
                patch("dolilocaledit_launcher.installation.sys.platform", "win32"),
                patch.dict("os.environ", {"LOCALAPPDATA": str(root / "local")}),
                patch("dolilocaledit_launcher.installation.register_protocol") as register,
            ):
                installed = install_for_current_user(source)
            self.assertEqual(
                installed,
                root / "local" / "Programs" / "DoliLocalEdit" / "dolilocaledit-launcher.exe",
            )
            self.assertEqual(installed.read_bytes(), b"frozen-launcher")
            register.assert_called_once_with(installed)

    def test_linux_install_is_private_and_registers_without_admin(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "download" / "dolilocaledit-launcher"
            source.parent.mkdir()
            source.write_bytes(b"frozen-linux-launcher")
            target = root / ".local" / "bin" / "dolilocaledit-launcher"
            with (
                patch("dolilocaledit_launcher.installation.sys.platform", "linux"),
                patch("dolilocaledit_launcher.installation.default_linux_install_path", return_value=target),
                patch("dolilocaledit_launcher.installation.register_protocol") as register,
            ):
                installed = install_for_current_user(source)
            self.assertEqual(installed, target)
            self.assertEqual(installed.read_bytes(), b"frozen-linux-launcher")
            self.assertEqual(installed.stat().st_mode & 0o777, 0o700)
            register.assert_called_once_with(installed)

    def test_linux_uninstall_removes_program_but_delegates_protocol_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / ".local" / "bin" / "dolilocaledit-launcher"
            target.parent.mkdir(parents=True)
            target.write_bytes(b"launcher")
            with (
                patch("dolilocaledit_launcher.installation.sys.platform", "linux"),
                patch("dolilocaledit_launcher.installation.default_linux_install_path", return_value=target),
                patch("dolilocaledit_launcher.installation.unregister_linux_protocol") as unregister,
            ):
                removed = uninstall_for_current_user()
            self.assertEqual(removed, target)
            self.assertFalse(target.exists())
            unregister.assert_called_once_with()

    def test_integrated_install_refuses_unsupported_platform(self) -> None:
        with patch("dolilocaledit_launcher.installation.sys.platform", "darwin"):
            with self.assertRaises(ConfigurationError) as context:
                install_for_current_user(Path("launcher"))
        self.assertEqual(context.exception.code, "installation_unsupported")

    def test_windows_install_reports_an_unwritable_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "launcher.exe"
            source.write_bytes(b"launcher")
            blocked = root / "blocked"
            blocked.write_bytes(b"not-a-directory")
            target = blocked / "dolilocaledit-launcher.exe"
            with (
                patch("dolilocaledit_launcher.installation.sys.platform", "win32"),
                patch("dolilocaledit_launcher.installation.default_windows_install_path", return_value=target),
            ):
                with self.assertRaises(ConfigurationError) as context:
                    install_for_current_user(source)
            self.assertEqual(context.exception.code, "installation_failed")


if __name__ == "__main__":
    unittest.main()
