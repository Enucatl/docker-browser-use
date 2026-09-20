#!/bin/sh
# Serve noVNC over HTTP and bridge WebSocket ↔ internal VNC (browser:5900).
# Public auth is Traefik Authelia + secured@file; this process has no RFB password.
set -eu

VNC_TARGET="${VNC_TARGET:-browser:5900}"
NOVNC_PORT="${NOVNC_PORT:-6080}"
NOVNC_WEB="${NOVNC_WEB:-/usr/share/novnc}"

exec websockify --web="${NOVNC_WEB}" "${NOVNC_PORT}" "${VNC_TARGET}"
