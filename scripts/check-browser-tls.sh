#!/bin/sh
# Check the browser image against a trusted intranet URL and an untrusted CA.
set -eu
trusted_url="${1:?Usage: sh scripts/check-browser-tls.sh https://intranet.example/}"
dom="$(mktemp)"
trap 'rm -f "${dom}"' EXIT

docker compose run --pull never --rm --no-deps \
  -e CHROME_USER_DATA_DIR=/tmp/tls-smoke browser smoke "${trusted_url}" > "${dom}"
grep -q '</html>' "${dom}"
if grep -q 'main-frame-error' "${dom}"; then
  echo "Trusted URL displayed a browser error page" >&2
  exit 1
fi

docker compose run --pull never --rm --no-deps \
  -e CHROME_USER_DATA_DIR=/tmp/tls-smoke browser smoke \
  https://self-signed.badssl.com/ > "${dom}" 2>&1 || true
grep -q 'ERR_CERT_AUTHORITY_INVALID' "${dom}"
echo 'Trusted page loaded; untrusted certificate rejected.'
