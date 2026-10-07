"""Nonblocking local completion control for editors with no closure observer."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import threading
from typing import Protocol

from .approval import _safe_dialog_value, _which_absolute


class FinishControl(Protocol):
    def requested(self) -> bool: ...
    def close(self) -> None: ...


def finish_message(document: Path) -> str:
    """Describe only the local filename and the explicit completion precondition."""
    filename = _safe_dialog_value(document.name, 255) or "document local"
    return (
        f"Document : {filename}\n\n"
        "La fermeture de ce document ne peut pas être détectée automatiquement avec cet éditeur. "
        "Le verrou Dolibarr reste actif pendant l’édition.\n\n"
        "Enregistrez le document, puis fermez-le dans l’éditeur. "
        "Choisissez « Terminer l’édition » uniquement après ces deux opérations. "
        "La dernière version enregistrée sera alors envoyée à Dolibarr."
    )


def create_finish_control(document: Path) -> FinishControl:
    return LocalFinishControl(finish_message(document))


class LocalFinishControl:
    """Keep all dialogs outside the lease/heartbeat loop and credentials out of them."""

    def __init__(self, message: str) -> None:
        self.message = message
        self._requested = threading.Event()
        self._stopping = threading.Event()
        self._process: subprocess.Popen[bytes] | None = None
        self._window: int | None = None
        self._thread = threading.Thread(target=self._run, name="local-edit-finish", daemon=True)
        self._thread.start()

    def requested(self) -> bool:
        return self._requested.is_set()

    def close(self) -> None:
        self._stopping.set()
        process = self._process
        if process is not None and process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=1)
            except (OSError, subprocess.TimeoutExpired):
                pass
        window = self._window
        if sys.platform == "win32" and window is not None:
            import ctypes
            from ctypes import wintypes

            post = ctypes.windll.user32.PostMessageW
            post.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
            post.restype = wintypes.BOOL
            post(window, 0x0010, 0, 0)  # WM_CLOSE

    def _run(self) -> None:
        try:
            if sys.platform == "win32":
                self._run_windows()
            elif sys.platform.startswith("linux"):
                self._run_linux()
        except (AttributeError, OSError, ValueError):
            # Lack of a desktop control never implies that a document was closed.
            return

    def _run_linux(self) -> None:
        if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
            return
        zenity = _which_absolute("zenity")
        kdialog = _which_absolute("kdialog") if zenity is None else None
        if zenity is not None:
            command = [
                str(zenity), "--question", "--title=Doli Local Edit", "--text=" + self.message,
                "--ok-label=Terminer l’édition", "--cancel-label=Continuer l’édition",
            ]
        elif kdialog is not None:
            command = [
                str(kdialog), "--title", "Doli Local Edit", "--yesno", self.message,
                "--yes-label", "Terminer l’édition", "--no-label", "Continuer l’édition",
            ]
        else:
            return
        while not self._stopping.is_set():
            self._process = subprocess.Popen(
                command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, shell=False, close_fds=True,
            )
            if self._stopping.is_set():
                self._process.terminate()
            result = self._process.wait()
            self._process = None
            if result == 0 and not self._stopping.is_set():
                self._requested.set()
                return
            # Continuing is not completion. Reoffer the control while the lease remains active.
            if self._stopping.wait(60):
                return

    def _run_windows(self) -> None:
        import ctypes
        from ctypes import wintypes

        callback_type = ctypes.WINFUNCTYPE(
            ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
        )

        class WindowClass(ctypes.Structure):
            _fields_ = [
                ("style", wintypes.UINT), ("lpfnWndProc", callback_type),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR),
            ]

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        kernel32.GetModuleHandleW.restype = wintypes.HINSTANCE
        user32.RegisterClassW.argtypes = [ctypes.POINTER(WindowClass)]
        user32.RegisterClassW.restype = wintypes.ATOM
        user32.UnregisterClassW.argtypes = [wintypes.LPCWSTR, wintypes.HINSTANCE]
        user32.UnregisterClassW.restype = wintypes.BOOL
        user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        user32.DefWindowProcW.restype = ctypes.c_ssize_t
        user32.DestroyWindow.argtypes = [wintypes.HWND]
        user32.DestroyWindow.restype = wintypes.BOOL
        user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.ShowWindow.restype = wintypes.BOOL
        user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
        user32.GetMessageW.restype = ctypes.c_int
        user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
        user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
        user32.DispatchMessageW.restype = ctypes.c_ssize_t
        create = user32.CreateWindowExW
        create.argtypes = [
            wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, ctypes.c_void_p,
        ]
        create.restype = wintypes.HWND
        send = user32.SendMessageW
        send.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        send.restype = ctypes.c_ssize_t
        controls: dict[str, int] = {}

        @callback_type
        def window_proc(hwnd: int, message: int, wparam: int, lparam: int) -> int:
            if message == 0x0111 and int(wparam) & 0xFFFF == 1:  # WM_COMMAND
                if send(controls["confirmed"], 0x00F0, 0, 0) == 1 and not self._stopping.is_set():
                    self._requested.set()
                    user32.DestroyWindow(hwnd)
                return 0
            if message == 0x0010:  # Closing the control never releases a document.
                if self._stopping.is_set():
                    user32.DestroyWindow(hwnd)
                else:
                    user32.ShowWindow(hwnd, 6)  # SW_MINIMIZE: keep a taskbar entry.
                return 0
            if message == 0x0002:
                user32.PostQuitMessage(0)
                return 0
            return int(user32.DefWindowProcW(hwnd, message, wparam, lparam))

        instance = kernel32.GetModuleHandleW(None)
        class_name = f"DoliLocalEditFinish_{os.getpid()}_{threading.get_ident()}"
        window_class = WindowClass()
        window_class.lpfnWndProc = window_proc
        window_class.hInstance = instance
        window_class.hbrBackground = ctypes.c_void_p(6)
        window_class.lpszClassName = class_name
        window: int | None = None
        try:
            if not user32.RegisterClassW(ctypes.byref(window_class)):
                raise OSError("RegisterClassW failed")
            window = create(
                0, class_name, "Doli Local Edit — Terminer l’édition", 0x00CA0000,
                160, 160, 650, 320, None, None, instance, None,
            )
            if not window:
                raise OSError("CreateWindowExW failed")
            specs = (
                ("message", "STATIC", self.message, 0x50000000, 20, 18, 600, 175, 0),
                ("confirmed", "BUTTON", "J’ai enregistré et fermé ce document dans l’éditeur.", 0x50010003, 20, 204, 600, 26, 2),
                ("finish", "BUTTON", "Terminer l’édition", 0x50010000, 400, 244, 220, 30, 1),
            )
            for name, kind, content, style, x, y, width, height, control_id in specs:
                control = create(
                    0, kind, content, style, x, y, width, height,
                    window, wintypes.HMENU(control_id), instance, None,
                )
                if not control:
                    raise OSError("CreateWindowExW control failed")
                controls[name] = control
            self._window = window
            if self._stopping.is_set():
                return
            user32.ShowWindow(window, 5)
            message = wintypes.MSG()
            while True:
                result = int(user32.GetMessageW(ctypes.byref(message), None, 0, 0))
                if result == 0:
                    break
                if result < 0:
                    raise OSError("GetMessageW failed")
                user32.TranslateMessage(ctypes.byref(message))
                user32.DispatchMessageW(ctypes.byref(message))
        finally:
            self._window = None
            if window is not None:
                user32.DestroyWindow(window)
            user32.UnregisterClassW(class_name, instance)
