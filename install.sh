#!/bin/bash
# install.sh

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "Starting Transcoder Pipeline Installation (full + distributed updates)..."

# 1. Install System Dependencies
# Includes legacy stack + distributed additions (Samba/CIFS/Redis CLI tools)
echo "Installing system dependencies..."
sudo apt-get update
sudo apt-get install -y \
    ffmpeg redis-server redis-tools \
    python3-pip python3-venv \
    libva-drm2 mesa-va-drivers vainfo \
    nginx curl \
    samba smbclient cifs-utils

# 2. Create/Update media user and groups
if ! id "media" &>/dev/null; then
    echo "Creating media user..."
    sudo useradd -m -s /bin/bash media
fi

sudo usermod -aG video media || true
sudo usermod -aG render media || true
sudo usermod -aG www-data media || true

# 3. Create/verify directory structure (legacy + distributed)
echo "Creating directory structure..."
DIRS=(
    "/srv/media_raw/movies"
    "/srv/media_raw/tv"
    "/srv/vod/hls/movies"
    "/srv/vod/hls/tv"
    "/srv/vod/ads"
    "/srv/downloads/movies"
    "/srv/downloads/tv"
    "/srv/downloads/ads"
    "/var/log/transcoder"
    "/opt/transcoder"
    "/mnt/win_worker"
)

for dir in "${DIRS[@]}"; do
    sudo mkdir -p "$dir"
done

# Permissions
sudo chown -R media:www-data /srv/vod
sudo chown -R media:media /srv/media_raw /srv/downloads /var/log/transcoder /opt/transcoder
sudo chmod -R 775 /srv/vod
sudo chmod -R 755 /srv/media_raw /srv/downloads /var/log/transcoder

# 4. Read Redis password from env/config (if set)
REDIS_PASSWORD="${REDIS_PASSWORD:-}"
if [[ -z "$REDIS_PASSWORD" && -f "$REPO_DIR/config.py" ]]; then
    REDIS_PASSWORD="$(python3 - <<'PY'
import os
import re
from pathlib import Path
content = Path('config.py').read_text()
m = re.search(r"REDIS_PASSWORD\s*=\s*['\"]([^'\"]*)['\"]", content)
print(m.group(1) if m else "")
PY
)"
fi

# 5. Configure Redis for local + LAN distributed routing
# This preserves existing config lines while enforcing required values.
REDIS_CONF="/etc/redis/redis.conf"
if [[ -f "$REDIS_CONF" ]]; then
    echo "Configuring Redis for distributed access..."
    sudo sed -i "s/^bind .*/bind 127.0.0.1 192.168.0.103/" "$REDIS_CONF" || true
    sudo sed -i "s/^protected-mode .*/protected-mode yes/" "$REDIS_CONF" || true

    if [[ -n "$REDIS_PASSWORD" ]]; then
        if grep -qE '^#\s*requirepass\s+' "$REDIS_CONF"; then
            sudo sed -i "s|^#\s*requirepass\s\+.*|requirepass ${REDIS_PASSWORD}|" "$REDIS_CONF"
        elif grep -qE '^requirepass\s+' "$REDIS_CONF"; then
            sudo sed -i "s|^requirepass\s\+.*|requirepass ${REDIS_PASSWORD}|" "$REDIS_CONF"
        else
            echo "requirepass ${REDIS_PASSWORD}" | sudo tee -a "$REDIS_CONF" >/dev/null
        fi
    fi
fi

# 6. Configure Samba shares used by distributed flow
SAMBA_CONF="/etc/samba/smb.conf"
if [[ -f "$SAMBA_CONF" ]]; then
    echo "Configuring Samba shares..."
    sudo cp "$SAMBA_CONF" "${SAMBA_CONF}.bak.$(date +%Y%m%d%H%M%S)"

    if ! grep -q "\[vod\]" "$SAMBA_CONF"; then
        cat <<'SMB' | sudo tee -a "$SAMBA_CONF" >/dev/null

[vod]
path = /srv/vod
browseable = yes
read only = no
guest ok = yes
create mask = 0755
directory mask = 0755

[win_worker]
path = /mnt/win_worker
browseable = yes
read only = no
guest ok = yes
create mask = 0755
directory mask = 0755
SMB
    fi
fi

# 7. Python virtual environment + dependencies
echo "Installing Python dependencies in virtual environment..."
if [[ ! -d /opt/transcoder/venv ]]; then
    sudo -u media python3 -m venv /opt/transcoder/venv
fi

sudo -u media /opt/transcoder/venv/bin/pip install --upgrade pip
sudo -u media /opt/transcoder/venv/bin/pip install --upgrade \
    flask redis psutil werkzeug python-magic watchdog

