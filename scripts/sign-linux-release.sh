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

for DLE_COMMAND in gpg sha256sum mktemp install python3; do
	if ! command -v "$DLE_COMMAND" >/dev/null 2>&1; then
		echo "Required Linux signing command is unavailable: $DLE_COMMAND" >&2
		exit 69
	fi
done

DLE_ARTIFACT_NAMES=$(python3 - "$DLE_VERSION" "$DLE_CATALOG_DIRECTORY" <<'PY'
import hashlib
import json
from pathlib import Path
import re
import sys

version, directory = sys.argv[1:]
catalog = Path(directory)
manifest_path = catalog / "manifest.json"
try:
    if manifest_path.is_symlink():
        raise ValueError("Launcher manifest is unsafe")
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict) or manifest.get("schema") not in {1, 2} or manifest.get("version") != version:
            raise ValueError("Launcher manifest version or schema is invalid")
        artifacts = manifest.get("artifacts")
        if not isinstance(artifacts, list) or len(artifacts) != 2:
            raise ValueError("Linux release signing requires exactly two launcher artifacts")
        names = {}
        for artifact in artifacts:
            if not isinstance(artifact, dict):
                raise ValueError("Launcher manifest artifact is invalid")
            platform = artifact.get("platform")
            name = artifact.get("filename")
            size = artifact.get("byte_size")
            digest = artifact.get("sha256")
            if (
                not isinstance(platform, str)
                or platform not in {"windows", "linux"}
                or platform in names
                or artifact.get("architecture") != "x86_64"
                or not isinstance(name, str)
                or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", name) is None
                or not name.endswith(".exe" if platform == "windows" else ".tar.gz")
                or not isinstance(size, int)
                or isinstance(size, bool)
                or size < 1
                or not isinstance(digest, str)
                or re.fullmatch(r"[a-f0-9]{64}", digest) is None
            ):
                raise ValueError("Launcher manifest artifact identity is invalid")
            expected_url = f"https://github.com/GregChant/dolilocaledit-launcher/releases/download/v{version}/{name}"
            if (manifest["schema"] == 2 and artifact.get("url") != expected_url) or (manifest["schema"] == 1 and "url" in artifact):
                raise ValueError("Launcher URL is not the immutable official release URL")
            path = catalog / "files" / name
            if path.is_symlink() or not path.is_file() or (catalog / "files").is_symlink() or path.stat().st_size != size:
                raise ValueError("Launcher artifact is missing or does not match its manifest")
            sha256 = hashlib.sha256()
            with path.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    sha256.update(block)
            if sha256.hexdigest() != digest:
                raise ValueError("Launcher artifact SHA-256 does not match its manifest")
            names[platform] = name
        print(names["windows"])
        print(names["linux"])
    else:
        # Compatibility with historical release catalogs created before manifests were required.
        print(f"DoliLocalEdit-Setup-{version}.exe")
        print(f"DoliLocalEdit-linux-x86_64-{version}.tar.gz")
except (OSError, UnicodeError, ValueError) as exc:
    print(f"Linux release catalog rejected: {exc}", file=sys.stderr)
    raise SystemExit(65)
PY
)
DLE_WINDOWS_NAME=$(printf '%s\n' "$DLE_ARTIFACT_NAMES" | sed -n '1p')
DLE_LINUX_NAME=$(printf '%s\n' "$DLE_ARTIFACT_NAMES" | sed -n '2p')
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

DLE_GPG_TTY=
if DLE_GPG_TTY=$(tty 2>/dev/null); then
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
