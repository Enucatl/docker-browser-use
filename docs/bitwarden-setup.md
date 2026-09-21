# Bitwarden browser extension (T026)

The browser image contains the official Bitwarden Chrome Web Store extension,
registered as a root-owned Chromium external extension. The image pins the
downloaded CRX version and SHA-256. Chromium installs it into the persistent
`chrome_profile` volume on first start; rebuilding or recreating the `browser`
container does not remove the profile.

This is an agent-specific Bitwarden account or vault collection, not an import
of an operator's desktop profile. Keep the collection least-privileged.

## Install and smoke test

```bash
docker compose build browser
docker compose run --rm --no-deps browser smoke
docker compose up -d browser novnc
docker compose exec browser curl -sf http://127.0.0.1:9222/json/version
docker compose exec browser sh -c \
  'test -r /usr/lib/chromium/extensions/external_extensions.json && \
   find /data/chrome-profile -path "*/Extensions/nngceckbapebfimnlniiiahkandclblb/*" -print -quit | grep -q .'
```

The first command exercises headless Chromium with the extension registered;
the CDP request exercises the headed Xvfb session. In the live view at
`https://browser-use.${DOCKER_DOMAIN}/vnc/`, open `chrome://extensions` and
confirm **Bitwarden Password Manager** is present. Then restart `browser` and
check the extension again to verify the profile volume survives recreation.

The browser network is intentionally internal. If the agent vault server or
Bitwarden cloud needs network access for sign-in, temporarily attach the
running container to an egress-capable Docker network, and disconnect it as
soon as setup is complete:

```bash
BROWSER_CONTAINER="$(docker compose ps -q browser)"
docker network connect bridge "$BROWSER_CONTAINER"
# Complete the takeover steps below, then:
docker network disconnect bridge "$BROWSER_CONTAINER"
```

## Human takeover: sign in and unlock

1. Start a run from the private Agent Web UI and immediately click **Take
   control** on its run page. This parks Jev and blocks agent browser actions.
2. Open **Open live view (/vnc)**, use the Chrome extension/puzzle menu to open
   Bitwarden, and sign in to the agent account. Complete SSO, MFA, or device
   approval manually if prompted.
3. Unlock the vault and confirm the expected least-privilege collection is
   available. Do not paste the master password, recovery code, or vault data
   into a run goal, Jev prompt, shell command, Compose variable, `.env` file,
   or git-tracked file.
4. Return to the run page and click **Release control**. The agent can resume;
   use the same takeover path whenever the vault locks or a human MFA step is
   required.

The encrypted vault session state remains in `chrome_profile`, which is
sensitive operational data. Protect that Docker volume and do not sync it with
a personal desktop profile.

## Limitations

- TOTP retrieval or autofill is an extension/UI interaction and may require
  human takeover; T027 owns any explicit agent action for it.
- Passkeys/WebAuthn depend on the browser and authenticator/device. They are not
  a reliable unattended action in this container; use human takeover.
- The extension is updated by rebuilding the image. When Bitwarden publishes a
  new CRX, update the pinned version and SHA-256 build arguments together.
