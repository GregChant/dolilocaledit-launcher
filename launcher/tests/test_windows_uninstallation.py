import base64
from contextlib import nullcontext
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from dolilocaledit_launcher.errors import ConfigurationError
from dolilocaledit_launcher.installation import (
    RunningLauncher,
    _schedule_windows_uninstall_cleanup,
    uninstall_for_current_user,
)
from dolilocaledit_launcher.registration import (
    register_windows_installation,
    unregister_windows_installation,
)


PROTOCOL = r"Software\Classes\dolilocaledit"
UNINSTALL = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\DoliLocalEdit"


class RegistryKey:
    def __init__(self, path: str) -> None:
        self.path = path

    def __enter__(self):
        return self

    def __exit__(self, *args) -> None:
        return None


class CurrentUserRegistry:
    """Synthetic registry that refuses any operation outside HKEY_CURRENT_USER."""

    HKEY_CURRENT_USER = object()
    REG_SZ = 1
    REG_DWORD = 4
    KEY_READ = 1
    KEY_SET_VALUE = 2

    def __init__(self) -> None:
        self.keys = {}
        self.fail_value = None
        for path in (r"Software\Classes", r"Software\Microsoft\Windows\CurrentVersion\Uninstall"):
            self.CreateKey(self.HKEY_CURRENT_USER, path)

    def _check_root(self, root) -> None:
        if root is not self.HKEY_CURRENT_USER:
            raise AssertionError("A registration operation requested administrator scope")

    def CreateKey(self, root, path):
        self._check_root(root)
        parts = path.split("\\")
        for length in range(1, len(parts) + 1):
            self.keys.setdefault("\\".join(parts[:length]), {})
        return RegistryKey(path)

    def OpenKey(self, root, path, reserved=0, access=0):
        self._check_root(root)
        if path not in self.keys:
            raise FileNotFoundError(path)
        return RegistryKey(path)

    def QueryValueEx(self, key, name):
        try:
            return self.keys[key.path][name or ""]
        except KeyError as exc:
            raise FileNotFoundError(name) from exc

    def SetValueEx(self, key, name, reserved, kind, value) -> None:
        if name == self.fail_value:
            self.fail_value = None
            raise PermissionError("Synthetic registry write failure")
        self.keys[key.path][name or ""] = (value, kind)

    def DeleteValue(self, key, name) -> None:
        try:
            del self.keys[key.path][name or ""]
        except KeyError as exc:
            raise FileNotFoundError(name) from exc

    def EnumKey(self, key, index):
        children = sorted(path.rsplit("\\", 1)[1] for path in self.keys if path.rsplit("\\", 1)[0] == key.path)
        if index >= len(children):
            error = OSError("No more registry keys")
            error.winerror = 259
            raise error
        return children[index]

    def DeleteKey(self, root, path) -> None:
        self._check_root(root)
        if path not in self.keys:
            raise FileNotFoundError(path)
        if any(item.startswith(path + "\\") for item in self.keys):
            raise PermissionError("Registry key is not empty")
        del self.keys[path]


