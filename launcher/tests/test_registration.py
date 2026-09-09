from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from dolilocaledit_launcher.registration import register_protocol, unregister_linux_protocol


class RegistrationTest(unittest.TestCase):
    def test_linux_registration_writes_desktop_entry_and_sets_handler(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            executable = root / "bin" / "dolilocaledit-launcher"
            executable.parent.mkdir()
            executable.write_bytes(b"launcher")
            data = root / "share"
            with (
                patch("dolilocaledit_launcher.registration.sys.platform", "linux"),
                patch.dict("dolilocaledit_launcher.registration.os.environ", {"XDG_DATA_HOME": str(data)}),
                patch("dolilocaledit_launcher.registration._set_linux_protocol_default") as set_default,
                patch("dolilocaledit_launcher.registration._refresh_linux_desktop_database") as refresh,
            ):
                desktop = register_protocol(executable)
            assert desktop is not None
            content = desktop.read_text(encoding="utf-8")
            self.assertIn("MimeType=x-scheme-handler/dolilocaledit;", content)
            self.assertEqual(desktop.stat().st_mode & 0o777, 0o644)
            self.assertIn(f'Exec="{executable}" open %u', content)
            self.assertNotIn("ticket", content.lower())
            set_default.assert_called_once_with("dolilocaledit-launcher.desktop")
            refresh.assert_called_once_with(data / "applications")

    def test_linux_unregistration_removes_only_the_desktop_entry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary) / "share"
            desktop = data / "applications" / "dolilocaledit-launcher.desktop"
            desktop.parent.mkdir(parents=True)
            desktop.write_text("[Desktop Entry]\n", encoding="utf-8")
            with (
                patch("dolilocaledit_launcher.registration.sys.platform", "linux"),
                patch.dict("dolilocaledit_launcher.registration.os.environ", {"XDG_DATA_HOME": str(data)}),
                patch("dolilocaledit_launcher.registration._refresh_linux_desktop_database") as refresh,
            ):
                removed = unregister_linux_protocol()
            self.assertEqual(removed, desktop)
            self.assertFalse(desktop.exists())
            refresh.assert_called_once_with(data / "applications")


if __name__ == "__main__":
    unittest.main()
