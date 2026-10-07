#!/usr/bin/env bash
# Sets up SOFA (shopping and search) on a fresh Ubuntu 22.04 or 24.04 server, with automatic HTTPS.
#
#   sudo bash setup_server.sh            first install
#   sudo bash setup_server.sh update     pull the latest code and restart
#
# Optional settings, given before the command:
#   SITE_HOST=sofa.example.com   use your own domain (its DNS must already point at this server)
#   REPO=https://github.com/...  use another copy of the code
#
# With no domain, the script uses <ip-with-dashes>.sslip.io, a free name that points at this server's IP, so HTTPS works at once.
# Ports 80 and 443 must be open in the cloud provider's security group.
set -euo pipefail

REPO="${REPO:-https://github.com/YakubuYinusaT/SOFA-Public.git}"
APP=/opt/sofa
SRC="$APP/src"

[[ $EUID -eq 0 ]] || { echo "Run this as root: sudo bash $0"; exit 1; }

# SOFA needs Python 3.11 or newer. Ubuntu 24.04 has it; Ubuntu 22.04 ships 3.10, so a newer one is installed beside it.
python_ok() { "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; }
PY=python3
python_ok python3 || PY=python3.12

install_code() {
  if [[ -d "$SRC/.git" ]]; then
    git -C "$SRC" pull --ff-only
  else
    git clone "$REPO" "$SRC"
  fi
  [[ -d "$APP/venv" ]] || "$PY" -m venv "$APP/venv"
  "$APP/venv/bin/pip" install --quiet --upgrade pip
  "$APP/venv/bin/pip" install --quiet -e "$SRC"
  chown -R sofa:sofa "$APP"
}

if [[ "${1:-}" == "update" ]]; then
  install_code
  systemctl restart sofa
  echo "Updated and restarted."
  exit 0
fi

. /etc/os-release
case "${VERSION_ID:-}" in
  22.04|24.04) ;;
  *) echo "Note: this script was written for Ubuntu 22.04 and 24.04 (this is ${PRETTY_NAME:-unknown}); it may still work." ;;
esac

echo "1/6 Installing packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq git ffmpeg ufw curl gpg ca-certificates software-properties-common debian-keyring debian-archive-keyring apt-transport-https python3 python3-venv

if ! python_ok python3; then
  echo "  adding a newer Python (3.12) because this system's is too old"
  add-apt-repository -y ppa:deadsnakes/ppa >/dev/null
  apt-get update -qq
  apt-get install -y -qq python3.12 python3.12-venv python3.12-dev
  PY=python3.12
fi

if ! command -v caddy >/dev/null 2>&1; then
  echo "  adding Caddy (the web server that gets the HTTPS certificate)"
  curl -fsSL https://dl.cloudsmith.io/public/caddy/stable/gpg.key | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -fsSL https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt >/etc/apt/sources.list.d/caddy-stable.list
  apt-get update -qq
  apt-get install -y -qq caddy
fi

echo "2/6 Creating the service user and fetching the code"
id sofa >/dev/null 2>&1 || useradd --system --home "$APP" --shell /usr/sbin/nologin sofa
mkdir -p "$APP/data"
install_code

IP="$(curl -fsS https://api.ipify.org)"
HOST="${SITE_HOST:-${IP//./-}.sslip.io}"

echo "3/6 Writing the settings (only if they do not exist yet)"
if [[ ! -f "$SRC/.env" ]]; then
  rand() { tr -dc 'A-Za-z0-9' </dev/urandom | head -c 20; }
  ADMIN_PW="$(rand)"; VENDOR_PW="$(rand)"
  cat >"$SRC/.env" <<EOF
SERVICES_ENABLED=commerce,lookup
DEMO_PAGE_ENABLED=true
PUBLIC_BASE_URL=https://$HOST
DATABASE_URL=sqlite:///$APP/data/sofa.db
STORAGE_DIR=$APP/data
ADMIN_TOKEN=$ADMIN_PW
VENDOR_TOKEN=$VENDOR_PW
AT_CALLBACK_SECRET=$(rand)
EOF
  chmod 600 "$SRC/.env"
  chown sofa:sofa "$SRC/.env"
  echo "$ADMIN_PW" >"$APP/first-run-admin-password.txt"
  echo "$VENDOR_PW" >"$APP/first-run-vendor-password.txt"
  chmod 600 "$APP"/first-run-*.txt
else
  echo "  .env already exists: left unchanged"
fi

echo "4/6 Loading the store's farm inputs and advice"
sudo -u sofa -H bash -c "cd '$SRC' && '$APP/venv/bin/python' -m scripts.seed_agro" || true

echo "5/6 Starting SOFA as a service"
cat >/etc/systemd/system/sofa.service <<EOF
[Unit]
Description=SOFA voice assistant
After=network.target

[Service]
User=sofa
WorkingDirectory=$SRC
ExecStart=$APP/venv/bin/uvicorn sofa.main:create_app --factory --host 127.0.0.1 --port 8000 --workers 1
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now sofa

echo "6/6 HTTPS and firewall"
cat >/etc/caddy/Caddyfile <<EOF
$HOST {
    encode gzip
    reverse_proxy 127.0.0.1:8000
}
EOF
systemctl restart caddy
ufw allow OpenSSH >/dev/null
ufw allow 80/tcp >/dev/null
ufw allow 443/tcp >/dev/null
ufw --force enable >/dev/null

sleep 5
echo
if curl -fsS "https://$HOST/health" >/dev/null 2>&1; then
  echo "SOFA is running."
else
  echo "SOFA is installed but https://$HOST/health did not answer yet. Wait a minute and try again; if it still fails: journalctl -u sofa -n 50"
fi
echo
echo "Live page:        https://$HOST/demo"
echo "Store portal:     https://$HOST/vendor"
echo "Team console:     https://$HOST/admin"
echo "Passwords are in $APP/first-run-admin-password.txt and $APP/first-run-vendor-password.txt (read them with: sudo cat <file>; then keep them somewhere safe and delete the files)."
