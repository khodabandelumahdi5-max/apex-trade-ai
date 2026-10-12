#!/usr/bin/env bash
# AlphaDuel / Apex Trade AI - one-shot installer for a fresh Ubuntu 22.04 or 24.04 server.
#
#   curl -fsSL https://raw.githubusercontent.com/khodabandelumahdi5-max/apex-trade-ai/main/deploy/install.sh -o install.sh
#   sudo bash install.sh
#
# Sets up, under /opt/alphaduel:
#   - system packages, firewall (only SSH, HTTP, HTTPS open)
#   - Python virtual environment with all libraries
#   - systemd services that start on boot and restart themselves on failure:
#       alphaduel-engine    trading engine (paper mode until you change TRADING_MODE)
#       alphaduel-dashboard Streamlit dashboard (behind HTTPS + password)
#       alphaduel-bot       Telegram bot + Mini App (only when a bot token is given)
#   - HTTPS with free certificates via Caddy and <ip>.sslip.io
#   - a health check of every service and its logs at the end
# Secrets are typed here, on the server, and stored only in /opt/alphaduel/.env (chmod 600). Safe to run again.
set -euo pipefail

REPO="khodabandelumahdi5-max/apex-trade-ai"
APP_USER=alphaduel
APP_DIR=/opt/alphaduel
ENV=$APP_DIR/.env

[[ $EUID -eq 0 ]] || { echo "Run as root:  sudo bash install.sh"; exit 1; }
. /etc/os-release
[[ "${ID:-}" == "ubuntu" ]] || echo "Warning: written for Ubuntu 22.04/24.04; continuing on ${PRETTY_NAME:-unknown}"

# set_env KEY VALUE: replace or append a line in .env without sed/regex surprises from special characters
set_env() {
  KEY="$1" VAL="$2" python3 - "$ENV" <<'PY'
import os, sys
path, key, val = sys.argv[1], os.environ["KEY"], os.environ["VAL"]
lines = open(path).read().splitlines() if os.path.exists(path) else []
out, done = [], False
for line in lines:
    if line.startswith(key + "="):
        if not done:
            out.append(f"{key}={val}"); done = True
    else:
        out.append(line)
if not done:
    out.append(f"{key}={val}")
open(path, "w").write("\n".join(out) + "\n")
PY
}
get_env() { if [[ -f $ENV ]]; then grep -m1 "^$1=" "$ENV" | cut -d= -f2- || true; fi; }

