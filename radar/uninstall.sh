#!/bin/sh
# Видаляє Radar. Ваші видання й теми (~/.config/radar) залишаються, якщо не додати --all.
systemctl --user disable --now radar.timer 2>/dev/null || true
systemctl --user disable --now radar-digest.timer 2>/dev/null || true
rm -f "$HOME/.config/systemd/user/radar.timer" "$HOME/.config/systemd/user/radar.service" "$HOME/.config/systemd/user/radar-digest.timer" "$HOME/.config/systemd/user/radar-digest.service"
systemctl --user daemon-reload 2>/dev/null || true
rm -f "$HOME/.local/bin/radar" "$HOME/.local/share/applications/radar.desktop"
rm -rf "$HOME/.local/share/radar"
if [ "$1" = "--all" ]; then
    rm -rf "$HOME/.config/radar"
    echo "Radar і всі його налаштування видалено."
else
    echo "Radar видалено. Ваші видання й теми збережено в ~/.config/radar"
fi
