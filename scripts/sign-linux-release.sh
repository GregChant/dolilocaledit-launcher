#!/bin/sh
set -eu

cd "$(dirname "$0")/.."
export LC_ALL=C

DLE_VERSION=${1:-}
DLE_CATALOG_DIRECTORY=${2:-}
DLE_SIGNING_FINGERPRINT=${DLE_LINUX_SIGNING_KEY_FINGERPRINT:-}
DLE_PUBLIC_KEY=${DLE_LINUX_SIGNING_PUBLIC_KEY:-}

if ! printf '%s\n' "$DLE_VERSION" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+$'; then
	echo "Invalid Linux release version: expected x.y.z" >&2
	exit 64
fi
if [ -z "$DLE_CATALOG_DIRECTORY" ] || [ ! -d "$DLE_CATALOG_DIRECTORY/files" ]; then
	echo "Linux release catalog directory is missing" >&2
	exit 64
fi
if ! printf '%s\n' "$DLE_SIGNING_FINGERPRINT" | grep -Eq '^[A-Fa-f0-9]{40,64}$'; then
	echo "DLE_LINUX_SIGNING_KEY_FINGERPRINT must contain the full signing-key fingerprint" >&2
	exit 64
fi
DLE_SIGNING_FINGERPRINT=$(printf '%s' "$DLE_SIGNING_FINGERPRINT" | tr 'a-f' 'A-F')
if [ -z "$DLE_PUBLIC_KEY" ] || [ ! -f "$DLE_PUBLIC_KEY" ] || [ -L "$DLE_PUBLIC_KEY" ]; then
	echo "DLE_LINUX_SIGNING_PUBLIC_KEY must reference the regular armored public key" >&2
	exit 64
fi
DLE_PRIVATE_MARKER='-----BEGIN PGP '"PRIVATE KEY BLOCK-----"
if grep -Fq -- "$DLE_PRIVATE_MARKER" "$DLE_PUBLIC_KEY"; then
	echo "The Linux release public-key file contains private key material" >&2
	exit 65
fi

for DLE_COMMAND in gpg sha256sum mktemp install; do
	if ! command -v "$DLE_COMMAND" >/dev/null 2>&1; then
		echo "Required Linux signing command is unavailable: $DLE_COMMAND" >&2
		exit 69
	fi
done

DLE_WINDOWS_NAME="DoliLocalEdit-Setup-$DLE_VERSION.exe"
DLE_LINUX_NAME="DoliLocalEdit-linux-x86_64-$DLE_VERSION.tar.gz"
DLE_WINDOWS_PATH="$DLE_CATALOG_DIRECTORY/files/$DLE_WINDOWS_NAME"
DLE_LINUX_PATH="$DLE_CATALOG_DIRECTORY/files/$DLE_LINUX_NAME"
for DLE_ARTIFACT in "$DLE_WINDOWS_PATH" "$DLE_LINUX_PATH"; do
	if [ ! -s "$DLE_ARTIFACT" ] || [ -L "$DLE_ARTIFACT" ]; then
		echo "Release artifact is missing or unsafe: $DLE_ARTIFACT" >&2
		exit 65
	fi
done

if ! gpg --batch --with-colons --list-secret-keys "$DLE_SIGNING_FINGERPRINT!" 2>/dev/null \
	| grep -Eq '^(sec|ssb)(:|::)'; then
	echo "The exact Linux release signing key is unavailable in the GPG secret keyring" >&2
	exit 65
fi

DLE_TEMPORARY=$(mktemp -d "${TMPDIR:-/tmp}/dolilocaledit-linux-signing.XXXXXX")
DLE_VERIFY_HOME="$DLE_TEMPORARY/verify-home"
mkdir -m 0700 "$DLE_VERIFY_HOME"
trap 'rm -rf "$DLE_TEMPORARY"' EXIT HUP INT TERM

gpg --homedir "$DLE_VERIFY_HOME" --batch --quiet --import "$DLE_PUBLIC_KEY" >/dev/null 2>&1
if ! gpg --homedir "$DLE_VERIFY_HOME" --batch --with-colons --fingerprint --fingerprint \
	| awk -F: -v expected="$DLE_SIGNING_FINGERPRINT" '$1 == "fpr" && toupper($10) == expected { found = 1 } END { exit(found ? 0 : 1) }'; then
	echo "The public key does not contain the exact Linux release signing fingerprint" >&2
	exit 65
fi

(
	cd "$DLE_CATALOG_DIRECTORY/files"
	sha256sum "$DLE_WINDOWS_NAME" "$DLE_LINUX_NAME"
) > "$DLE_TEMPORARY/SHA256SUMS"

DLE_GPG_TTY=$(tty 2>/dev/null || true)
if [ -n "$DLE_GPG_TTY" ]; then
	export GPG_TTY="$DLE_GPG_TTY"
fi

gpg --yes --armor --detach-sign --local-user "$DLE_SIGNING_FINGERPRINT!" \
	--output "$DLE_TEMPORARY/$DLE_LINUX_NAME.asc" "$DLE_LINUX_PATH"
gpg --yes --armor --detach-sign --local-user "$DLE_SIGNING_FINGERPRINT!" \
	--output "$DLE_TEMPORARY/SHA256SUMS.asc" "$DLE_TEMPORARY/SHA256SUMS"

dle_verify_signature()
{
	DLE_SIGNATURE=$1
	DLE_SIGNED_FILE=$2
	DLE_STATUS=$(gpg --homedir "$DLE_VERIFY_HOME" --batch --status-fd=1 \
		--verify "$DLE_SIGNATURE" "$DLE_SIGNED_FILE" 2>/dev/null)
	printf '%s\n' "$DLE_STATUS" \
		| awk -v expected="$DLE_SIGNING_FINGERPRINT" '$2 == "VALIDSIG" && toupper($3) == expected { found = 1 } END { exit(found ? 0 : 1) }'
}

dle_verify_signature "$DLE_TEMPORARY/$DLE_LINUX_NAME.asc" "$DLE_LINUX_PATH"
dle_verify_signature "$DLE_TEMPORARY/SHA256SUMS.asc" "$DLE_TEMPORARY/SHA256SUMS"

install -m 0644 "$DLE_TEMPORARY/$DLE_LINUX_NAME.asc" "$DLE_LINUX_PATH.asc"
install -m 0644 "$DLE_TEMPORARY/SHA256SUMS" "$DLE_CATALOG_DIRECTORY/SHA256SUMS"
install -m 0644 "$DLE_TEMPORARY/SHA256SUMS.asc" "$DLE_CATALOG_DIRECTORY/SHA256SUMS.asc"
install -m 0644 "$DLE_PUBLIC_KEY" "$DLE_CATALOG_DIRECTORY/dolilocaledit-release-key.asc"

echo "Linux OpenPGP release signature: valid, exact signer $DLE_SIGNING_FINGERPRINT"
