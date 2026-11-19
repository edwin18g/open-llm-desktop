#!/usr/bin/env bash
# Adds "EK Light LLM" to your applications menu.
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
chmod +x "$DIR/run.sh"
mkdir -p ~/.local/share/applications
cat > ~/.local/share/applications/ek-light-llm.desktop <<DESK
[Desktop Entry]
Type=Application
Name=EK Light LLM
Comment=Offline chat with Gemma 4 E2B (LiteRT-LM)
Exec=$DIR/run.sh
Icon=$DIR/ui/icon.svg
Terminal=false
Categories=Development;Utility;
StartupWMClass=ek-light-llm
DESK
update-desktop-database ~/.local/share/applications 2>/dev/null || true
echo "Installed. Find 'EK Light LLM' in your app menu."
