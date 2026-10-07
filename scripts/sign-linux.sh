#!/bin/sh
set -eu

DLE_SCRIPT_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)
cd "$DLE_SCRIPT_ROOT"
DLE_RELEASE_VERSION=
DLE_VERSION_SET=0

for DLE_ARGUMENT in "$@"; do
	case "$DLE_ARGUMENT" in
		--V=*|--version=*)
			if [ "$DLE_VERSION_SET" = "1" ]; then
				echo "Specify the release version only once." >&2
				exit 64
			fi
			DLE_RELEASE_VERSION=${DLE_ARGUMENT#*=}
			DLE_VERSION_SET=1
			;;
		--help|-h)
			echo "Usage: $0 --V=x.y.z"
			echo "Sign build/release-launchers-x.y.z using the configured GnuPG key."
			echo "Optional environment: DLE_LINUX_SIGNING_KEY_FINGERPRINT, DLE_LINUX_SIGNING_PUBLIC_KEY, DLE_LINUX_RELEASE_CATALOG."
			exit 0
			;;
		*)
			echo "Unknown argument: $DLE_ARGUMENT" >&2
			exit 64
			;;
	esac
done

if ! printf '%s\n' "$DLE_RELEASE_VERSION" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+$'; then
	echo "A release version is required: --V=x.y.z" >&2
	exit 64
fi
DLE_RELEASE_CATALOG=${DLE_LINUX_RELEASE_CATALOG:-build/release-launchers-$DLE_RELEASE_VERSION}
if [ ! -d "$DLE_RELEASE_CATALOG/files" ]; then
	echo "Release catalog not found: $DLE_RELEASE_CATALOG/files" >&2
	exit 65
fi
DLE_LINUX_SIGNING_KEY_FINGERPRINT=${DLE_LINUX_SIGNING_KEY_FINGERPRINT:-A51FBBAB9A5092771768E8398C9507E9997C0569}
if ! printf '%s\n' "$DLE_LINUX_SIGNING_KEY_FINGERPRINT" | grep -Eq '^[A-Fa-f0-9]{40,64}$'; then
	echo "The signing-key fingerprint must be complete." >&2
	exit 64
fi
export DLE_LINUX_SIGNING_KEY_FINGERPRINT

DLE_EXPORTED_KEY_DIRECTORY=
trap 'if [ -n "$DLE_EXPORTED_KEY_DIRECTORY" ]; then rm -rf -- "$DLE_EXPORTED_KEY_DIRECTORY"; fi' EXIT HUP INT TERM
if [ -z "${DLE_LINUX_SIGNING_PUBLIC_KEY:-}" ]; then
	DLE_EXPORTED_KEY_DIRECTORY=$(mktemp -d "${TMPDIR:-/tmp}/dolilocaledit-public-signing-key.XXXXXX")
	DLE_LINUX_SIGNING_PUBLIC_KEY="$DLE_EXPORTED_KEY_DIRECTORY/release-key.asc"
	# Export only the public key; authentication remains entirely inside GnuPG.
	gpg --batch --armor --export "$DLE_LINUX_SIGNING_KEY_FINGERPRINT" > "$DLE_LINUX_SIGNING_PUBLIC_KEY"
	if [ ! -s "$DLE_LINUX_SIGNING_PUBLIC_KEY" ]; then
		echo "The configured public signing key is unavailable." >&2
		exit 65
	fi
fi
export DLE_LINUX_SIGNING_PUBLIC_KEY
./scripts/sign-linux-release.sh "$DLE_RELEASE_VERSION" "$DLE_RELEASE_CATALOG"
