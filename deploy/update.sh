#!/usr/bin/env bash
# Pull the latest code, refresh libraries, restart the services. Keeps .env and the database.
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "Run as root:  sudo bash update.sh"; exit 1; }
if [[ -d /opt/alphaduel/.git ]]; then
  APP_DIR=/opt/alphaduel; APP_USER=alphaduel; SERVICES="alphaduel-engine alphaduel-dashboard alphaduel-bot"
else
  APP_DIR=/opt/apex-trade-ai; APP_USER=apex; SERVICES="apex-engine apex-dashboard apex-tgbot"
fi
sudo -u $APP_USER git -C $APP_DIR pull --ff-only
sudo -u $APP_USER $APP_DIR/.venv/bin/pip install -q -r $APP_DIR/requirements.txt
for s in $SERVICES; do
  if systemctl is-enabled --quiet $s 2>/dev/null; then systemctl restart $s; fi
done
sleep 5
for s in $SERVICES; do
  if systemctl is-enabled --quiet $s 2>/dev/null; then printf "%-22s %s\n" "$s" "$(systemctl is-active $s)"; fi
done
echo "Updated to $(git -C $APP_DIR log --oneline -1)"
