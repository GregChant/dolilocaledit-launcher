"""Observe one local document and the exact native process that owns it."""

from __future__ import annotations

from enum import Enum
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import threading
import time
from typing import Callable, Protocol


class DocumentState(Enum):
    OPEN = "open"
    CLOSED = "closed"
    UNKNOWN = "unknown"


class DocumentMonitor(Protocol):
    def poll(self) -> DocumentState: ...
    def close(self) -> None: ...


class UnknownDocumentMonitor:
    """Require an explicit local finish when no qualified observation is available."""

    def poll(self) -> DocumentState:
        return DocumentState.UNKNOWN

    def close(self) -> None:
        pass


class LibreOfficeDocumentMonitor:
    """Observe a qualified LibreOffice owner's file, allowing atomic-save gaps.

    This adapter is selected only for an explicit local LibreOffice executable.
    A missing owner file before a successful OPEN observation never means CLOSED.
    An unreadable, malformed or replaced-by-symlink owner file means UNKNOWN.
    """

    def __init__(
        self,
        document: Path,
        monotonic: Callable[[], float] = time.monotonic,
        close_grace_seconds: float = 3.0,
    ) -> None:
        self.document = document
        self.owner_file = document.with_name(".~lock." + document.name + "#")
        self.monotonic = monotonic
        self.close_grace_seconds = close_grace_seconds
        try:
            self._initial_owner_signature = _regular_signature(self.owner_file)
        except (OSError, ValueError):
            self._initial_owner_signature = None
        self._observed_open = False
        self._missing_since: float | None = None
        self._document_signature: tuple[int, int, int] | None = None

    def poll(self) -> DocumentState:
        try:
            owner_signature = _regular_signature(self.owner_file)
            if owner_signature is not None:
                self._missing_since = None
                self._document_signature = None
                if (
                    not self._valid_owner_file()
                    or (
                        not self._observed_open
                        and owner_signature == self._initial_owner_signature
                    )
                ):
                    return DocumentState.UNKNOWN
                self._observed_open = True
                return DocumentState.OPEN
            if not self._observed_open:
                return DocumentState.UNKNOWN
            document_signature = _regular_signature(self.document)
        except (OSError, ValueError):
            self._missing_since = None
            self._document_signature = None
            return DocumentState.UNKNOWN
        if document_signature is None:
            self._missing_since = None
            self._document_signature = None
            return DocumentState.UNKNOWN
        now = self.monotonic()
        if self._missing_since is None or self._document_signature != document_signature:
            self._missing_since = now
            self._document_signature = document_signature
            return DocumentState.UNKNOWN
        if now - self._missing_since < self.close_grace_seconds:
            return DocumentState.UNKNOWN
        return DocumentState.CLOSED

    def _valid_owner_file(self) -> bool:
        # LibreOffice uses five comma-separated fields ending with a semicolon.
        # The contents remain local and are never logged or sent to Dolibarr.
        with self.owner_file.open("rb") as stream:
            contents = stream.read(8193)
        if not contents or len(contents) > 8192 or not contents.rstrip().endswith(b";"):
            return False
        return len(contents.rstrip()[:-1].split(b",")) >= 5

    def close(self) -> None:
        pass


class WindowsOfficeDocumentMonitor:
    """Read document-specific COM observations from a local, secret-free helper."""

    def __init__(
        self,
        document: Path,
        applications: tuple[str, ...],
        helper_factory: Callable[[Path, tuple[str, ...]], subprocess.Popen[str] | None] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        freshness_seconds: float = 1.0,
    ) -> None:
        self._lock = threading.Lock()
        self._state = DocumentState.UNKNOWN
        self._observed_open = False
        self._stopped = False
        self._monotonic = monotonic
        self._freshness_seconds = freshness_seconds
        self._last_observation: float | None = None
        self._helper = (helper_factory or _start_office_helper)(document, applications)
        self._reader: threading.Thread | None = None
        if self._helper is not None and self._helper.stdout is not None:
            self._reader = threading.Thread(target=self._read_states, daemon=True)
            self._reader.start()

    def _read_states(self) -> None:
        assert self._helper is not None and self._helper.stdout is not None
        try:
            for line in self._helper.stdout:
                self._accept_state(line.strip())
        except (OSError, UnicodeError, ValueError):
            pass
        finally:
            with self._lock:
                self._state = DocumentState.UNKNOWN

    def _accept_state(self, value: str) -> None:
        with self._lock:
            if self._stopped:
                return
            self._last_observation = self._monotonic()
            if value == "OPEN":
                self._observed_open = True
                self._state = DocumentState.OPEN
            elif value == "CLOSED" and self._observed_open:
                self._state = DocumentState.CLOSED
            else:
                self._state = DocumentState.UNKNOWN

    def poll(self) -> DocumentState:
        with self._lock:
            if (
                self._last_observation is None
                or self._monotonic() - self._last_observation > self._freshness_seconds
            ):
                return DocumentState.UNKNOWN
            return self._state

    def close(self) -> None:
        with self._lock:
            if self._stopped:
                return
            self._stopped = True
        helper = self._helper
        if helper is None:
            return
        try:
            if helper.poll() is None:
                helper.terminate()
            helper.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            try:
                helper.kill()
                helper.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                pass
        if self._reader is not None:
            self._reader.join(timeout=1)
        if helper.stdout is not None and (self._reader is None or not self._reader.is_alive()):
            try:
                helper.stdout.close()
            except (OSError, ValueError):
                pass


