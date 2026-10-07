#!/bin/bash
# Создаёт venv, ставит зависимости, делает первый (интерактивный) логин и вешает launchd на каждый час.
set -euo pipefail
DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$DIR"

[ -f .env ] || { cp .env.example .env; echo "Заполни $DIR/.env и запусти снова."; exit 1; }

python3 -m venv .venv
.venv/bin/pip install -q -r requirements.txt

echo "Первый запуск: Telegram спросит номер и код (один раз)."
.venv/bin/python reader.py

PLIST="$HOME/Library/LaunchAgents/com.user.tg-group-reader.plist"
sed "s#__DIR__#$DIR#g" launchd/com.user.tg-group-reader.plist > "$PLIST"
launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "Готово: запускается раз в час. Логи: $DIR/reader.log"
