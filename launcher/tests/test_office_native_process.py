"""Exercise the actual Windows owner-handle probe without requiring Office."""

import base64
import os
from pathlib import Path
import subprocess
import sys
import unittest

from dolilocaledit_launcher.document_monitor import _OFFICE_MONITOR_NATIVE_SUPPORT, _OFFICE_MONITOR_SCRIPT


def _powershell() -> Path:
    if sys.platform == "win32":
        return Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    return Path("/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe")


@unittest.skipUnless(_powershell().is_file(), "Native Windows PowerShell is unavailable.")
class OfficeNativeProcessTest(unittest.TestCase):
    def run_native(self, fixture: str) -> None:
        script = "$ErrorActionPreference='Stop'\nSet-StrictMode -Version 2.0\n" + _OFFICE_MONITOR_NATIVE_SUPPORT + fixture
        encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
        result = subprocess.run(
            [str(_powershell()), "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=45,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "PASS")

    def helper_fixture(self, mutation: str) -> list[str]:
        """Run the real helper loop with synthetic Office collections and a bounded ROT."""
        fixture = r'''
Add-Type -TypeDefinition @'
using System;
using System.Collections.Generic;
public sealed class FixtureOfficeDocument {
    public string FullName;
    public FixtureOfficeApplication Application;
    public FixtureOfficeDocument(string path, FixtureOfficeApplication app) {
        FullName = path; Application = app;
    }
}
public sealed class FixtureOfficeCollection {
    private List<FixtureOfficeDocument> documents = new List<FixtureOfficeDocument>();
    public int Count { get { return documents.Count; } }
    public FixtureOfficeDocument Item(int index) { return documents[index - 1]; }
    public void Add(FixtureOfficeDocument document) { documents.Add(document); }
    public void Clear() { documents.Clear(); }
}
public sealed class FixtureOfficeApplication {
    public FixtureOfficeCollection Workbooks = new FixtureOfficeCollection();
    public IntPtr Hwnd = IntPtr.Zero;
}
'@
$oldApplication = New-Object FixtureOfficeApplication
$newApplication = New-Object FixtureOfficeApplication
$oldDocument = New-Object FixtureOfficeDocument -ArgumentList $targetPath,$oldApplication
$newDocument = New-Object FixtureOfficeDocument -ArgumentList $targetPath,$newApplication
$otherDocument = New-Object FixtureOfficeDocument -ArgumentList ($targetPath+'.other'),$oldApplication
$oldApplication.Workbooks.Add($oldDocument)
$newApplication.Workbooks.Add($newDocument)
$script:fixtureCandidate = $oldDocument
$fixtureCycle = 0
'''
        script = _OFFICE_MONITOR_SCRIPT.replace(
            "$inputData = [Console]::ReadLine() | ConvertFrom-Json",
            "$inputData = '{\"path\":\"C:/synthetic/monitor.xlsx\",\"applications\":[]}' | ConvertFrom-Json",
        ).replace(
            "$trackedApplication = $null\n$trackedDocument = $null\n$collectionName = $null\n$trackedIdentity = 0L\n",
            fixture + "$trackedApplication = $null\n$trackedDocument = $null\n$collectionName = $null\n$trackedIdentity = 0L\n",
            1,
        ).replace(
            "[DoliLocalEditRunningDocument]::Find($targetPath)", "$script:fixtureCandidate",
        ).replace(
            "while ($true) {", "while ($fixtureCycle -lt 5) {\n    $fixtureCycle++",
        ).replace("Start-Sleep -Milliseconds 300", mutation)
        result = subprocess.run(
            [str(_powershell()), "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, errors="replace", timeout=45, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.splitlines()

    def test_helper_reopens_exact_document_in_another_live_instance(self) -> None:
        states = self.helper_fixture(r'''
if ($fixtureCycle -eq 1) {
    $oldApplication.Workbooks.Clear()
    $oldApplication.Workbooks.Add($otherDocument)
    $script:fixtureCandidate = $newDocument
}
if ($fixtureCycle -eq 3) { $newDocument.FullName = $targetPath+'.renamed' }
if ($fixtureCycle -eq 4) {
    $newApplication.Workbooks.Clear()
    $script:fixtureCandidate = $null
}
''')
        self.assertEqual(states, ["OPEN", "UNKNOWN", "OPEN", "UNKNOWN", "UNKNOWN"])

    def test_helper_keeps_close_proof_while_rescanning_for_other_instances(self) -> None:
        states = self.helper_fixture(r'''
if ($fixtureCycle -eq 1) {
    $oldApplication.Workbooks.Clear()
    $oldApplication.Workbooks.Add($otherDocument)
    $script:fixtureCandidate = $null
}
''')
        self.assertEqual(states, ["OPEN", "UNKNOWN", "CLOSED", "CLOSED", "CLOSED"])

    def test_com_failure_requires_a_proven_exact_owner_exit(self) -> None:
        self.run_native(r'''
$alive = New-Object PSObject
$alive | Add-Member ScriptMethod Observe { return 0 }
$uncertain = New-Object PSObject
$uncertain | Add-Member ScriptMethod Observe { return -1 }
$denied = New-Object PSObject
$denied | Add-Member ScriptMethod Observe { throw [UnauthorizedAccessException]::new('Synthetic denial') }
$exited = New-Object PSObject
$exited | Add-Member ScriptMethod Observe { return 1 }
foreach ($candidate in @($null, $alive, $uncertain, $denied)) {
    if ((Get-OfficeFailureState $candidate $false) -ne 'UNKNOWN') { throw 'Unproven close after COM failure.' }
}
if ((Get-OfficeFailureState $exited $false) -ne 'CLOSED') { throw 'Proven owner exit was missed.' }
if ((Get-OfficeFailureState $exited $true) -ne 'UNKNOWN') { throw 'Save As published the old target.' }
if ($null -ne [DoliLocalEditOwnerProcess]::Capture([IntPtr]::Zero)) { throw 'An absent owner window was accepted.' }
[Console]::WriteLine('PASS')
''')

    def test_native_handle_survives_exit_and_new_process_without_pid_relookup(self) -> None:
        self.run_native(r'''
function Start-FixtureWindow {
    $childCode = @'
$ErrorActionPreference='Stop'
Add-Type -AssemblyName System.Windows.Forms
$form=New-Object Windows.Forms.Form
try {
    [Console]::WriteLine($form.Handle.ToInt64())
    [Console]::Out.Flush()
    [void][Console]::ReadLine()
} finally { $form.Dispose() }
'@
    $options=New-Object Diagnostics.ProcessStartInfo
    $options.FileName=Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $options.Arguments='-NoLogo -NoProfile -NonInteractive -EncodedCommand '+[Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($childCode))
    $options.UseShellExecute=$false
    $options.CreateNoWindow=$true
    $options.RedirectStandardInput=$true
    $options.RedirectStandardOutput=$true
    $options.RedirectStandardError=$true
    $fixture=New-Object PSObject -Property @{ Process=[Diagnostics.Process]::Start($options); Window=[IntPtr]::Zero }
    try {
        $line=$fixture.Process.StandardOutput.ReadLineAsync()
        if (-not $line.Wait(15000)) { throw 'Owned fixture did not create its window in time.' }
        $fixture.Window=[IntPtr]([long]$line.Result)
        if ($fixture.Window -eq [IntPtr]::Zero) { throw 'Owned fixture window is missing.' }
        return $fixture
    } catch {
        $fixture.Process.StandardInput.Close()
        if (-not $fixture.Process.WaitForExit(5000)) { $fixture.Process.Kill() }
        $fixture.Process.Dispose()
        throw
    }
}
function Stop-FixtureWindow($fixture) {
    if ($null -ne $fixture) {
        if (-not $fixture.Process.HasExited) {
            $fixture.Process.StandardInput.WriteLine('finish')
            $fixture.Process.StandardInput.Flush()
            if (-not $fixture.Process.WaitForExit(10000)) { throw 'Owned fixture did not exit.' }
        }
        $fixture.Process.Dispose()
    }
}
$original=$null; $unrelated=$null; $replacement=$null; $owner=$null
try {
    $original=Start-FixtureWindow
    $owner=[DoliLocalEditOwnerProcess]::Capture($original.Window)
    if ($null -eq $owner -or $owner.Observe() -ne 0) { throw 'Exact live owner was not retained.' }
    $unrelated=Start-FixtureWindow
    Stop-FixtureWindow $unrelated; $unrelated=$null
    if ($owner.Observe() -ne 0) { throw 'An unrelated process exit closed the document.' }
    if ((Get-OfficeFailureState $owner $false) -ne 'UNKNOWN') { throw 'A live owner closed on COM busy.' }
    Stop-FixtureWindow $original; $original=$null
    if ($owner.Observe() -ne 1) { throw 'The exact owner exit was not detected.' }
    $replacement=Start-FixtureWindow
    # The original process's retained HANDLE remains signaled even when a new
    # instance exists. A recycled numerical PID cannot change this identity.
    for ($index=0; $index -lt 6; $index++) {
        if ($owner.Observe() -ne 1 -or (Get-OfficeFailureState $owner $false) -ne 'CLOSED') { throw 'Exit evidence was lost or reused.' }
        Start-Sleep -Milliseconds 100
    }
    $owner.Dispose()
    if ((Get-OfficeFailureState $owner $false) -ne 'UNKNOWN') { throw 'A disposed handle was accepted as proof.' }
    [Console]::WriteLine('PASS')
} finally {
    if ($null -ne $owner) { $owner.Dispose() }
    Stop-FixtureWindow $original
    Stop-FixtureWindow $unrelated
    Stop-FixtureWindow $replacement
}
''')


if __name__ == "__main__":
    unittest.main()
