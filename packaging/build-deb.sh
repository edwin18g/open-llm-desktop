#!/usr/bin/env bash
# Builds dist/ek-light-llm_<version>_all.deb.
# The package holds the app code only; on first launch it creates a Python venv in
# ~/.local/share/ek-light-llm and the app downloads the Gemma model from its own UI.
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")/.."

VERSION="${1:-1.0.0}"
PKG=ek-light-llm
BUILD="$(mktemp -d)"
trap 'rm -rf "$BUILD"' EXIT
ROOT="$BUILD/${PKG}_${VERSION}_all"

# --- app files ---
install -d "$ROOT/usr/lib/$PKG"
install -m 644 app.py attachments.py browser.py mcp_client.py plugins.py server.py store.py voice.py requirements.txt README.md "$ROOT/usr/lib/$PKG/"
cp -r ui servers "$ROOT/usr/lib/$PKG/"
find "$ROOT/usr/lib/$PKG" -name __pycache__ -prune -exec rm -rf {} +
find "$ROOT/usr/lib/$PKG" -type d -exec chmod 755 {} + -o -type f -exec chmod 644 {} +

# --- launcher ---
install -d "$ROOT/usr/bin"
cat > "$ROOT/usr/bin/$PKG" <<'SH'
#!/usr/bin/env bash
# EK Light LLM launcher: sets up a per-user venv on first run, then starts the app.
set -euo pipefail
APP=/usr/lib/ek-light-llm
DATA="${XDG_DATA_HOME:-$HOME/.local/share}/ek-light-llm"
VENV="$DATA/venv"
mkdir -p "$DATA"
export EK_MODELS_DIR="$DATA/models" EK_CACHE_DIR="$DATA/cache"

say() { echo "[ek-light-llm] $*"; command -v notify-send >/dev/null && notify-send "EK Light LLM" "$*" || true; }

if [ ! -x "$VENV/bin/python" ] || [ "$APP/requirements.txt" -nt "$VENV/.deps-ok" ]; then
  say "Setting up for first use (downloading Python packages, a few minutes)…"
  {
    [ -x "$VENV/bin/python" ] || python3 -m venv "$VENV"
    "$VENV/bin/pip" install -U pip
    "$VENV/bin/pip" install -r "$APP/requirements.txt"
  } >"$DATA/setup.log" 2>&1 || { say "Setup failed, see $DATA/setup.log"; exit 1; }
  touch "$VENV/.deps-ok"
  say "Setup done, starting…"
fi

cd "$DATA"
exec "$VENV/bin/python" "$APP/app.py" "$@"
SH
chmod 755 "$ROOT/usr/bin/$PKG"

# --- menu entry + icon ---
install -D -m 644 ui/icon.svg "$ROOT/usr/share/icons/hicolor/scalable/apps/$PKG.svg"
install -d "$ROOT/usr/share/applications"
cat > "$ROOT/usr/share/applications/$PKG.desktop" <<DESK
[Desktop Entry]
Type=Application
Name=EK Light LLM
Comment=Offline chat with Gemma 4 E2B (LiteRT-LM)
Exec=$PKG
Icon=$PKG
Terminal=false
Categories=Utility;
StartupWMClass=$PKG
DESK
chmod 644 "$ROOT/usr/share/applications/$PKG.desktop"

# --- control ---
install -d "$ROOT/DEBIAN"
cat > "$ROOT/DEBIAN/control" <<CTRL
Package: $PKG
Version: $VERSION
Section: utils
Priority: optional
Architecture: all
Depends: python3 (>= 3.10), python3-venv, libnotify-bin
Recommends: google-chrome-stable | chromium | chromium-browser, nodejs, npm, ffmpeg
Installed-Size: $(du -sk "$ROOT/usr" | cut -f1)
Maintainer: EK Light <edwin18g@gmail.com>
Description: Offline AI chat with Gemma 4 E2B and MCP plugins
 Chat app running Gemma 4 E2B locally with LiteRT-LM, shown in a Chrome or
 Chromium app window (any browser works). Connects to
 MCP servers (local commands or remote URLs, with OAuth sign-in) as plugins.
 On first launch it installs its Python packages into ~/.local/share and
 downloads the model (about 2.5 GB) from inside the app. Spoken replies
 (Piper, about 230 MB) are an optional download from voice mode.
CTRL
cat > "$ROOT/DEBIAN/postinst" <<'POST'
#!/bin/sh
set -e
command -v update-desktop-database >/dev/null && update-desktop-database -q /usr/share/applications || true
command -v gtk-update-icon-cache >/dev/null && gtk-update-icon-cache -q /usr/share/icons/hicolor || true
POST
cp "$ROOT/DEBIAN/postinst" "$ROOT/DEBIAN/postrm"
chmod 755 "$ROOT/DEBIAN/postinst" "$ROOT/DEBIAN/postrm"

mkdir -p dist
fakeroot dpkg-deb --build -Zxz "$ROOT" "dist/${PKG}_${VERSION}_all.deb"
echo "Built dist/${PKG}_${VERSION}_all.deb"