echo "================ AlphaDuel installer ================"
# minimal images ship without curl; it is needed already for checking the bot token below
if ! command -v curl >/dev/null || ! command -v python3 >/dev/null; then
  echo "Installing curl / python3 first..."
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq && apt-get install -y -qq curl python3 >/dev/null
fi
read -rp  "Dashboard username [admin]: " DASH_USER; DASH_USER=${DASH_USER:-admin}
while :; do
  read -rsp "Dashboard password (min 10 chars): " DASH_PASS; echo
  [[ ${#DASH_PASS} -ge 10 ]] && break || echo "Too short."
done
tg_api() { curl -fsS -m 30 "https://api.telegram.org/bot$1/$2" 2>/dev/null || true; }
TG_TOKEN=""; ADMIN_ID=""; BOTNAME=""
if [[ -z "$(get_env TELEGRAM_BOT_TOKEN)" ]]; then
  # shown while typing: in a browser console the token usually has to be typed by hand
  while :; do
    read -rp "Telegram bot token from @BotFather (Enter to skip): " TG_TOKEN
    TG_TOKEN=$(echo "$TG_TOKEN" | tr -d '[:space:]')
    [[ -z "$TG_TOKEN" ]] && break
    ME=$(tg_api "$TG_TOKEN" getMe)
    if [[ "$ME" == *'"ok":true'* ]]; then
      BOTNAME=$(echo "$ME" | python3 -c 'import json,sys; print(json.load(sys.stdin)["result"]["username"])')
      echo "  OK: token belongs to @$BOTNAME"
      break
    fi
    echo "  This token was rejected by Telegram (typo?). Check it in @BotFather and type it again."
  done
  if [[ -n "$TG_TOKEN" ]]; then
    echo "  Now open @$BOTNAME in Telegram and send it any message (e.g. hi). Waiting up to 2 minutes..."
    for _ in $(seq 1 12); do
      ADMIN_ID=$(tg_api "$TG_TOKEN" "getUpdates?timeout=10" | python3 -c '
import json, sys
try:
    upd = json.load(sys.stdin).get("result", [])
except Exception:
    upd = []
ids = [u["message"]["from"]["id"] for u in upd if u.get("message", {}).get("chat", {}).get("type") == "private"]
print(ids[-1] if ids else "")')
      [[ -n "$ADMIN_ID" ]] && { echo "  OK: your Telegram id is $ADMIN_ID"; break; }
    done
  fi
else
  echo "Telegram bot token: already set in $ENV (kept)"
fi
if [[ -z "$ADMIN_ID" ]]; then
  read -rp  "Your Telegram numeric id for the admin panel (from @userinfobot, Enter to skip): " ADMIN_ID
fi
read -rp  "Your trading budget in USDT for signal sizes [100]: " BUDGET
read -rp  "Subscription price in Telegram Stars (Enter to skip): " STARS
read -rp  "Support contact shown by /paysupport, e.g. @yourname (Enter to skip): " SUPPORT

echo "[1/7] System packages..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get -y -qq -o Dpkg::Options::=--force-confold upgrade >/dev/null
apt-get install -y -qq python3 python3-venv python3-pip git ufw curl gnupg debian-keyring \
  debian-archive-keyring apt-transport-https >/dev/null
if ! command -v caddy >/dev/null; then
  curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/gpg.key \
    | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -qq && apt-get install -y -qq caddy >/dev/null
fi
timedatectl set-ntp true 2>/dev/null || true          # correct time matters for Telegram auth and daily limits

echo "[2/7] Code -> $APP_DIR ..."
id -u $APP_USER &>/dev/null || useradd --system --create-home --shell /usr/sbin/nologin $APP_USER
if [[ ! -d $APP_DIR/.git ]]; then
  if ! git clone -q "https://github.com/${REPO}.git" $APP_DIR 2>/dev/null; then
    read -rsp "Repository is private - GitHub token with read access: " GH_TOKEN; echo
    git clone -q "https://x-access-token:${GH_TOKEN}@github.com/${REPO}.git" $APP_DIR
    chmod 600 $APP_DIR/.git/config                    # holds the token used by update.sh
  fi
else
  sudo -u $APP_USER git -C $APP_DIR pull -q --ff-only || echo "  (could not pull; keeping current code)"
fi
mkdir -p $APP_DIR/logs
chown -R $APP_USER:$APP_USER $APP_DIR

echo "[3/7] Python environment (a few minutes)..."
sudo -u $APP_USER python3 -m venv $APP_DIR/.venv
sudo -u $APP_USER $APP_DIR/.venv/bin/pip install -q --upgrade pip
sudo -u $APP_USER $APP_DIR/.venv/bin/pip install -q -r $APP_DIR/requirements.txt

echo "[4/7] Settings ($ENV)..."
PUBLIC_IP=$(curl -fsS https://api.ipify.org || hostname -I | awk '{print $1}')
HOST="${PUBLIC_IP//./-}.sslip.io"
if [[ ! -f $ENV ]]; then
  cp $APP_DIR/.env.example $ENV
  set_env EXECUTION_VENUE mexc
fi
if [[ -z "$(get_env DB_ENCRYPTION_KEY)" ]]; then
  set_env DB_ENCRYPTION_KEY "$($APP_DIR/.venv/bin/python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"
fi
if [[ -n "$TG_TOKEN" ]]; then set_env TELEGRAM_BOT_TOKEN "$TG_TOKEN"; fi
if [[ -n "$ADMIN_ID" ]]; then set_env ADMIN_TELEGRAM_IDS "$ADMIN_ID"; fi
if [[ -n "$BOTNAME" ]]; then set_env BOT_USERNAME "$BOTNAME"; fi
set_env SIGNAL_BUDGET_USD "${BUDGET:-100}"
if [[ -n "$STARS" ]]; then set_env STARS_PRICE "$STARS"; fi
if [[ -n "$SUPPORT" ]]; then set_env SUPPORT_CONTACT "$SUPPORT"; fi
set_env MINIAPP_URL "https://app.$HOST"
set_env MINIAPP_HOST 127.0.0.1
set_env MINIAPP_PORT 8080
chown $APP_USER:$APP_USER $ENV && chmod 600 $ENV

echo "[5/7] Services..."
# remove services of an older install under the previous names
for old in apex-engine apex-dashboard apex-tgbot apex-miniapp; do
  if [[ -f /etc/systemd/system/$old.service ]]; then
    systemctl disable --now $old >/dev/null 2>&1 || true
    rm -f /etc/systemd/system/$old.service
  fi
done
unit() {  # unit NAME DESCRIPTION EXEC [extra [Service] lines]
  cat > /etc/systemd/system/$1.service <<EOF
[Unit]
Description=$2
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=0

[Service]
User=$APP_USER
WorkingDirectory=$APP_DIR
ExecStart=$3
Restart=always
RestartSec=10
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
${4:-}

[Install]
WantedBy=multi-user.target
EOF
}
unit alphaduel-engine "AlphaDuel trading engine" "$APP_DIR/.venv/bin/python main.py" $'KillSignal=SIGINT\nTimeoutStopSec=60'
unit alphaduel-dashboard "AlphaDuel dashboard" \
  "$APP_DIR/.venv/bin/streamlit run dashboard.py --server.address 127.0.0.1 --server.port 8501 --server.headless true"
unit alphaduel-bot "AlphaDuel Telegram bot + Mini App" "$APP_DIR/.venv/bin/python -m tgbot" $'KillSignal=SIGINT\nTimeoutStopSec=30'

echo "[6/7] HTTPS + firewall..."
HASH=$(caddy hash-password --plaintext "$DASH_PASS")
cat > /etc/caddy/Caddyfile <<EOF
$HOST {
	basic_auth {
		$DASH_USER $HASH
	}
	reverse_proxy 127.0.0.1:8501
}
app.$HOST {
	reverse_proxy 127.0.0.1:8080
}
EOF
ufw allow OpenSSH >/dev/null && ufw allow 80/tcp >/dev/null && ufw allow 443/tcp >/dev/null
ufw --force enable >/dev/null

systemctl daemon-reload
SERVICES="alphaduel-engine alphaduel-dashboard"
if [[ -n "$(get_env TELEGRAM_BOT_TOKEN)" ]]; then
  SERVICES="$SERVICES alphaduel-bot"
else
  systemctl disable --now alphaduel-bot >/dev/null 2>&1 || true
fi
systemctl enable $SERVICES >/dev/null 2>&1
systemctl restart caddy $SERVICES

echo "[7/7] Health check (waiting 40 s for everything to start)..."
sleep 40
FAIL=0
for s in $SERVICES caddy; do
  state=$(systemctl is-active $s || true)
  restarts=$(systemctl show -p NRestarts --value $s 2>/dev/null || echo 0)
  errors=$(journalctl -u $s --since "-2min" --no-pager 2>/dev/null | grep -Eic "traceback|error|exception" || true)
  if [[ "$state" == "active" && "${restarts:-0}" == "0" ]]; then
    printf "  OK    %-22s (log lines mentioning errors: %s)\n" "$s" "$errors"
  else
    printf "  FAIL  %-22s state=%s restarts=%s\n" "$s" "$state" "$restarts"; FAIL=1
    journalctl -u $s -n 25 --no-pager | sed 's/^/        /'
  fi
done
if curl -fsS -o /dev/null http://127.0.0.1:8501; then echo "  OK    dashboard answers on 8501"; else echo "  FAIL  dashboard not answering"; FAIL=1; fi
if [[ " $SERVICES " == *" alphaduel-bot "* ]]; then
  if curl -fsS -o /dev/null http://127.0.0.1:8080/api/challenges; then echo "  OK    Mini App API answers on 8080"
  else echo "  FAIL  Mini App API not answering"; FAIL=1; fi
  journalctl -u alphaduel-bot --since "-2min" --no-pager | grep -m1 "Running as @" | sed 's/.*Running as/  OK    Telegram: running as/' || true
fi

cat <<EOF

================  $([[ $FAIL == 0 ]] && echo DONE || echo "DONE WITH PROBLEMS - see FAIL lines above")  ================
Dashboard : https://$HOST          (user: $DASH_USER)
Mini App  : https://app.$HOST      (set as the bot's menu button automatically)
Server IP : $PUBLIC_IP   <- MEXC API key IP whitelist, if you ever add a key
            HTTPS certificates can take 1-2 minutes on the first visit.

Live logs     : journalctl -u alphaduel-bot -f      (or alphaduel-engine / alphaduel-dashboard)
Status        : systemctl status alphaduel-engine alphaduel-dashboard alphaduel-bot
Edit settings : nano $ENV   then   systemctl restart alphaduel-bot alphaduel-engine
First challenge: sudo -u $APP_USER bash -c 'cd $APP_DIR && .venv/bin/python -m competition create "AlphaDuel 21-day" SOL,JUP 21 "prize"'
Update        : bash $APP_DIR/deploy/update.sh
Back up .env (DB_ENCRYPTION_KEY) and apex.db somewhere safe. Trading mode stays PAPER until you change TRADING_MODE.
EOF
