#!/bin/sh
# Встановлює Radar для поточного користувача (без sudo).
set -e

SRC="$(cd "$(dirname "$0")" && pwd)"
APP="$HOME/.local/share/radar/app"
BIN="$HOME/.local/bin"
APPS="$HOME/.local/share/applications"

mkdir -p "$APP/defaults" "$BIN" "$APPS"
cp "$SRC/radar.py" "$SRC/radar_gui.py" "$SRC/dashboard.py" "$SRC/dashboard_template.html" "$SRC/report_template.html" "$SRC/radar.svg" "$SRC/uninstall.sh" "$SRC/README.md" "$APP/"
cp "$SRC/defaults/sources.json" "$SRC/defaults/topics.txt" "$SRC/defaults/dashboard.json" "$APP/defaults/"
chmod +x "$APP/radar.py" "$APP/radar_gui.py" "$APP/uninstall.sh"
ln -sf "$APP/radar.py" "$BIN/radar"

cat > "$APPS/radar.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Radar
GenericName=Фінансові новини
Comment=Фінансові новини з обраних видань
Exec=/usr/bin/python3 $APP/radar_gui.py
Icon=$APP/radar.svg
Terminal=false
StartupWMClass=Radar
StartupNotify=true
Categories=Network;News;
Keywords=news;новини;фінанси;radar;
Actions=open;dashboard;run;

[Desktop Action open]
Name=Відкрити список новин
Exec=/usr/bin/python3 $APP/radar.py open

[Desktop Action dashboard]
Name=Дашборд: валюти й ринки
Exec=/usr/bin/python3 $APP/radar.py dashboard

[Desktop Action run]
Name=Оновити новини зараз
Exec=/usr/bin/python3 $APP/radar.py run --quiet
EOF
update-desktop-database "$APPS" 2>/dev/null || true

# Налаштування створюються лише якщо їх ще немає — оновлення програми їх не перезапише
/usr/bin/python3 "$APP/radar.py" schedule

echo "Radar встановлено. Меню → Radar, або в терміналі: radar help"
