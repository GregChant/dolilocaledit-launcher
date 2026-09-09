param(
    [Parameter(Mandatory = $true)]
    [string]$SourceRoot,

    [Parameter(Mandatory = $true)]
    [string]$OutputDirectory
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$BuildRoot = Join-Path ([System.IO.Path]::GetTempPath()) ('dolilocaledit-build-' + [guid]::NewGuid().ToString('N'))
$RuntimeArchive = Join-Path $BuildRoot 'python-3.14.7-amd64.zip'
$Runtime = Join-Path $BuildRoot 'python'
$Python = Join-Path $Runtime 'python.exe'
$VirtualEnvironment = Join-Path $BuildRoot 'venv'
$TemporaryOutput = Join-Path $BuildRoot 'output'
$TemporaryWork = Join-Path $BuildRoot 'work'
$VenvPython = Join-Path $VirtualEnvironment 'Scripts\python.exe'
$RuntimeUrl = 'https://www.python.org/ftp/python/3.14.7/python-3.14.7-amd64.zip'
$RuntimeSha256 = 'ac1a727a71738e11de80b76e975f9b8a258aea6412bfc31696b929d59c6aafd0'
Remove-Item Env:PYTHONHOME -ErrorAction SilentlyContinue
Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
$env:PYTHONNOUSERSITE = '1'
$env:PYTHONHASHSEED = '0'

try {
    New-Item -ItemType Directory -Path $BuildRoot | Out-Null
    Invoke-WebRequest -UseBasicParsing -Uri $RuntimeUrl -OutFile $RuntimeArchive
    $ActualRuntimeSha256 = (Get-FileHash -LiteralPath $RuntimeArchive -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($ActualRuntimeSha256 -ne $RuntimeSha256) {
        throw 'The Python runtime archive does not match the audited SHA-256.'
    }
    Expand-Archive -LiteralPath $RuntimeArchive -DestinationPath $Runtime
    $PythonSignature = Get-AuthenticodeSignature -LiteralPath $Python
    $RuntimeDllSignature = Get-AuthenticodeSignature -LiteralPath (Join-Path $Runtime 'python314.dll')
    if (
        $PythonSignature.Status -ne 'Valid' -or
        $RuntimeDllSignature.Status -ne 'Valid' -or
        $null -eq $PythonSignature.SignerCertificate -or
        $null -eq $RuntimeDllSignature.SignerCertificate -or
        $null -eq $PythonSignature.TimeStamperCertificate -or
        $null -eq $RuntimeDllSignature.TimeStamperCertificate -or
        $PythonSignature.SignerCertificate.Thumbprint -ne $RuntimeDllSignature.SignerCertificate.Thumbprint
    ) {
        throw 'The Python runtime does not have consistent valid timestamped Authenticode signatures.'
    }
    $PythonVersion = (& $Python -c 'import platform; print(platform.python_version())').Trim()
    if ($LASTEXITCODE -ne 0 -or $PythonVersion -ne '3.14.7') {
        throw 'The temporary Windows runtime is not Python 3.14.7.'
    }
    & $Python -m venv $VirtualEnvironment
    if ($LASTEXITCODE -ne 0) { throw 'Unable to create the isolated Windows build environment.' }
    & $VenvPython -m pip install --disable-pip-version-check --require-hashes --no-cache-dir `
        --requirement (Join-Path $SourceRoot 'launcher\requirements-build.txt')
    if ($LASTEXITCODE -ne 0) { throw 'Unable to install the hash-locked build dependencies.' }
    & $VenvPython (Join-Path $SourceRoot 'launcher\build.py') `
        --output-directory $TemporaryOutput `
        --work-directory $TemporaryWork
    if ($LASTEXITCODE -ne 0) { throw 'The Windows launcher build failed.' }
    New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null
    Copy-Item -LiteralPath (Join-Path $TemporaryOutput 'dolilocaledit-launcher.exe') `
        -Destination (Join-Path $OutputDirectory 'dolilocaledit-launcher.exe') -Force
    Copy-Item -LiteralPath (Join-Path $TemporaryOutput 'install.cmd') `
        -Destination (Join-Path $OutputDirectory 'install.cmd') -Force
}
finally {
    if (Test-Path -LiteralPath $BuildRoot) {
        Remove-Item -LiteralPath $BuildRoot -Recurse -Force
    }
}