_LIBREOFFICE_EXECUTABLES = {
    "libreoffice", "libreoffice.exe", "soffice", "soffice.exe",
    "swriter", "swriter.exe", "scalc", "scalc.exe", "simpress", "simpress.exe",
    "sdraw", "sdraw.exe",
}

_OFFICE_APPLICATIONS = {
    "winword.exe": ("Word.Application",),
    "excel.exe": ("Excel.Application",),
    "powerpnt.exe": ("PowerPoint.Application",),
}


def create_document_monitor(
    document: Path,
    configured_command: tuple[str, ...] | None = None,
) -> DocumentMonitor:
    """Prepare observation before opening an editor chosen entirely on this machine."""
    executable = Path(configured_command[0]).name.casefold() if configured_command else None
    if executable in _LIBREOFFICE_EXECUTABLES:
        return LibreOfficeDocumentMonitor(document)
    if sys.platform == "win32":
        applications = _OFFICE_APPLICATIONS.get(executable or "")
        if applications is None and configured_command is None:
            # A system association is not assumed to be Office. The helper must
            # observe this exact private document in a running Office instance.
            applications = ("Word.Application", "Excel.Application", "PowerPoint.Application")
        if applications is not None:
            return WindowsOfficeDocumentMonitor(document, applications)
    return UnknownDocumentMonitor()


def _regular_signature(path: Path) -> tuple[int, int, int] | None:
    try:
        details = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(details.st_mode) or stat.S_ISLNK(details.st_mode):
        raise ValueError("A document monitor only accepts regular local files.")
    return details.st_ino, details.st_size, details.st_mtime_ns


