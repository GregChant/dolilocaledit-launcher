#!/bin/sh
set -eu

cd "$(dirname "$0")/.."

DLE_IMAGE=dolilocaledit-linux-builder:python-3.14.7
DLE_OUTPUT=${1:-dist/launcher-linux}

docker build \
	--file launcher/linux/Dockerfile.build \
	--tag "$DLE_IMAGE" \
	.
mkdir -p "$DLE_OUTPUT"
DLE_ROOT=$(pwd -P)
DLE_OUTPUT_ABSOLUTE=$(cd "$DLE_OUTPUT" && pwd -P)
case "$DLE_OUTPUT_ABSOLUTE" in
	"$DLE_ROOT"/*) DLE_OUTPUT_RELATIVE=${DLE_OUTPUT_ABSOLUTE#"$DLE_ROOT"/} ;;
	*) echo "The launcher output directory must be inside the repository." >&2; exit 64 ;;
esac

docker run --rm \
	--network none \
	--user "$(id -u):$(id -g)" \
	--volume "$DLE_ROOT:/source" \
	"$DLE_IMAGE" \
	--output-directory "/source/$DLE_OUTPUT_RELATIVE"
