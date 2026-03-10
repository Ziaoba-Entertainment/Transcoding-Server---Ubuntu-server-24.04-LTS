#!/bin/bash
# install.sh - Stream-Ziaoba Transcoder Pipeline Maintenance Script
# This script handles installation, upgrades, and system maintenance.

set -e

# --- CONFIGURATION ---
ENV_FILE="/etc/transcoder/env"
INSTALL_DIR="/opt/transcoder"
LOG_DIR="/var/log/transcoder"
VOD_DIR="/srv/vod"
RAW_DIR="/srv/media_raw"
BACKUP_DIR="/opt/transcoder_backups/$(date +%Y%m%d_%H%M%S)"

echo "=== Transcoder Pipeline: Maintenance & Update ==="
echo "Target: $INSTALL_DIR"
echo "Backup: $BACKUP_DIR"

# 0. Pre-flight Checks
if [[ $EUID -ne 0 ]]; then
   echo "This script must be run as root (use sudo)"
   exit 1
fi

# 1. Create Backup
echo "Backing up current installation..."
mkdir -p "$BACKUP_DIR"
if [ -d "$INSTALL_DIR" ]; then
    cp -r "$INSTALL_DIR" "$BACKUP_DIR/opt_transcoder" || true
fi
if [ -f "/etc/nginx/sites-available/mediaserver" ]; then
    cp "/etc/nginx/sites-available/mediaserver" "$BACKUP_DIR/nginx_config" || true
fi

# 2. Configuration & Environment
sudo mkdir -p /etc/transcoder
sudo touch "$ENV_FILE"
sudo chmod 600 "$ENV_FILE"

# Load existing environment
if [ -f "$ENV_FILE" ]; then
    source "$ENV_FILE"
fi

# 3. Install/Update System Dependencies
echo "Updating system dependencies..."
apt-get update -qq
apt-get install -y -qq ffmpeg redis-server python3-pip python3-venv libva-drm2 mesa-va-drivers vainfo nginx samba smbclient curl uuid-runtime > /dev/null

# 4. Redis Configuration
REDIS_HOST=${REDIS_HOST:-"192.168.0.103"}
REDIS_PORT=${REDIS_PORT:-"6379"}
REDIS_PASSWORD=${REDIS_PASSWORD:-"TranscoderRedis2024!"}

# Update environment file atomically
cat <<EOF > /tmp/transcoder_env
REDIS_HOST=$REDIS_HOST
REDIS_PORT=$REDIS_PORT
REDIS_PASSWORD=$REDIS_PASSWORD
EOF
sudo mv /tmp/transcoder_env "$ENV_FILE"
sudo chmod 600 "$ENV_FILE"

# Configure local Redis if applicable
if [[ "$REDIS_HOST" == "127.0.0.1" || "$REDIS_HOST" == "localhost" || "$REDIS_HOST" == "$(hostname -I | awk '{print $1}')" ]]; then
    echo "Configuring local Redis..."
    sed -i "/^requirepass /d" /etc/redis/redis.conf
    echo "requirepass $REDIS_PASSWORD" >> /etc/redis/redis.conf
    systemctl restart redis-server
fi

# 5. User & Permission Management
echo "Syncing users and permissions..."
for user in "media" "transcoder"; do
    if ! id "$user" &>/dev/null; then
        if [ "$user" == "media" ]; then
            useradd -m -s /bin/bash media
        else
            useradd -M -s /sbin/nologin transcoder
        fi
    fi
done

# Sudoers management
if [ -f "transcoder.sudoers" ]; then
    cp transcoder.sudoers /etc/sudoers.d/transcoder
    chmod 0440 /etc/sudoers.d/transcoder
fi

# Groups
usermod -aG video media
usermod -aG render media
usermod -aG www-data media

# 6. Directory Structure
echo "Enforcing directory structure..."
DIRS=(
    "$RAW_DIR/movies" "$RAW_DIR/tv"
    "$VOD_DIR/hls/movies" "$VOD_DIR/hls/tv" "$VOD_DIR/ads"
    "/srv/downloads/movies" "/srv/downloads/tv" "/srv/downloads/ads"
    "$LOG_DIR" "$INSTALL_DIR" "/mnt/win_worker"
)
for dir in "${DIRS[@]}"; do
    mkdir -p "$dir"
done

# Permissions
chown -R media:www-data "$VOD_DIR"
chown -R media:media "$RAW_DIR" "/srv/downloads" "$LOG_DIR" "$INSTALL_DIR" "/mnt/win_worker"
chmod -R 775 "$VOD_DIR" "/mnt/win_worker"
chmod -R 755 "$RAW_DIR" "/srv/downloads" "$LOG_DIR"

