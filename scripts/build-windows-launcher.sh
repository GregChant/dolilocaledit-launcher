#!/bin/sh
set -eu

cd "$(dirname "$0")/.."

DLE_OUTPUT=${1:-dist/launcher-windows}
mkdir -p "$DLE_OUTPUT"
DLE_ROOT=$(pwd -P)
DLE_OUTPUT_ABSOLUTE=$(cd "$DLE_OUTPUT" && pwd -P)

powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass \
	-File "$(wslpath -w "$DLE_ROOT/launcher/windows/build-secure.ps1")" \
	-SourceRoot "$(wslpath -w "$DLE_ROOT")" \
	-OutputDirectory "$(wslpath -w "$DLE_OUTPUT_ABSOLUTE")"
