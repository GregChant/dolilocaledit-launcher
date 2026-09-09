#!/bin/sh
set -eu

DLE_SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
DLE_LAUNCHER="$DLE_SCRIPT_DIR/dolilocaledit-launcher"

if [ ! -f "$DLE_LAUNCHER" ] || [ ! -x "$DLE_LAUNCHER" ]; then
	echo "Doli Local Edit : l’exécutable Linux est introuvable." >&2
	exit 2
fi

"$DLE_LAUNCHER" install --source "$DLE_LAUNCHER"
