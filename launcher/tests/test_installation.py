import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from dolilocaledit_launcher.errors import ConfigurationError
from dolilocaledit_launcher.installation import (
    RunningLauncher,
    cleanup_obsolete_windows_launchers,
    default_windows_install_path,
    install_for_current_user,
    uninstall_for_current_user,
)


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
                installed = install_for_current_user(source, "1.0.3")
            digest = hashlib.sha256(b"frozen-launcher").hexdigest()
            self.assertEqual(
                installed.path,
                root
                / "local"
                / "Programs"
                / "DoliLocalEdit"
                / f"dolilocaledit-launcher-1.0.3-{digest}.exe",
            )
            self.assertEqual(installed.path.read_bytes(), b"frozen-launcher")
            register.assert_called_once_with(installed.path)

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
                installed = install_for_current_user(source, "1.0.3")
            self.assertEqual(installed.path, target)
            self.assertEqual(installed.path.read_bytes(), b"frozen-linux-launcher")
            self.assertEqual(installed.path.stat().st_mode & 0o777, 0o700)
            register.assert_called_once_with(installed.path)

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

    def test_linux_upgrade_atomically_replaces_the_installed_binary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "downloaded-launcher"
            source.write_bytes(b"new-launcher")
            target = root / "installed-launcher"
            target.write_bytes(b"running-launcher")
            with (
                target.open("rb") as running_binary,
                patch("dolilocaledit_launcher.installation.sys.platform", "linux"),
                patch("dolilocaledit_launcher.installation.default_linux_install_path", return_value=target),
                patch("dolilocaledit_launcher.installation.register_protocol") as register,
            ):
                installed = install_for_current_user(source, "1.0.3")
                self.assertEqual(running_binary.read(), b"running-launcher")
            self.assertEqual(installed.path.read_bytes(), b"new-launcher")
            register.assert_called_once_with(target)

    def test_windows_reinstall_rejects_changed_immutable_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "downloaded-launcher.exe"
            source.write_bytes(b"new-launcher")
            target = root / "installed-launcher.exe"
            target.write_bytes(b"unexpected-launcher")
            with (
                patch("dolilocaledit_launcher.installation.sys.platform", "win32"),
                patch("dolilocaledit_launcher.installation.default_windows_install_path", return_value=target),
                patch("dolilocaledit_launcher.installation.register_protocol") as register,
            ):
                with self.assertRaises(ConfigurationError) as context:
                    install_for_current_user(source, "1.0.3")
            self.assertEqual(context.exception.code, "installation_target_invalid")
            self.assertEqual(target.read_bytes(), b"unexpected-launcher")
            register.assert_not_called()

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
                    install_for_current_user(source, "1.0.3")
            self.assertEqual(context.exception.code, "installation_failed")

    def test_install_converts_a_system_registration_failure_to_a_visible_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "launcher.exe"
            source.write_bytes(b"launcher")
            target = root / "installed" / "launcher.exe"
            with (
                patch("dolilocaledit_launcher.installation.sys.platform", "win32"),
                patch("dolilocaledit_launcher.installation.default_windows_install_path", return_value=target),
                patch("dolilocaledit_launcher.installation.register_protocol", side_effect=PermissionError("denied")),
            ):
                with self.assertRaises(ConfigurationError) as context:
                    install_for_current_user(source, "1.0.3")
            self.assertEqual(context.exception.code, "registration_failed")

    def test_windows_upgrade_keeps_running_previous_version_and_switches_protocol(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "download" / "dolilocaledit-launcher.exe"
            source.parent.mkdir()
            source.write_bytes(b"new-launcher")
            install_directory = root / "local" / "Programs" / "DoliLocalEdit"
            install_directory.mkdir(parents=True)
            previous = install_directory / "dolilocaledit-launcher.exe"
            previous.write_bytes(b"running-launcher")
            running = (RunningLauncher(1234, previous),)
            original_unlink = Path.unlink

            def keep_running(path: Path) -> None:
                if path == previous:
                    raise PermissionError("in use")
                original_unlink(path)

            with (
                patch("dolilocaledit_launcher.installation.sys.platform", "win32"),
                patch.dict("os.environ", {"LOCALAPPDATA": str(root / "local")}),
                patch("dolilocaledit_launcher.installation.register_protocol") as register,
                patch("dolilocaledit_launcher.installation.find_running_windows_launchers", return_value=running),
                patch("dolilocaledit_launcher.installation.Path.unlink", autospec=True, side_effect=keep_running),
            ):
                installed = install_for_current_user(source, "1.0.3")

            self.assertTrue(installed.path.is_file())
            self.assertNotEqual(installed.path, previous)
            self.assertEqual(installed.running_previous_instances, running)
            self.assertEqual(installed.retained_previous_files, (previous,))
            register.assert_called_once_with(installed.path)

    def test_windows_reinstall_reuses_identical_immutable_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "download" / "dolilocaledit-launcher.exe"
            source.parent.mkdir()
            source.write_bytes(b"same-launcher")
            digest = hashlib.sha256(b"same-launcher").hexdigest()
            target = (
                root
                / "local"
                / "Programs"
                / "DoliLocalEdit"
                / f"dolilocaledit-launcher-1.0.3-{digest}.exe"
            )
            target.parent.mkdir(parents=True)
            target.write_bytes(b"same-launcher")
            with (
                patch("dolilocaledit_launcher.installation.sys.platform", "win32"),
                patch.dict("os.environ", {"LOCALAPPDATA": str(root / "local")}),
                patch("dolilocaledit_launcher.installation.register_protocol") as register,
            ):
                installed = install_for_current_user(source, "1.0.3")
            self.assertEqual(installed.path, target)
            register.assert_called_once_with(target)

    def test_cleanup_ignores_unrelated_windows_executables(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            install_directory = root / "local" / "Programs" / "DoliLocalEdit"
            install_directory.mkdir(parents=True)
            active = install_directory / f"dolilocaledit-launcher-1.0.3-{'a' * 64}.exe"
            active.write_bytes(b"active")
            obsolete = install_directory / "dolilocaledit-launcher.exe"
            obsolete.write_bytes(b"old")
            unrelated = install_directory / "customer-tool.exe"
            unrelated.write_bytes(b"keep")
            with (
                patch("dolilocaledit_launcher.installation.sys.platform", "win32"),
                patch.dict("os.environ", {"LOCALAPPDATA": str(root / "local")}),
            ):
                retained = cleanup_obsolete_windows_launchers(active)
            self.assertEqual(retained, ())
            self.assertFalse(obsolete.exists())
            self.assertTrue(active.exists())
            self.assertTrue(unrelated.exists())

    def test_windows_install_rejects_invalid_version(self) -> None:
        with self.assertRaises(ConfigurationError) as context:
            default_windows_install_path("1.0.3-beta", "a" * 64)
        self.assertEqual(context.exception.code, "installation_version_invalid")


if __name__ == "__main__":
    unittest.main()
