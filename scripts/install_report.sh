#!/bin/bash
# Ставит launchd-задачу отправки ежедневного отчёта USM в Telegram (11:32, повторы до 15:00).
set -euo pipefail
DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$DIR"
[ -x .venv/bin/python ] && [ -f tg.session ] || { echo "Сначала настрой reader (scripts/install.sh)."; exit 1; }
PLIST="$HOME/Library/LaunchAgents/com.user.usm-report.plist"
mkdir -p "$HOME/Library/LaunchAgents"
sed "s#__DIR__#$DIR#g" launchd/com.user.usm-report.plist > "$PLIST"
launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "Готово: отчёт уходит в «Избранное» после 11:32. Лог: $DIR/launchd.out"