class WindowsRegistrationTest(unittest.TestCase):
    def _installed(self, root: Path) -> Path:
        target = root / "User with spaces" / "Programs" / "DoliLocalEdit" / "dolilocaledit-launcher.exe"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"installed launcher")
        return target

    def test_install_is_visible_per_user_with_quoted_uninstall_commands(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = self._installed(Path(temporary))
            registry = CurrentUserRegistry()
            with (
                patch.dict("sys.modules", {"winreg": registry}),
                patch.dict("os.environ", {"LOCALAPPDATA": str(target.parent.parent.parent)}),
            ):
                register_windows_installation(target, "1.1.1")
            values = registry.keys[UNINSTALL]
            self.assertEqual(values["DisplayName"], ("Doli Local Edit", registry.REG_SZ))
            self.assertEqual(values["DisplayVersion"], ("1.1.1", registry.REG_SZ))
            self.assertEqual(values["Publisher"], ("Experts Conseils Chanton", registry.REG_SZ))
            self.assertEqual(values["InstallLocation"][0], str(target.parent))
            self.assertEqual(values["UninstallString"][0], f'"{target}" uninstall')
            self.assertEqual(values["QuietUninstallString"][0], f'"{target}" uninstall --quiet')
            self.assertEqual(values["NoModify"], (1, registry.REG_DWORD))
            self.assertEqual(values["NoRepair"], (1, registry.REG_DWORD))
            self.assertEqual(registry.keys[PROTOCOL + r"\shell\open\command"][""][0], f'"{target}" open "%1"')

    def test_failed_upgrade_restores_protocol_and_previous_apps_entry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = self._installed(Path(temporary))
            registry = CurrentUserRegistry()
            with (
                patch.dict("sys.modules", {"winreg": registry}),
                patch.dict("os.environ", {"LOCALAPPDATA": str(target.parent.parent.parent)}),
            ):
                register_windows_installation(target, "1.1.0")
                registry.keys[UNINSTALL]["UnrelatedValue"] = ("preserved", registry.REG_SZ)
                before = deepcopy(registry.keys)
                registry.fail_value = "DisplayVersion"
                with self.assertRaises(ConfigurationError) as error:
                    register_windows_installation(target, "1.1.1")
            self.assertEqual(error.exception.code, "registration_failed")
            self.assertEqual(registry.keys, before)

    def test_failed_first_install_leaves_no_partial_protocol_or_apps_entry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = self._installed(Path(temporary))
            registry = CurrentUserRegistry()
            before = deepcopy(registry.keys)
            registry.fail_value = "DisplayVersion"
            with (
                patch.dict("sys.modules", {"winreg": registry}),
                patch.dict("os.environ", {"LOCALAPPDATA": str(target.parent.parent.parent)}),
            ):
                with self.assertRaises(ConfigurationError):
                    register_windows_installation(target, "1.1.1")
            self.assertEqual(registry.keys, before)

    def test_uninstall_preserves_a_foreign_protocol_handler(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = self._installed(Path(temporary))
            registry = CurrentUserRegistry()
            with (
                patch.dict("sys.modules", {"winreg": registry}),
                patch.dict("os.environ", {"LOCALAPPDATA": str(target.parent.parent.parent)}),
            ):
                register_windows_installation(target, "1.1.1")
                registry.keys[PROTOCOL + r"\shell\open\command"][""] = ('"C:\\Other App\\handler.exe" "%1"', registry.REG_SZ)
                before = deepcopy(registry.keys[PROTOCOL + r"\shell\open\command"])
                unregister_windows_installation()
            self.assertNotIn(UNINSTALL, registry.keys)
            self.assertEqual(registry.keys[PROTOCOL + r"\shell\open\command"], before)

    def test_uninstall_removes_only_its_owned_protocol_and_apps_keys(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = self._installed(Path(temporary))
            registry = CurrentUserRegistry()
            with (
                patch.dict("sys.modules", {"winreg": registry}),
                patch.dict("os.environ", {"LOCALAPPDATA": str(target.parent.parent.parent)}),
            ):
                register_windows_installation(target, "1.1.1")
                registry.CreateKey(registry.HKEY_CURRENT_USER, PROTOCOL + "-foreign")
                unregister_windows_installation()
                unregister_windows_installation()
            self.assertNotIn(PROTOCOL, registry.keys)
            self.assertNotIn(UNINSTALL, registry.keys)
            self.assertIn(PROTOCOL + "-foreign", registry.keys)

    def test_uninstall_does_not_delete_a_foreign_apps_entry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = self._installed(Path(temporary))
            registry = CurrentUserRegistry()
            with (
                patch.dict("sys.modules", {"winreg": registry}),
                patch.dict("os.environ", {"LOCALAPPDATA": str(target.parent.parent.parent)}),
            ):
                register_windows_installation(target, "1.1.1")
                registry.keys[UNINSTALL]["UninstallString"] = ('"C:\\Other App\\uninstall.exe"', registry.REG_SZ)
                before = deepcopy(registry.keys[UNINSTALL])
                unregister_windows_installation()
            self.assertEqual(registry.keys[UNINSTALL], before)


class WindowsUninstallationTest(unittest.TestCase):
    def setUp(self) -> None:
        mutex = patch("dolilocaledit_launcher.installation._windows_installation_lock", side_effect=lambda: nullcontext())
        mutex.start()
        self.addCleanup(mutex.stop)

    def test_uninstall_removes_known_programs_and_preserves_all_user_data(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = root / "local" / "Programs" / "DoliLocalEdit"
            directory.mkdir(parents=True)
            old = directory / "dolilocaledit-launcher.exe"
            old.write_bytes(b"old")
            current = directory / f"dolilocaledit-launcher-1.1.1-{'a' * 64}.exe"
            current.write_bytes(b"current")
            recovery = directory / "recovery" / "document.txt"
            recovery.parent.mkdir()
            recovery.write_bytes(b"unpublished document")
            config = directory / "config.json"
            config.write_bytes(b"private settings")
            unrelated = directory / "customer-tool.exe"
            unrelated.write_bytes(b"unrelated")
            with (
                patch("dolilocaledit_launcher.installation.sys.platform", "win32"),
                patch.dict("os.environ", {"LOCALAPPDATA": str(root / "local")}),
                patch("dolilocaledit_launcher.installation.unregister_windows_installation") as unregister,
                patch("dolilocaledit_launcher.installation.find_running_windows_launchers", return_value=()),
            ):
                result = uninstall_for_current_user()
            self.assertEqual(result.path, directory)
            self.assertEqual(result.deferred_files, ())
            self.assertFalse(old.exists())
            self.assertFalse(current.exists())
            self.assertEqual(recovery.read_bytes(), b"unpublished document")
            self.assertEqual(config.read_bytes(), b"private settings")
            self.assertEqual(unrelated.read_bytes(), b"unrelated")
            unregister.assert_called_once_with()

    def test_active_sessions_are_not_stopped_and_only_their_files_are_deferred(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = root / "local" / "Programs" / "DoliLocalEdit"
            directory.mkdir(parents=True)
            busy = directory / "dolilocaledit-launcher.exe"
            busy.write_bytes(b"busy")
            idle = directory / f"dolilocaledit-launcher-1.1.1-{'b' * 64}.exe"
            idle.write_bytes(b"idle")
            instances = (RunningLauncher(1234, busy),)
            with (
                patch("dolilocaledit_launcher.installation.sys.platform", "win32"),
                patch.dict("os.environ", {"LOCALAPPDATA": str(root / "local")}),
                patch("dolilocaledit_launcher.installation.unregister_windows_installation"),
                patch("dolilocaledit_launcher.installation.find_running_windows_launchers", return_value=instances),
                patch("dolilocaledit_launcher.installation._schedule_windows_uninstall_cleanup") as schedule,
            ):
                result = uninstall_for_current_user()
            self.assertEqual(result.deferred_files, (busy,))
            self.assertEqual(busy.read_bytes(), b"busy")
            self.assertFalse(idle.exists())
            schedule.assert_called_once_with(directory, (busy,), instances)

    def test_uninstall_refuses_a_redirected_install_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = root / "local" / "Programs" / "DoliLocalEdit"
            directory.parent.mkdir(parents=True)
            external = root / "customer-data"
            external.mkdir()
            document = external / "dolilocaledit-launcher.exe"
            document.write_bytes(b"preserve")
            directory.symlink_to(external, target_is_directory=True)
            with (
                patch("dolilocaledit_launcher.installation.sys.platform", "win32"),
                patch.dict("os.environ", {"LOCALAPPDATA": str(root / "local")}),
                patch("dolilocaledit_launcher.installation.unregister_windows_installation") as unregister,
            ):
                with self.assertRaises(ConfigurationError):
                    uninstall_for_current_user()
            self.assertEqual(document.read_bytes(), b"preserve")
            unregister.assert_not_called()

    def test_deferred_cleanup_uses_fixed_code_and_private_json_pipe(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dle space & quote '") as temporary:
            root = Path(temporary)
            directory = root / "local" / "Programs" / "DoliLocalEdit"
            directory.mkdir(parents=True)
            target = directory / "dolilocaledit-launcher.exe"
            target.write_bytes(b"current")
            process = MagicMock()
            with (
                patch.dict("os.environ", {"LOCALAPPDATA": str(root / "local")}),
                patch("dolilocaledit_launcher.installation._windows_powershell_path", return_value=Path("system-powershell.exe")),
                patch("dolilocaledit_launcher.installation.subprocess.Popen", return_value=process) as popen,
                patch("dolilocaledit_launcher.installation.threading.Thread"),
            ):
                _schedule_windows_uninstall_cleanup(directory, (target,), (RunningLauncher(1234, target),))
            arguments = popen.call_args.args[0]
            self.assertEqual(arguments[:4], ["system-powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand"])
            program = base64.b64decode(arguments[4]).decode("utf-16-le")
            self.assertNotIn(str(directory), program)
            self.assertFalse(popen.call_args.kwargs["shell"])
            payload = json.loads(process.stdin.write.call_args.args[0])
            self.assertEqual(payload["directory"], str(directory))
            self.assertEqual(payload["files"], [{"path": str(target), "sha256": hashlib.sha256(b"current").hexdigest(), "pids": [1234]}])
            self.assertTrue(payload["mutex"].startswith("Global\\DoliLocalEdit-Install-"))
            process.stdin.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