# 8. Copy scripts to /opt/transcoder (legacy + distributed)
echo "Copying scripts to /opt/transcoder..."
FILES_TO_COPY=(
    config.py
    transcoder_worker.py
    webhook_receiver.py
    folder_scanner.py
    webui.py
    auto_requeue.py
    check_vaapi.sh
    job_router.py
    win_output_watcher.py
    test_distributed.sh
)

for f in "${FILES_TO_COPY[@]}"; do
    if [[ -f "$REPO_DIR/$f" ]]; then
        sudo cp "$REPO_DIR/$f" /opt/transcoder/
    fi
done

sudo chmod +x /opt/transcoder/check_vaapi.sh || true
sudo chmod +x /opt/transcoder/test_distributed.sh || true
sudo chown -R media:media /opt/transcoder

# 9. Install systemd services + Nginx config
echo "Installing systemd services and Nginx config..."
SERVICE_FILES=(
    transcoder-worker.service
    transcoder-webhook.service
    transcoder-webui.service
    transcoder-scanner.service
    transcoder-router.service
    transcoder-win-watcher.service
)

for svc in "${SERVICE_FILES[@]}"; do
    if [[ -f "$REPO_DIR/$svc" ]]; then
        sudo cp "$REPO_DIR/$svc" /etc/systemd/system/
    fi
done

# Configure Nginx
sudo mkdir -p /etc/nginx/snippets
if [[ -f "$REPO_DIR/proxy-params.conf" ]]; then
    sudo cp "$REPO_DIR/proxy-params.conf" /etc/nginx/snippets/proxy-params.conf
fi
if [[ -f "$REPO_DIR/mediaserver.conf" ]]; then
    sudo cp "$REPO_DIR/mediaserver.conf" /etc/nginx/sites-available/mediaserver
    sudo rm -f /etc/nginx/sites-enabled/default
    sudo ln -sf /etc/nginx/sites-available/mediaserver /etc/nginx/sites-enabled/
fi

sudo nginx -t
sudo systemctl restart nginx

# 10. Firewall rules (legacy + distributed)
echo "Configuring firewall..."
if command -v ufw >/dev/null; then
    # Base
    sudo ufw allow 22/tcp || true
    sudo ufw allow 80/tcp || true
    sudo ufw allow 443/tcp || true

    # *arr stack
    sudo ufw allow 7878/tcp || true
    sudo ufw allow 8989/tcp || true
    sudo ufw allow 9696/tcp || true

    # Ad stack + Redis + Samba
    sudo ufw allow 8080/tcp || true
    sudo ufw allow 8081/tcp || true
    sudo ufw allow 8082/tcp || true
    sudo ufw allow 6379/tcp || true
    sudo ufw allow 139/tcp || true
    sudo ufw allow 445/tcp || true
    sudo ufw allow 137/udp || true
    sudo ufw allow 138/udp || true

    sudo ufw --force enable
fi

# 11. Reload + enable/restart services
echo "Reloading systemd and restarting services..."
sudo systemctl daemon-reload

sudo systemctl enable redis-server
sudo systemctl restart redis-server

sudo systemctl enable smbd nmbd || true
sudo systemctl restart smbd nmbd || true

ENABLED_SERVICES=(
    transcoder-webhook
    transcoder-webui
    transcoder-scanner
    transcoder-worker
    transcoder-router
    transcoder-win-watcher
)

for service in "${ENABLED_SERVICES[@]}"; do
    if systemctl list-unit-files | grep -q "^${service}\.service"; then
        sudo systemctl enable "$service"
        sudo systemctl restart "$service"
    fi
done

# 12. Verification checks
echo "Verifying local Web UI connectivity via Nginx..."
sleep 2
if curl -fsS http://127.0.0.1/transcoder/ >/dev/null; then
    echo "Web UI is responding via Nginx on port 80."
else
    echo "WARNING: Web UI is not responding via Nginx. Check /var/log/nginx/mediaserver_error.log"
fi

echo "Verifying Redis auth/access..."
if [[ -n "$REDIS_PASSWORD" ]]; then
    redis-cli -h 127.0.0.1 -p 6379 -a "$REDIS_PASSWORD" PING || true
else
    redis-cli -h 127.0.0.1 -p 6379 PING || true
fi

echo "Verifying VAAPI hardware acceleration..."
if [[ -x /opt/transcoder/check_vaapi.sh ]]; then
    /opt/transcoder/check_vaapi.sh || true
fi

echo "-------------------------------------------------------"
echo "Installation Complete!"
echo "Dashboard: http://$(hostname -I | awk '{print $1}')/transcoder/"
echo "Radarr: http://$(hostname -I | awk '{print $1}')/radarr"
echo "Sonarr: http://$(hostname -I | awk '{print $1}')/sonarr"
echo "Prowlarr: http://$(hostname -I | awk '{print $1}')/prowlarr"
echo "-------------------------------------------------------"
