"""Open documents through local-only editor choices without a shell."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Protocol

from .errors import EditorError


class ProcessLike(Protocol):
    def poll(self) -> int | None: ...


@dataclass(frozen=True)
class EditorHandle:
    process: ProcessLike | None
    reliable_exit: bool


@dataclass(frozen=True)
class EditorCandidate:
    label: str
    command: tuple[str, ...]


@dataclass(frozen=True)
class EditorChoice:
    command: tuple[str, ...] | None
    remember: bool


_WINDOWS_EDITOR_SPECS: dict[str, tuple[tuple[str, str, tuple[str, ...]], ...]] = {
    ".docx": (
        ("Microsoft Word", "WINWORD.EXE", ("{file}",)),
        ("LibreOffice Writer", "soffice.exe", ("--writer", "{file}")),
    ),
    ".odt": (
        ("LibreOffice Writer", "soffice.exe", ("--writer", "{file}")),
        ("Microsoft Word", "WINWORD.EXE", ("{file}",)),
    ),
    ".xlsx": (
        ("Microsoft Excel", "EXCEL.EXE", ("{file}",)),
        ("LibreOffice Calc", "soffice.exe", ("--calc", "{file}")),
    ),
    ".ods": (
        ("LibreOffice Calc", "soffice.exe", ("--calc", "{file}")),
        ("Microsoft Excel", "EXCEL.EXE", ("{file}",)),
    ),
    ".csv": (
        ("Microsoft Excel", "EXCEL.EXE", ("{file}",)),
        ("LibreOffice Calc", "soffice.exe", ("--calc", "{file}")),
    ),
    ".pptx": (
        ("Microsoft PowerPoint", "POWERPNT.EXE", ("{file}",)),
        ("LibreOffice Impress", "soffice.exe", ("--impress", "{file}")),
    ),
    ".odp": (
        ("LibreOffice Impress", "soffice.exe", ("--impress", "{file}")),
        ("Microsoft PowerPoint", "POWERPNT.EXE", ("{file}",)),
    ),
    ".pdf": (
        ("Adobe Acrobat", "Acrobat.exe", ("{file}",)),
        ("Adobe Acrobat Reader", "AcroRd32.exe", ("{file}",)),
        ("LibreOffice Draw", "soffice.exe", ("--draw", "{file}")),
        ("Microsoft Edge", "msedge.exe", ("{file}",)),
    ),
    ".txt": (
        ("Bloc-notes Windows", "notepad.exe", ("{file}",)),
        ("Microsoft Word", "WINWORD.EXE", ("{file}",)),
        ("LibreOffice Writer", "soffice.exe", ("--writer", "{file}")),
    ),
}

_LINUX_EDITOR_SPECS: dict[str, tuple[tuple[str, str, tuple[str, ...]], ...]] = {
    ".docx": (
        ("LibreOffice Writer", "libreoffice", ("--writer", "{file}")),
        ("ONLYOFFICE Desktop Editors", "desktopeditors", ("{file}",)),
        ("WPS Writer", "wps", ("{file}",)),
        ("Calligra Words", "calligrawords", ("{file}",)),
    ),
    ".odt": (
        ("LibreOffice Writer", "libreoffice", ("--writer", "{file}")),
        ("ONLYOFFICE Desktop Editors", "desktopeditors", ("{file}",)),
        ("Calligra Words", "calligrawords", ("{file}",)),
    ),
    ".xlsx": (
        ("LibreOffice Calc", "libreoffice", ("--calc", "{file}")),
        ("ONLYOFFICE Desktop Editors", "desktopeditors", ("{file}",)),
        ("WPS Spreadsheets", "et", ("{file}",)),
        ("Calligra Sheets", "calligrasheets", ("{file}",)),
    ),
    ".ods": (
        ("LibreOffice Calc", "libreoffice", ("--calc", "{file}")),
        ("ONLYOFFICE Desktop Editors", "desktopeditors", ("{file}",)),
        ("Calligra Sheets", "calligrasheets", ("{file}",)),
    ),
    ".csv": (
        ("LibreOffice Calc", "libreoffice", ("--calc", "{file}")),
        ("ONLYOFFICE Desktop Editors", "desktopeditors", ("{file}",)),
        ("WPS Spreadsheets", "et", ("{file}",)),
    ),
    ".pptx": (
        ("LibreOffice Impress", "libreoffice", ("--impress", "{file}")),
        ("ONLYOFFICE Desktop Editors", "desktopeditors", ("{file}",)),
        ("WPS Presentation", "wpp", ("{file}",)),
        ("Calligra Stage", "calligrastage", ("{file}",)),
    ),
    ".odp": (
        ("LibreOffice Impress", "libreoffice", ("--impress", "{file}")),
        ("ONLYOFFICE Desktop Editors", "desktopeditors", ("{file}",)),
        ("Calligra Stage", "calligrastage", ("{file}",)),
    ),
    ".pdf": (
        ("Xournal++", "xournalpp", ("{file}",)),
        ("Okular", "okular", ("{file}",)),
        ("Evince", "evince", ("{file}",)),
        ("LibreOffice Draw", "libreoffice", ("--draw", "{file}")),
        ("Master PDF Editor", "masterpdfeditor5", ("{file}",)),
    ),
    ".txt": (
        ("GNOME Text Editor", "gnome-text-editor", ("{file}",)),
        ("Gedit", "gedit", ("{file}",)),
        ("Kate", "kate", ("{file}",)),
        ("Xed", "xed", ("{file}",)),
        ("Mousepad", "mousepad", ("{file}",)),
        ("LibreOffice Writer", "libreoffice", ("--writer", "{file}")),
    ),
}


def choose_editor(document: Path) -> EditorChoice | None:
    extension = document.suffix.lower()
    if sys.platform == "win32":
        if extension not in _WINDOWS_EDITOR_SPECS:
            return None
        return _choose_windows_editor(discover_windows_editors(extension), extension)
    if sys.platform.startswith("linux"):
        if (
            extension not in _LINUX_EDITOR_SPECS
            or (not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"))
        ):
            return None
        return _choose_linux_editor(discover_linux_editors(extension), extension)
    return None


def discover_windows_editors(extension: str) -> tuple[EditorCandidate, ...]:
    candidates: list[EditorCandidate] = []
    seen: set[tuple[str, ...]] = set()
    for label, executable_name, arguments in _WINDOWS_EDITOR_SPECS.get(extension.lower(), ()):
        executable = _find_windows_executable(executable_name)
        if executable is None:
            continue
        command = (str(executable), *arguments)
        normalized = (str(executable).casefold(), *arguments)
        if normalized in seen:
            continue
        seen.add(normalized)
        candidates.append(EditorCandidate(label, command))
    return tuple(candidates)


def discover_linux_editors(extension: str) -> tuple[EditorCandidate, ...]:
    candidates: list[EditorCandidate] = []
    seen: set[tuple[str, ...]] = set()
    for label, executable_name, arguments in _LINUX_EDITOR_SPECS.get(extension.lower(), ()):
        executable = shutil.which(executable_name)
        if not executable:
            continue
        path = Path(executable).resolve()
        if not path.is_file() or not os.access(path, os.X_OK):
            continue
        command = (str(path), *arguments)
        if command in seen:
            continue
        seen.add(command)
        candidates.append(EditorCandidate(label, command))
    return tuple(candidates)


def _choose_linux_editor(candidates: tuple[EditorCandidate, ...], extension: str) -> EditorChoice | None:
    frontend = _linux_dialog_frontend()
    if frontend is None:
        return None
    kind, executable = frontend
    labels = [candidate.label for candidate in candidates]
    labels.extend(("Application Linux par défaut", "Choisir une autre application…"))
    if kind == "zenity":
        command = [
            str(executable),
            "--list",
            "--title=Doli Local Edit",
            f"--text=Avec quelle application ouvrir ce fichier {extension} ?",
            "--column=Identifiant",
            "--column=Application",
            "--hide-column=1",
            "--print-column=1",
            "--width=620",
            "--height=360",
        ]
        for index, label in enumerate(labels):
            command.extend((str(index), label))
    else:
        command = [
            str(executable),
            "--title",
            "Doli Local Edit",
            "--menu",
            f"Avec quelle application ouvrir ce fichier {extension} ?",
        ]
        for index, label in enumerate(labels):
            command.extend((str(index), label))
    selection = _run_linux_dialog(command, "editor_chooser_failed")
    if selection.returncode != 0:
        if selection.returncode == 1:
            raise EditorError("editor_selection_cancelled", "La sélection de l’application a été annulée.")
        raise EditorError("editor_chooser_failed", "Le sélecteur d’application Linux a échoué.")
    try:
        selected_index = int(selection.stdout.strip())
    except ValueError as exc:
        raise EditorError("editor_chooser_failed", "Le sélecteur d’application Linux a renvoyé un choix invalide.") from exc
    if selected_index < 0 or selected_index >= len(labels):
        raise EditorError("editor_chooser_failed", "Le sélecteur d’application Linux a renvoyé un choix invalide.")

    if selected_index == len(candidates):
        return EditorChoice(None, _confirm_linux_editor_choice(frontend, extension))
    if selected_index == len(candidates) + 1:
        selected = _browse_linux_editor(frontend)
        return EditorChoice(
            (str(selected), "{file}"),
            _confirm_linux_editor_choice(frontend, extension),
        )
    return EditorChoice(
        candidates[selected_index].command,
        _confirm_linux_editor_choice(frontend, extension),
    )


def _linux_dialog_frontend() -> tuple[str, Path] | None:
    for name in ("zenity", "kdialog"):
        executable = shutil.which(name)
        if executable:
            path = Path(executable).resolve()
            if path.is_file() and os.access(path, os.X_OK):
                return name, path
    return None


def _confirm_linux_editor_choice(frontend: tuple[str, Path], extension: str) -> bool:
    kind, executable = frontend
    if kind == "zenity":
        command = [
            str(executable),
            "--question",
            "--title=Doli Local Edit",
            f"--text=Toujours utiliser ce choix pour les fichiers {extension} ?",
            "--ok-label=Toujours utiliser",
            "--cancel-label=Demander à nouveau",
        ]
    else:
        command = [
            str(executable),
            "--title",
            "Doli Local Edit",
            "--yesno",
            f"Toujours utiliser ce choix pour les fichiers {extension} ?",
            "--yes-label",
            "Toujours utiliser",
            "--no-label",
            "Demander à nouveau",
        ]
    result = _run_linux_dialog(command, "editor_chooser_failed")
    if result.returncode not in (0, 1):
        raise EditorError("editor_chooser_failed", "La mémorisation du choix Linux a échoué.")
    return result.returncode == 0


def _browse_linux_editor(frontend: tuple[str, Path]) -> Path:
    kind, executable = frontend
    if kind == "zenity":
        command = [
            str(executable),
            "--file-selection",
            "--title=Choisir l’application pour Doli Local Edit",
            "--filename=/usr/bin/",
        ]
    else:
        command = [
            str(executable),
            "--title",
            "Doli Local Edit",
            "--getopenfilename",
            "/usr/bin/",
        ]
    result = _run_linux_dialog(command, "editor_browser_failed")
    if result.returncode != 0:
        if result.returncode == 1:
            raise EditorError("editor_selection_cancelled", "La sélection de l’application a été annulée.")
        raise EditorError("editor_browser_failed", "Le parcours des applications Linux a échoué.")
    selected = Path(result.stdout.strip())
    try:
        resolved = selected.resolve(strict=True)
    except OSError as exc:
        raise EditorError("editor_missing", "L’application Linux sélectionnée est introuvable.") from exc
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        raise EditorError("editor_missing", "Le fichier Linux sélectionné n’est pas exécutable.")
    return resolved


def _run_linux_dialog(command: list[str], error_code: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=300,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise EditorError(error_code, "L’interface de sélection Linux est indisponible.") from exc


def _find_windows_executable(executable_name: str) -> Path | None:
    try:
        import winreg

        access_modes = [winreg.KEY_READ]
        for flag_name in ("KEY_WOW64_64KEY", "KEY_WOW64_32KEY"):
            flag = getattr(winreg, flag_name, 0)
            if flag:
                access_modes.append(winreg.KEY_READ | flag)
        key_name = rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{executable_name}"
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            for access in access_modes:
                try:
                    with winreg.OpenKey(hive, key_name, 0, access) as key:
                        value, _kind = winreg.QueryValueEx(key, "")
                except OSError:
                    continue
                if isinstance(value, str):
                    candidate = Path(value.strip().strip('"'))
                    if candidate.is_absolute() and candidate.is_file():
                        return candidate.resolve()
    except (ImportError, OSError):
        pass

    program_files = tuple(
        Path(value) for name in ("ProgramFiles", "ProgramFiles(x86)")
        if (value := os.environ.get(name))
    )
    relative_candidates: dict[str, tuple[Path, ...]] = {
        "WINWORD.EXE": (Path("Microsoft Office/root/Office16/WINWORD.EXE"),),
        "EXCEL.EXE": (Path("Microsoft Office/root/Office16/EXCEL.EXE"),),
        "POWERPNT.EXE": (Path("Microsoft Office/root/Office16/POWERPNT.EXE"),),
        "soffice.exe": (Path("LibreOffice/program/soffice.exe"),),
        "Acrobat.exe": (Path("Adobe/Acrobat DC/Acrobat/Acrobat.exe"),),
        "AcroRd32.exe": (Path("Adobe/Acrobat DC/Acrobat/AcroRd32.exe"),),
        "msedge.exe": (Path("Microsoft/Edge/Application/msedge.exe"),),
    }
    for base in program_files:
        for relative in relative_candidates.get(executable_name, ()):
            candidate = base / relative
            if candidate.is_file():
                return candidate.resolve()
    if executable_name == "notepad.exe":
        windows = os.environ.get("WINDIR")
        candidate = Path(windows) / "System32" / executable_name if windows else None
        if candidate is not None and candidate.is_file():
            return candidate.resolve()
    return None


def _choose_windows_editor(candidates: tuple[EditorCandidate, ...], extension: str) -> EditorChoice:
    import ctypes
    from ctypes import wintypes

    window_proc_type = ctypes.WINFUNCTYPE(
        ctypes.c_ssize_t,
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
    )

    class WindowClass(ctypes.Structure):
        _fields_ = [
            ("style", wintypes.UINT),
            ("lpfnWndProc", window_proc_type),
            ("cbClsExtra", ctypes.c_int),
            ("cbWndExtra", ctypes.c_int),
            ("hInstance", wintypes.HINSTANCE),
            ("hIcon", wintypes.HICON),
            ("hCursor", wintypes.HANDLE),
            ("hbrBackground", wintypes.HBRUSH),
            ("lpszMenuName", wintypes.LPCWSTR),
            ("lpszClassName", wintypes.LPCWSTR),
        ]

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    gdi32 = ctypes.windll.gdi32
    kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    kernel32.GetModuleHandleW.restype = wintypes.HINSTANCE
    user32.LoadCursorW.argtypes = [wintypes.HINSTANCE, ctypes.c_void_p]
    user32.LoadCursorW.restype = wintypes.HANDLE
    user32.RegisterClassW.argtypes = [ctypes.POINTER(WindowClass)]
    user32.RegisterClassW.restype = wintypes.ATOM
    user32.UnregisterClassW.argtypes = [wintypes.LPCWSTR, wintypes.HINSTANCE]
    user32.UnregisterClassW.restype = wintypes.BOOL
    user32.DefWindowProcW.argtypes = [
        wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
    ]
    user32.DefWindowProcW.restype = ctypes.c_ssize_t
    user32.DestroyWindow.argtypes = [wintypes.HWND]
    user32.DestroyWindow.restype = wintypes.BOOL
    user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
    user32.GetMessageW.restype = wintypes.BOOL
    user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
    user32.TranslateMessage.restype = wintypes.BOOL
    user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
    user32.DispatchMessageW.restype = ctypes.c_ssize_t
    gdi32.GetStockObject.argtypes = [ctypes.c_int]
    gdi32.GetStockObject.restype = wintypes.HANDLE
    create_window = user32.CreateWindowExW
    create_window.argtypes = [
        wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, ctypes.c_void_p,
    ]
    create_window.restype = wintypes.HWND
    send_message = user32.SendMessageW
    send_message.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    send_message.restype = ctypes.c_ssize_t

    combo_id = 100
    remember_id = 101
    open_id = 1
    cancel_id = 2
    controls: dict[str, wintypes.HWND] = {}
    result: dict[str, int | bool] = {"confirmed": False, "selected": 0, "remember": False}

    @window_proc_type
    def window_proc(hwnd: wintypes.HWND, message: int, wparam: int, lparam: int) -> int:
        if message == 0x0111:  # WM_COMMAND
            control_id = int(wparam) & 0xFFFF
            if control_id == open_id:
                selected = int(send_message(controls["combo"], 0x0147, 0, 0))  # CB_GETCURSEL
                if selected >= 0:
                    result["selected"] = selected
                    result["remember"] = bool(send_message(controls["remember"], 0x00F0, 0, 0))
                    result["confirmed"] = True
                    user32.DestroyWindow(hwnd)
                return 0
            if control_id == cancel_id:
                user32.DestroyWindow(hwnd)
                return 0
        if message == 0x0010:  # WM_CLOSE
            user32.DestroyWindow(hwnd)
            return 0
        if message == 0x0002:  # WM_DESTROY
            user32.PostQuitMessage(0)
            return 0
        return int(user32.DefWindowProcW(hwnd, message, wparam, lparam))

    instance = kernel32.GetModuleHandleW(None)
    class_name = f"DoliLocalEditEditorChooser_{os.getpid()}"
    window_class = WindowClass()
    window_class.lpfnWndProc = window_proc
    window_class.hInstance = instance
    window_class.hCursor = user32.LoadCursorW(None, ctypes.c_void_p(32512))  # IDC_ARROW
    window_class.hbrBackground = ctypes.c_void_p(6)  # COLOR_WINDOW + 1
    window_class.lpszClassName = class_name
    try:
        if not user32.RegisterClassW(ctypes.byref(window_class)):
            raise OSError("RegisterClassW failed")
        screen_width = user32.GetSystemMetrics(0)
        screen_height = user32.GetSystemMetrics(1)
        width, height = 640, 270
        window = create_window(
            0,
            class_name,
            "Doli Local Edit",
            0x00C80000,
            max(0, (screen_width - width) // 2),
            max(0, (screen_height - height) // 2),
            width,
            height,
            None,
            None,
            instance,
            None,
        )
        if not window:
            raise OSError("CreateWindowExW failed")

        control_specs = (
            ("instruction", "STATIC", f"Avec quelle application ouvrir ce fichier {extension} ?", 0x50000000, 24, 20, 580, 28, 0),
            ("notice", "STATIC", "Ce choix reste sur ce poste et n’est jamais envoyé à Dolibarr.", 0x50000000, 24, 48, 580, 22, 0),
            ("combo", "COMBOBOX", "", 0x50210003, 24, 78, 580, 180, combo_id),
            ("remember", "BUTTON", f"Toujours utiliser ce choix pour les fichiers {extension}", 0x50010003, 24, 126, 580, 24, remember_id),
            ("open", "BUTTON", "Ouvrir", 0x50010001, 408, 174, 94, 32, open_id),
            ("cancel", "BUTTON", "Annuler", 0x50010000, 510, 174, 94, 32, cancel_id),
        )
        font = gdi32.GetStockObject(17)  # DEFAULT_GUI_FONT
        for name, kind, text, style, x, y, control_width, control_height, control_id in control_specs:
            control = create_window(
                0,
                kind,
                text,
                style,
                x,
                y,
                control_width,
                control_height,
                window,
                wintypes.HMENU(control_id),
                instance,
                None,
            )
            if not control:
                raise OSError("CreateWindowExW control failed")
            controls[name] = control
            send_message(control, 0x0030, int(font or 0), 1)  # WM_SETFONT

        labels = [candidate.label for candidate in candidates]
        labels.extend(("Application Windows par défaut", "Choisir une autre application…"))
        # A combobox copies each string synchronously, but the backing buffers
        # must still remain alive for the complete SendMessageW call. Keeping
        # them in this list also avoids transient c_wchar_p pointers on Windows.
        label_buffers = [ctypes.create_unicode_buffer(label) for label in labels]
        for label_buffer in label_buffers:
            send_message(controls["combo"], 0x0143, 0, ctypes.addressof(label_buffer))  # CB_ADDSTRING
        send_message(controls["combo"], 0x014E, 0, 0)  # CB_SETCURSEL
        user32.ShowWindow(window, 5)
        user32.SetForegroundWindow(window)
        user32.UpdateWindow(window)
        message = wintypes.MSG()
        while True:
            status = int(user32.GetMessageW(ctypes.byref(message), None, 0, 0))
            if status == 0:
                break
            if status < 0:
                raise OSError("GetMessageW failed")
            user32.TranslateMessage(ctypes.byref(message))
            user32.DispatchMessageW(ctypes.byref(message))
    except (AttributeError, OSError, ValueError) as exc:
        raise EditorError("editor_chooser_unavailable", "Le sélecteur d’application Windows est indisponible.") from exc
    finally:
        try:
            user32.UnregisterClassW(class_name, instance)
        except (AttributeError, OSError):
            pass

    if not result["confirmed"]:
        raise EditorError("editor_selection_cancelled", "La sélection de l’application a été annulée.")
    selected_index = int(result["selected"])
    remember = bool(result["remember"])
    if selected_index == len(candidates):
        return EditorChoice(None, remember)
    if selected_index == len(candidates) + 1:
        executable = _browse_windows_editor()
        return EditorChoice((str(executable), "{file}"), remember)
    if selected_index < 0 or selected_index >= len(candidates):
        raise EditorError("editor_selection_cancelled", "La sélection de l’application a été annulée.")
    return EditorChoice(candidates[selected_index].command, remember)


def _browse_windows_editor() -> Path:
    import ctypes
    from ctypes import wintypes

    class OpenFileName(ctypes.Structure):
        _fields_ = [
            ("lStructSize", wintypes.DWORD),
            ("hwndOwner", wintypes.HWND),
            ("hInstance", wintypes.HINSTANCE),
            ("lpstrFilter", wintypes.LPCWSTR),
            ("lpstrCustomFilter", wintypes.LPWSTR),
            ("nMaxCustFilter", wintypes.DWORD),
            ("nFilterIndex", wintypes.DWORD),
            ("lpstrFile", wintypes.LPWSTR),
            ("nMaxFile", wintypes.DWORD),
            ("lpstrFileTitle", wintypes.LPWSTR),
            ("nMaxFileTitle", wintypes.DWORD),
            ("lpstrInitialDir", wintypes.LPCWSTR),
            ("lpstrTitle", wintypes.LPCWSTR),
            ("Flags", wintypes.DWORD),
            ("nFileOffset", wintypes.WORD),
            ("nFileExtension", wintypes.WORD),
            ("lpstrDefExt", wintypes.LPCWSTR),
            ("lCustData", ctypes.c_ssize_t),
            ("lpfnHook", ctypes.c_void_p),
            ("lpTemplateName", wintypes.LPCWSTR),
            ("pvReserved", ctypes.c_void_p),
            ("dwReserved", wintypes.DWORD),
            ("FlagsEx", wintypes.DWORD),
        ]

    buffer = ctypes.create_unicode_buffer(32_768)
    dialog = OpenFileName()
    dialog.lStructSize = ctypes.sizeof(OpenFileName)
    dialog.lpstrFilter = "Applications Windows (*.exe)\0*.exe\0\0"
    dialog.nFilterIndex = 1
    dialog.lpstrFile = ctypes.cast(buffer, wintypes.LPWSTR)
    dialog.nMaxFile = len(buffer)
    dialog.lpstrTitle = "Choisir l’application pour Doli Local Edit"
    dialog.lpstrDefExt = "exe"
    dialog.Flags = 0x00000008 | 0x00000800 | 0x00001000 | 0x02000000
    try:
        get_open_file = ctypes.windll.comdlg32.GetOpenFileNameW
        get_open_file.argtypes = [ctypes.POINTER(OpenFileName)]
        get_open_file.restype = wintypes.BOOL
        selected = bool(get_open_file(ctypes.byref(dialog)))
        extended_error = int(ctypes.windll.comdlg32.CommDlgExtendedError())
    except (AttributeError, OSError) as exc:
        raise EditorError("editor_browser_unavailable", "Le parcours des applications Windows est indisponible.") from exc
    if not selected:
        if extended_error == 0:
            raise EditorError("editor_selection_cancelled", "La sélection de l’application a été annulée.")
        raise EditorError("editor_browser_failed", "Le parcours des applications Windows a échoué.")
    executable = Path(buffer.value)
    if not executable.is_absolute() or not executable.is_file() or executable.suffix.lower() != ".exe":
        raise EditorError("editor_missing", "L’application Windows sélectionnée est invalide.")
    return executable.resolve()


def open_editor(document: Path, configured_command: tuple[str, ...] | None) -> EditorHandle:
    if configured_command is not None:
        executable = Path(configured_command[0])
        if not executable.is_absolute() or not executable.is_file():
            raise EditorError("editor_missing", "L’éditeur configuré est introuvable.")
        arguments = [argument.replace("{file}", str(document)) for argument in configured_command]
        if not any("{file}" in argument for argument in configured_command):
            arguments.append(str(document))
        return EditorHandle(_spawn(arguments), True)

    if sys.platform == "win32":
        try:
            os.startfile(str(document))  # type: ignore[attr-defined]
        except OSError as exc:
            raise EditorError("editor_open_failed", "L’association de fichiers Windows a échoué.") from exc
        return EditorHandle(None, False)
    if sys.platform == "darwin":
        return EditorHandle(_spawn(["/usr/bin/open", str(document)]), False)
    opener = shutil.which("xdg-open")
    if not opener:
        raise EditorError("editor_missing", "Aucune association de fichiers système n’est disponible.")
    return EditorHandle(_spawn([str(Path(opener).resolve()), str(document)]), False)


def _spawn(arguments: list[str]) -> subprocess.Popen[bytes]:
    try:
        return subprocess.Popen(
            arguments,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            shell=False,
            close_fds=True,
        )
    except OSError as exc:
        raise EditorError("editor_open_failed", "L’ouverture de l’éditeur local a échoué.") from exc