def _start_office_helper(document: Path, applications: tuple[str, ...]) -> subprocess.Popen[str] | None:
    system_root = os.environ.get("SystemRoot", r"C:\Windows")
    powershell = Path(system_root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    if not powershell.is_file():
        return None
    process: subprocess.Popen[str] | None = None
    try:
        process = subprocess.Popen(
            [str(powershell), "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", _OFFICE_MONITOR_SCRIPT],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            bufsize=1,
            shell=False,
            close_fds=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        assert process.stdin is not None
        process.stdin.write(json.dumps({"path": str(document), "applications": applications}, ensure_ascii=True) + "\n")
        process.stdin.close()
        return process
    except (OSError, ValueError):
        if process is not None:
            try:
                process.terminate()
            except OSError:
                pass
        return None


# PowerShell is part of the supported Windows installation. COM objects are
# attached only: the helper never starts Office, opens a document, invokes a
# macro, closes an application, or exposes an automation endpoint. The sole
# input is a local private path; no ticket, Bearer, URI or document bytes enter
# this process. It emits exactly one of three constant state words.
_OFFICE_MONITOR_NATIVE_SUPPORT = r'''
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;
using Microsoft.Win32.SafeHandles;
public sealed class DoliLocalEditOwnerProcess : IDisposable {
    private SafeWaitHandle handle;
    private DoliLocalEditOwnerProcess(IntPtr value) {
        handle = new SafeWaitHandle(value, true);
    }
    [DllImport("user32.dll")]
    private static extern uint GetWindowThreadProcessId(IntPtr window, out uint processId);
    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern IntPtr OpenProcess(uint access, bool inherit, uint processId);
    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern uint WaitForSingleObject(SafeWaitHandle process, uint milliseconds);
    public static DoliLocalEditOwnerProcess Capture(IntPtr window) {
        if (window == IntPtr.Zero) return null;
        uint processId;
        if (GetWindowThreadProcessId(window, out processId) == 0 || processId == 0) return null;
        // SYNCHRONIZE alone can prove exit; no termination or memory access is requested.
        IntPtr value = OpenProcess(0x00100000, false, processId);
        if (value == IntPtr.Zero) return null;
        DoliLocalEditOwnerProcess owner = new DoliLocalEditOwnerProcess(value);
        uint verifiedProcessId;
        if (GetWindowThreadProcessId(window, out verifiedProcessId) == 0
            || verifiedProcessId != processId) {
            owner.Dispose();
            return null;
        }
        return owner;
    }
    public int Observe() {
        // A retained kernel handle identifies the original process even if its PID is reused.
        // 1 proves exit, 0 proves it is still alive, -1 leaves ownership uncertain.
        try {
            if (handle == null || handle.IsClosed || handle.IsInvalid) return -1;
            uint result = WaitForSingleObject(handle, 0);
            if (result == 0) return 1;
            if (result == 258) return 0;
        } catch { }
        return -1;
    }
    public void Dispose() {
        if (handle != null) handle.Dispose();
    }
}
public static class DoliLocalEditRunningDocument {
    [DllImport("ole32.dll")]
    private static extern int GetRunningObjectTable(int reserved, out IRunningObjectTable table);
    [DllImport("ole32.dll")]
    private static extern int CreateBindCtx(int reserved, out IBindCtx context);
    public static object Find(string path) {
        IRunningObjectTable table = null;
        IBindCtx context = null;
        IEnumMoniker enumerator = null;
        try {
            Marshal.ThrowExceptionForHR(GetRunningObjectTable(0, out table));
            Marshal.ThrowExceptionForHR(CreateBindCtx(0, out context));
            table.EnumRunning(out enumerator);
            IMoniker[] monikers = new IMoniker[1];
            while (enumerator.Next(1, monikers, IntPtr.Zero) == 0) {
                try {
                    string name;
                    monikers[0].GetDisplayName(context, null, out name);
                    if (String.Equals(name, path, StringComparison.OrdinalIgnoreCase)
                        || String.Equals(name, "!" + path, StringComparison.OrdinalIgnoreCase)) {
                        object document;
                        table.GetObject(monikers[0], out document);
                        return document;
                    }
                } catch (COMException) {
                    // An unrelated running object can disappear during enumeration.
                } finally {
                    if (monikers[0] != null) Marshal.ReleaseComObject(monikers[0]);
                }
            }
            return null;
        } finally {
            if (enumerator != null) Marshal.ReleaseComObject(enumerator);
            if (context != null) Marshal.ReleaseComObject(context);
            if (table != null) Marshal.ReleaseComObject(table);
        }
    }
    public static long Identity(object value) {
        IntPtr identity = Marshal.GetIUnknownForObject(value);
        try { return identity.ToInt64(); }
        finally { Marshal.Release(identity); }
    }
}
'@

function Get-DocumentCollection($application) {
    foreach ($name in @('Documents', 'Workbooks', 'Presentations')) {
        try {
            $collection = $application.$name
            if ($null -ne $collection) { return $name }
        } catch { }
    }
    return $null
}

function Get-DocumentOwnerProcess($application, $document, $name) {
    try {
        # Word has no Application.Hwnd: use this exact document's own window.
        if ($name -eq 'Documents') {
            $window = [IntPtr]$document.Windows.Item(1).Hwnd
        } else {
            # Excel uses Hwnd; PowerPoint uses HWND (COM property names ignore case).
            $window = [IntPtr]$application.Hwnd
        }
        return [DoliLocalEditOwnerProcess]::Capture($window)
    } catch {
        return $null
    }
}

function Get-OfficeFailureState($owner, $renamed) {
    # A busy or inaccessible COM server alone never proves the document closed.
    # Only the retained handle of a previously matched document owner can do so.
    if (-not $renamed -and $null -ne $owner) {
        try {
            if ($owner.Observe() -eq 1) { return 'CLOSED' }
        } catch { }
    }
    return 'UNKNOWN'
}
'''

_OFFICE_MONITOR_SCRIPT = r'''
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0
$inputData = [Console]::ReadLine() | ConvertFrom-Json
$targetPath = [IO.Path]::GetFullPath([string]$inputData.path)
$applications = @($inputData.applications)
''' + _OFFICE_MONITOR_NATIVE_SUPPORT + r'''

$trackedApplication = $null
$trackedDocument = $null
$collectionName = $null
$trackedIdentity = 0L
$trackedOwner = $null
$documentClosed = $false
$documentRenamed = $false
while ($true) {
    $state = 'UNKNOWN'
    try {
        if ((Get-OfficeFailureState $trackedOwner $documentRenamed) -eq 'CLOSED') {
            # Disconnected COM objects survive a normal last-window quit. Release their
            # identities and search the ROT again, including a newly started process.
            $trackedApplication = $null
            $trackedDocument = $null
            $collectionName = $null
            $trackedIdentity = 0L
            $trackedOwner.Dispose()
            $trackedOwner = $null
            $documentClosed = $true
        }
        if ($documentClosed) { $state = 'CLOSED' }
        if ($null -eq $trackedDocument) {
            $candidate = [DoliLocalEditRunningDocument]::Find($targetPath)
            if ($null -ne $candidate) {
                if ([String]::Equals([string]$candidate.FullName, $targetPath, [StringComparison]::OrdinalIgnoreCase)) {
                    # Any new exact match invalidates the previous owner's close proof.
                    $documentClosed = $false
                    $state = 'UNKNOWN'
                    $candidateApplication = $candidate.Application
                    $candidateCollection = Get-DocumentCollection $candidateApplication
                    if ($null -ne $candidateCollection) {
                        $trackedApplication = $candidateApplication
                        $trackedDocument = $candidate
                        $collectionName = $candidateCollection
                    }
                }
            }
            if ($null -eq $trackedDocument) {
                foreach ($program in $applications) {
                    try {
                        $application = [Runtime.InteropServices.Marshal]::GetActiveObject([string]$program)
                        $name = Get-DocumentCollection $application
                        if ($null -eq $name) { continue }
                        $collection = $application.$name
                        for ($index = 1; $index -le $collection.Count; $index++) {
                            $candidate = $collection.Item($index)
                            if ([String]::Equals([string]$candidate.FullName, $targetPath, [StringComparison]::OrdinalIgnoreCase)) {
                                $documentClosed = $false
                                $state = 'UNKNOWN'
                                $trackedApplication = $application
                                $trackedDocument = $candidate
                                $collectionName = $name
                                break
                            }
                        }
                        if ($null -ne $trackedDocument) { break }
                    } catch { }
                }
            }
            if ($null -ne $trackedDocument) {
                $trackedIdentity = [DoliLocalEditRunningDocument]::Identity($trackedDocument)
            }
        }
        if ($null -ne $trackedDocument -and -not $documentRenamed) {
            $present = $false
            $collection = $trackedApplication.$collectionName
            for ($index = 1; $index -le $collection.Count; $index++) {
                $current = $collection.Item($index)
                if ([DoliLocalEditRunningDocument]::Identity($current) -eq $trackedIdentity) {
                    $present = $true
                    if ([String]::Equals([string]$current.FullName, $targetPath, [StringComparison]::OrdinalIgnoreCase)) {
                        $state = 'OPEN'
                    } else {
                        # Save As must not silently publish the old destination.
                        $documentRenamed = $true
                    }
                    break
                }
            }
            if (-not $present) {
                # A document can reopen during the final-save grace period.
                # Continue observing the same path in this same application.
                for ($index = 1; $index -le $collection.Count; $index++) {
                    $current = $collection.Item($index)
                    if ([String]::Equals([string]$current.FullName, $targetPath, [StringComparison]::OrdinalIgnoreCase)) {
                        $trackedDocument = $current
                        $trackedIdentity = [DoliLocalEditRunningDocument]::Identity($current)
                        $state = 'OPEN'
                        break
                    }
                }
                if ($state -ne 'OPEN') {
                    # The old application may stay alive with other documents while
                    # this path reopens in a different instance. Forget its identity
                    # and owner so every following observation searches the ROT again.
                    $trackedApplication = $null
                    $trackedDocument = $null
                    $collectionName = $null
                    $trackedIdentity = 0L
                    if ($null -ne $trackedOwner) { $trackedOwner.Dispose() }
                    $trackedOwner = $null
                    $documentClosed = $true
                    $state = 'UNKNOWN'
                }
            }
            if ($state -eq 'OPEN' -and $null -eq $trackedOwner) {
                $trackedOwner = Get-DocumentOwnerProcess $trackedApplication $trackedDocument $collectionName
            }
        }
    } catch {
        $state = Get-OfficeFailureState $trackedOwner $documentRenamed
    }
    [Console]::WriteLine($state)
    [Console]::Out.Flush()
    Start-Sleep -Milliseconds 300
}
'''
