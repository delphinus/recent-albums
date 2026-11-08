#!/bin/bash
# LaunchAgent を入れて (入れ直して) 起動する。Web アプリ (常駐) と、月初にランキングを作るもの (毎月 1 日) の 2 つ。
#
#   ./install.sh
#
# plist にはこのディレクトリと python3 の絶対パスを埋める。launchd の PATH には Homebrew や
# バージョン管理ツールの python3 が無く、/usr/bin/python3 (3.9) では server.py の型注釈が通らないため。
set -euo pipefail

dir=$(cd "$(dirname "$0")" && pwd)
python=$(command -v python3)

"$python" -c 'import sys; sys.exit(sys.version_info < (3, 10))' ||
  { echo "Python 3.10 以上が要る: $python" >&2; exit 1; }

for label in dev.delphinus.recent-albums dev.delphinus.recent-albums-monthly; do
  dest=$HOME/Library/LaunchAgents/$label.plist
  sed -e "s#@PYTHON@#$python#" -e "s#@DIR@#$dir#" -e "s#@HOME@#$HOME#g" \
    "$dir/launchd/$label.plist.in" > "$dest"
  launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$dest"
  echo "installed: $dest (python: $python)"
done