# 7. Samba Configuration
echo "Updating Samba shares..."
if [ -f "/etc/samba/smb.conf" ]; then
    (echo "TranscoderSMB2024!"; echo "TranscoderSMB2024!") | smbpasswd -a -s transcoder
    
    # Idempotent share addition
    if ! grep -q "\[media_raw\]" /etc/samba/smb.conf; then
        cat <<EOF >> /etc/samba/smb.conf

[media_raw]
    path = $RAW_DIR
    browseable = yes
    read only = yes
    guest ok = no
    valid users = transcoder
    force user = media

[win_output]
    path = /mnt/win_worker
    browseable = yes
    read only = no
    guest ok = no
    valid users = transcoder
    force user = media
    create mask = 0775
    directory mask = 0775
EOF
        systemctl restart smbd
    fi
fi

# 8. Python Environment
echo "Updating Python virtual environment..."
if [ ! -d "$INSTALL_DIR/venv" ]; then
    sudo -u media python3 -m venv "$INSTALL_DIR/venv"
fi
sudo -u media "$INSTALL_DIR/venv/bin/pip" install -q --upgrade pip
sudo -u media "$INSTALL_DIR/venv/bin/pip" install -q flask flask-cors redis psutil werkzeug python-magic watchdog

# 9. Deploy Scripts
echo "Deploying application scripts..."
SCRIPTS=(
    "config.py" "transcoder_worker.py" "webhook_receiver.py" 
    "folder_scanner.py" "webui.py" "auto_requeue.py" 
    "job_router.py" "win_output_watcher.py" "check_vaapi.sh" 
    "windows_worker_setup.bat" "force_win.py"
)
for script in "${SCRIPTS[@]}"; do
    if [ -f "$script" ]; then
        cp "$script" "$INSTALL_DIR/"
    fi
done
chmod +x "$INSTALL_DIR/check_vaapi.sh"
chown -R media:media "$INSTALL_DIR"

# 10. System Services
echo "Deploying systemd units..."
SERVICES=(
    "transcoder-worker.service" "transcoder-webhook.service"
    "transcoder-webui.service" "transcoder-scanner.service"
    "transcoder-router.service" "transcoder-win-watcher.service"
    "transcoder-ad-stitcher.service" "transcoder-ad-admin.service"
)
for service in "${SERVICES[@]}"; do
    if [ -f "$service" ]; then
        cp "$service" /etc/systemd/system/
    fi
done

# Logrotate
if [ -f "transcoder.logrotate" ]; then
    cp transcoder.logrotate /etc/logrotate.d/transcoder
fi

# Nginx
if [ -f "mediaserver.conf" ]; then
    cp mediaserver.conf /etc/nginx/sites-available/mediaserver
    rm -f /etc/nginx/sites-enabled/default
    ln -sf /etc/nginx/sites-available/mediaserver /etc/nginx/sites-enabled/
    nginx -t && systemctl restart nginx
fi

systemctl daemon-reload

# 11. Firewall
if command -v ufw > /dev/null; then
    ufw allow 80/tcp > /dev/null
    ufw allow 6379/tcp > /dev/null # Redis for workers
    ufw allow 445/tcp > /dev/null  # Samba
fi

# 12. Service Startup
echo "Restarting services..."
for service in "${SERVICES[@]}"; do
    if [ -f "/etc/systemd/system/$service" ]; then
        name=$(basename "$service" .service)
        systemctl enable "$name" > /dev/null 2>&1
        systemctl restart "$name"
    fi
done

# 13. Health Check
echo "Performing health check..."
sleep 3
HEALTH_OK=true
if ! curl -s http://127.0.0.1/transcoder/ > /dev/null; then
    echo "WARNING: Web UI not responding on port 80"
    HEALTH_OK=false
fi

if ! redis-cli -h "$REDIS_HOST" -p "$REDIS_PORT" -a "$REDIS_PASSWORD" ping | grep -q "PONG"; then
    echo "WARNING: Redis connectivity failed"
    HEALTH_OK=false
fi

# 14. Final Report
echo "-------------------------------------------------------"
if [ "$HEALTH_OK" = true ]; then
    echo "SUCCESS: Pipeline updated and healthy."
else
    echo "NOTICE: Pipeline updated but health checks failed. Check logs."
fi
echo "Dashboard: http://$(hostname -I | awk '{print $1}')/transcoder/"
echo "-------------------------------------------------------"
