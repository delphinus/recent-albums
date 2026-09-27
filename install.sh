#!/bin/bash
# LaunchAgent を入れて (入れ直して) 起動する。
#
#   ./install.sh
#
# plist にはこのディレクトリと python3 の絶対パスを埋める。launchd の PATH には Homebrew や
# バージョン管理ツールの python3 が無く、/usr/bin/python3 (3.9) では server.py の型注釈が通らないため。
set -euo pipefail

label=dev.delphinus.recent-albums
dir=$(cd "$(dirname "$0")" && pwd)
python=$(command -v python3)
dest=$HOME/Library/LaunchAgents/$label.plist

"$python" -c 'import sys; sys.exit(sys.version_info < (3, 10))' ||
  { echo "Python 3.10 以上が要る: $python" >&2; exit 1; }

sed -e "s#@PYTHON@#$python#" -e "s#@DIR@#$dir#" -e "s#@HOME@#$HOME#g" \
  "$dir/launchd/$label.plist.in" > "$dest"
launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$dest"
echo "installed: $dest (python: $python)"
