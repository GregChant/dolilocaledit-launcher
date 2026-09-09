param(
    [Parameter(Mandatory = $true)]
    [string]$Executable,

    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[A-Fa-f0-9]{64}$')]
    [string]$ExpectedSignerSha256
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$ResolvedExecutable = (Resolve-Path -LiteralPath $Executable).Path
$Signature = Get-AuthenticodeSignature -LiteralPath $ResolvedExecutable
if ($Signature.Status -ne 'Valid' -or $null -eq $Signature.SignerCertificate) {
    throw "The Windows launcher Authenticode signature is not valid: $($Signature.Status)."
}
if ($null -eq $Signature.TimeStamperCertificate) {
    throw 'The Windows launcher signature has no trusted timestamp.'
}
$SignerSha256 = $Signature.SignerCertificate.GetCertHashString(
    [System.Security.Cryptography.HashAlgorithmName]::SHA256
).ToUpperInvariant()
if ($SignerSha256 -ne $ExpectedSignerSha256.ToUpperInvariant()) {
    throw "The Windows launcher signer is unexpected: $SignerSha256."
}

Write-Output "Windows Authenticode signature: valid, timestamped, expected signer $SignerSha256"
