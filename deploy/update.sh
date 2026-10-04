#!/usr/bin/env bash
# Pull the latest code, refresh libraries, restart. Keeps .env and the database.
set -euo pipefail
APP_DIR=/opt/apex-trade-ai
[[ $EUID -eq 0 ]] || { echo "Run as root:  sudo bash update.sh"; exit 1; }
sudo -u apex git -C $APP_DIR pull --ff-only
sudo -u apex $APP_DIR/.venv/bin/pip install -q -r $APP_DIR/requirements.txt
systemctl restart apex-engine apex-dashboard
sleep 3
systemctl --no-pager --lines=0 status apex-engine apex-dashboard | grep -E "●|Active:"
echo "Updated to $(git -C $APP_DIR log --oneline -1)"
