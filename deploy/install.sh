#!/usr/bin/env bash
# Apex Trade AI - one-shot installer for a fresh Ubuntu 24.04 VPS.
#   sudo bash install.sh
# Sets up: the bot as a service that restarts by itself, the dashboard behind HTTPS + password
# (Caddy, free certificate via <ip>.sslip.io), and a firewall. Safe to run again.
set -euo pipefail

REPO="khodabandelumahdi5-max/apex-trade-ai"
APP_USER=apex
APP_DIR=/opt/apex-trade-ai

[[ $EUID -eq 0 ]] || { echo "Run as root:  sudo bash install.sh"; exit 1; }
. /etc/os-release
[[ "${ID:-}" == "ubuntu" ]] || echo "Warning: written for Ubuntu 24.04; continuing on ${PRETTY_NAME:-unknown}"

echo "== Apex Trade AI installer =="
if [[ ! -d $APP_DIR/.git ]]; then
  read -rsp "GitHub token (read-only access to $REPO): " GH_TOKEN; echo
  [[ -n "$GH_TOKEN" ]] || { echo "A token is required for the private repository."; exit 1; }
fi
read -rp  "Dashboard username [apex]: " DASH_USER; DASH_USER=${DASH_USER:-apex}
while :; do
  read -rsp "Dashboard password (min 10 chars): " DASH_PASS; echo
  [[ ${#DASH_PASS} -ge 10 ]] && break || echo "Too short."
done
read -rsp "Helius API key (Enter to skip): " HELIUS_KEY; echo

echo "[1/6] System packages..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip git ufw curl gnupg debian-keyring \
  debian-archive-keyring apt-transport-https >/dev/null
if ! command -v caddy >/dev/null; then
  curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/gpg.key \
    | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -qq && apt-get install -y -qq caddy >/dev/null
fi

echo "[2/6] Code..."
id -u $APP_USER &>/dev/null || useradd --system --create-home --shell /usr/sbin/nologin $APP_USER
if [[ ! -d $APP_DIR/.git ]]; then
  git clone -q "https://x-access-token:${GH_TOKEN}@github.com/${REPO}.git" $APP_DIR
fi
chmod 600 $APP_DIR/.git/config          # holds the token used by update.sh
chown -R $APP_USER:$APP_USER $APP_DIR

echo "[3/6] Python environment (a few minutes)..."
sudo -u $APP_USER python3 -m venv $APP_DIR/.venv
sudo -u $APP_USER $APP_DIR/.venv/bin/pip install -q --upgrade pip
sudo -u $APP_USER $APP_DIR/.venv/bin/pip install -q -r $APP_DIR/requirements.txt

echo "[4/6] Settings..."
ENV=$APP_DIR/.env
if [[ ! -f $ENV ]]; then
  cp $APP_DIR/.env.example $ENV
  sed -i 's/^EXECUTION_VENUE=.*/EXECUTION_VENUE=mexc/' $ENV
fi
[[ -n "$HELIUS_KEY" ]] && sed -i "s/^HELIUS_API_KEY=.*/HELIUS_API_KEY=${HELIUS_KEY}/" $ENV
chown $APP_USER:$APP_USER $ENV && chmod 600 $ENV

echo "[5/6] Services..."
cat > /etc/systemd/system/apex-engine.service <<EOF
[Unit]
Description=Apex Trade AI engine
After=network-online.target
Wants=network-online.target

[Service]
User=$APP_USER
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/.venv/bin/python main.py
Restart=always
RestartSec=10
KillSignal=SIGINT
TimeoutStopSec=60

[Install]
WantedBy=multi-user.target
EOF
cat > /etc/systemd/system/apex-dashboard.service <<EOF
[Unit]
Description=Apex Trade AI dashboard
After=network-online.target apex-engine.service

[Service]
User=$APP_USER
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/.venv/bin/streamlit run dashboard.py --server.address 127.0.0.1 --server.port 8501 --server.headless true
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

echo "[6/6] HTTPS dashboard + firewall..."
PUBLIC_IP=$(curl -fsS https://api.ipify.org)
HOST="${PUBLIC_IP//./-}.sslip.io"
HASH=$(caddy hash-password --plaintext "$DASH_PASS")
cat > /etc/caddy/Caddyfile <<EOF
$HOST {
	basic_auth {
		$DASH_USER $HASH
	}
	reverse_proxy 127.0.0.1:8501
}
EOF
ufw allow OpenSSH >/dev/null && ufw allow 80/tcp >/dev/null && ufw allow 443/tcp >/dev/null
ufw --force enable >/dev/null

systemctl daemon-reload
systemctl enable --now apex-engine apex-dashboard >/dev/null
systemctl restart caddy apex-engine apex-dashboard

cat <<EOF

================  DONE  ================
Dashboard : https://$HOST   (user: $DASH_USER)
            the HTTPS certificate can take 1-2 minutes on first visit
Server IP : $PUBLIC_IP   <- put this in the MEXC API key IP whitelist

Edit settings : sudo nano $ENV   then   sudo systemctl restart apex-engine
Bot log (live): sudo journalctl -u apex-engine -f
Check MEXC key: sudo -u $APP_USER bash -c 'cd $APP_DIR && .venv/bin/python check_mexc.py'
Update bot    : sudo bash $APP_DIR/deploy/update.sh
Mode is PAPER until you change TRADING_MODE in .env.
EOF
