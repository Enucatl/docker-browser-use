#!/bin/sh
# Launch headed Chromium on Xvfb with un-published CDP + VNC.
# Chromium binds DevTools to loopback only; nginx publishes CDP on 0.0.0.0:CDP_PORT
# with Host rewritten to localhost. Host publishing of CDP/VNC remains omitted in compose.
# noVNC (Traefik/Authelia) reaches x11vnc over the internal default network.
set -eu

USER_DATA_DIR="${CHROME_USER_DATA_DIR:-/data/chrome-profile}"
DOWNLOAD_DIR="${CHROME_DOWNLOAD_DIR:-/data/downloads}"
CDP_PORT="${CDP_PORT:-9222}"
# Loopback-only port Chromium actually listens on (modern Chrome is loopback-only).
CDP_LOOPBACK_PORT="${CDP_LOOPBACK_PORT:-9223}"
NGINX_CONF="${CDP_NGINX_CONF:-/etc/chromium-cdp-nginx.conf}"
DISPLAY_NUM="${DISPLAY_NUM:-99}"
export DISPLAY=":${DISPLAY_NUM}"
XVFB_WHD="${XVFB_WHD:-1920x1080x24}"
VNC_PORT="${VNC_PORT:-5900}"
# T022: view-only operator watch. T023 will add interactive takeover.
VNC_VIEW_ONLY="${VNC_VIEW_ONLY:-true}"

mkdir -p "${USER_DATA_DIR}" "${DOWNLOAD_DIR}" /tmp/chromium \
  /tmp/nginx_client_body /tmp/nginx_proxy /tmp/nginx_fastcgi \
  /tmp/nginx_uwsgi /tmp/nginx_scgi

# Drop stale singleton locks left by a previous container instance on this volume.
rm -f \
  "${USER_DATA_DIR}/SingletonLock" \
  "${USER_DATA_DIR}/SingletonCookie" \
  "${USER_DATA_DIR}/SingletonSocket"

# Drop the image CMD token when present so it is not treated as a URL.
if [ "${1:-}" = "chromium" ]; then
  shift
fi

# Smoke mode: prove the image can start Chromium, then exit (headless; no Xvfb needed).
if [ "${1:-}" = "smoke" ]; then
  shift
  exec chromium \
    --headless=new \
    --ozone-platform=headless \
    --no-first-run \
    --no-default-browser-check \
    --user-data-dir="${USER_DATA_DIR}" \
    --disk-cache-dir=/tmp/chromium/cache \
    ${CHROMIUM_FLAGS:-} \
    --dump-dom \
    "about:blank" \
    "$@"
fi

# Virtual framebuffer for headed Chrome (live view via VNC/noVNC).
Xvfb "${DISPLAY}" -screen 0 "${XVFB_WHD}" -ac +extension RANDR +extension GLX &
xvfb_pid=$!

i=0
while [ "${i}" -lt 50 ]; do
  if [ -S "/tmp/.X11-unix/X${DISPLAY_NUM}" ]; then
    break
  fi
  i=$((i + 1))
  sleep 0.1
done
if [ ! -S "/tmp/.X11-unix/X${DISPLAY_NUM}" ]; then
  echo "Xvfb failed to start on ${DISPLAY}" >&2
  kill "${xvfb_pid}" 2>/dev/null || true
  exit 1
fi

viewonly_args=""
if [ "${VNC_VIEW_ONLY}" = "true" ] || [ "${VNC_VIEW_ONLY}" = "1" ]; then
  viewonly_args="-viewonly"
fi

# Never publish ${VNC_PORT} on the host or traefik_proxy.
# -nopw: Authelia gates the HTTP noVNC edge; raw RFB is not an external service.
x11vnc \
  -display "${DISPLAY}" \
  -forever \
  -shared \
  -rfbport "${VNC_PORT}" \
  -listen 0.0.0.0 \
  -nopw \
  -xkb \
  -noxdamage \
  -quiet \
  ${viewonly_args} &
x11vnc_pid=$!

# Render listen / upstream ports into a writable nginx conf.
RUNTIME_NGINX_CONF="/tmp/chromium-cdp-nginx.conf"
sed \
  -e "s/listen 9222;/listen ${CDP_PORT};/" \
  -e "s|http://127.0.0.1:9223|http://127.0.0.1:${CDP_LOOPBACK_PORT}|g" \
  -e "s|Host 127.0.0.1:9223;|Host 127.0.0.1:${CDP_LOOPBACK_PORT};|" \
  "${NGINX_CONF}" > "${RUNTIME_NGINX_CONF}"

# Headed Chromium on Xvfb (not headless) so operators can watch via noVNC.
chromium \
  --no-first-run \
  --no-default-browser-check \
  --user-data-dir="${USER_DATA_DIR}" \
  --disk-cache-dir=/tmp/chromium/cache \
  --download-default-directory="${DOWNLOAD_DIR}" \
  --remote-debugging-address=127.0.0.1 \
  --remote-debugging-port="${CDP_LOOPBACK_PORT}" \
  --remote-allow-origins=* \
  --window-size=1920,1080 \
  ${CHROMIUM_FLAGS:-} \
  "about:blank" \
  "$@" &
chrome_pid=$!

# Wait briefly for DevTools before starting the front proxy.
i=0
while [ "${i}" -lt 50 ]; do
  if curl -sf "http://127.0.0.1:${CDP_LOOPBACK_PORT}/json/version" >/dev/null 2>&1; then
    break
  fi
  i=$((i + 1))
  sleep 0.1
done

nginx -c "${RUNTIME_NGINX_CONF}" &
nginx_pid=$!

shutdown() {
  kill "${chrome_pid}" "${nginx_pid}" "${x11vnc_pid}" "${xvfb_pid}" 2>/dev/null || true
  wait "${chrome_pid}" 2>/dev/null || true
  wait "${nginx_pid}" 2>/dev/null || true
  wait "${x11vnc_pid}" 2>/dev/null || true
  wait "${xvfb_pid}" 2>/dev/null || true
}
trap shutdown INT TERM

wait "${chrome_pid}"
status=$?
shutdown
exit "${status}"
