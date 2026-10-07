#!/bin/bash
# Создаёт venv, ставит зависимости, делает первый (интерактивный) логин и вешает launchd на каждый час.
# launchd ставится только после первого прогона (когда есть state.json), чтобы RunAtLoad
# не сделал прогон раньше бэкфилла: сначала `.venv/bin/python reader.py --days 7`.
set -euo pipefail
DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$DIR"

[ -f .env ] || { cp .env.example .env; echo "Заполни $DIR/.env и запусти снова."; exit 1; }

[ -x .venv/bin/python ] || python3 -m venv .venv
.venv/bin/pip install -q -r requirements.txt

if [ ! -f tg.session ]; then
  echo "Первый вход: Telegram спросит номер и код (один раз)."
fi
.venv/bin/python reader.py --login

if [ ! -f state.json ]; then
  echo "Теперь первый прогон, например за неделю: .venv/bin/python reader.py --days 7"
  echo "(посмотреть заранее, что запишется: добавь --dry-run). Потом запусти install.sh ещё раз — он поставит launchd."
  exit 0
fi

PLIST="$HOME/Library/LaunchAgents/com.user.tg-group-reader.plist"
mkdir -p "$HOME/Library/LaunchAgents"
sed "s#__DIR__#$DIR#g" launchd/com.user.tg-group-reader.plist > "$PLIST"
launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "Готово: запускается раз в час. Логи: $DIR/reader.log"
