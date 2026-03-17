#!/bin/bash
# install.sh - Stream-Ziaoba Transcoder Pipeline Maintenance Script
# This script handles installation, upgrades, and system maintenance.
# Updated: 2026-03-13 - v2.5.0 (VOD/Stitched Links Fix, Job Titles, Output Cleanup).

set -e

# --- CONFIGURATION ---
cd "$(dirname "$0")"
ENV_FILE="/etc/transcoder/env"
INSTALL_DIR="/opt/transcoder"
LOG_DIR="/var/log/transcoder"
VOD_DIR="/srv/vod"
RAW_DIR="/srv/media_raw"
BACKUP_DIR="/opt/transcoder_backups/$(date +%Y%m%d_%H%M%S)"
WEBUI_PORT=8081
ADSERVER_PORT=8082

echo "=== Transcoder Pipeline: Maintenance & Update ==="
echo "Target: $INSTALL_DIR"
echo "Backup: $BACKUP_DIR"

# 0. Pre-flight Checks
if [[ $EUID -ne 0 ]]; then
   echo "This script must be run as root (use sudo)"
   exit 1
fi

# Stop Nginx temporarily to check for real port conflicts
echo "Stopping Nginx to check for port conflicts..."
systemctl stop nginx || true

# Check for port conflicts
echo "Checking for port conflicts on $WEBUI_PORT and $ADSERVER_PORT..."
for port in $WEBUI_PORT $ADSERVER_PORT; do
    if ss -tlnp | grep -q ":$port "; then
        CONFLICTING_PROCESS=$(ss -tlnp | grep ":$port " | awk '{print $7}')
        echo "ERROR: Port $port is already in use by: $CONFLICTING_PROCESS"
        echo "Please stop the conflicting service before proceeding."
        systemctl start nginx || true
        exit 1
    fi
done

# Check for swap
if [ "$(free | grep Swap | awk '{print $2}')" -eq 0 ]; then
    echo "WARNING: No swap space detected. FFmpeg may OOM during heavy transcoding. Consider adding a 4GB swap file."
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
mkdir -p /etc/transcoder
touch "$ENV_FILE"
chmod 600 "$ENV_FILE"

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
mv /tmp/transcoder_env "$ENV_FILE"
chmod 600 "$ENV_FILE"

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
    "$VOD_DIR/ads/incoming" "$VOD_DIR/ads/rejected" "$VOD_DIR/output"
    "/srv/downloads/movies" "/srv/downloads/tv" "/srv/downloads/ads"
    "$LOG_DIR" "$INSTALL_DIR" "/mnt/win_worker"
    "$INSTALL_DIR/templates" "/var/log/adserver"
)
for dir in "${DIRS[@]}"; do
    mkdir -p "$dir"
done

# Permissions
chown -R media:www-data "$VOD_DIR"
chown -R media:media "$RAW_DIR" "/srv/downloads" "$LOG_DIR" "$INSTALL_DIR" "/mnt/win_worker" "/var/log/adserver"
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

# 9. Deploy Scripts & Templates
echo "Deploying application scripts and templates..."
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

# Copy templates
if [ -d "templates" ]; then
    mkdir -p "$INSTALL_DIR/templates"
    cp -r templates/* "$INSTALL_DIR/templates/"
fi

chmod +x "$INSTALL_DIR/check_vaapi.sh"
chown -R media:media "$INSTALL_DIR"

# 10. System Services
echo "Deploying systemd units..."
SERVICES=(
    "transcoder-worker.service" "transcoder-webhook.service"
    "transcoder-webui.service" "transcoder-scanner.service"
    "transcoder-router.service" "transcoder-win-watcher.service"
    "adserver.service" "adserver-admin.service" "ad-watcher.service"
    "ad-redis-listener.service"
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
if [ -f "proxy-params.conf" ]; then
    mkdir -p /etc/nginx/snippets
    cp proxy-params.conf /etc/nginx/snippets/proxy-params.conf
fi

if [ -f "mediaserver" ]; then
    echo "Cleaning up old Nginx configurations..."
    rm -f /etc/nginx/sites-enabled/*
    rm -f /etc/nginx/sites-available/mediaserver
    
    cp mediaserver /etc/nginx/sites-available/mediaserver
    ln -sf /etc/nginx/sites-available/mediaserver /etc/nginx/sites-enabled/
    
    echo "Testing Nginx configuration..."
    if nginx -t; then
        systemctl restart nginx
    else
        echo "ERROR: Nginx configuration is invalid. Check /etc/nginx/sites-available/mediaserver"
        systemctl start nginx || true
        exit 1
    fi
fi

systemctl daemon-reload

# 11. Firewall
if command -v ufw > /dev/null; then
    ufw allow $WEBUI_PORT/tcp > /dev/null
    ufw allow $ADSERVER_PORT/tcp > /dev/null
    ufw allow 6379/tcp > /dev/null # Redis for workers
    ufw allow 445/tcp > /dev/null  # Samba
fi

# 12. Service Startup
echo "Restarting services..."

# Check Redis first as it's a dependency
if systemctl is-active --quiet redis-server; then
    echo "  - redis-server is active"
else
    echo "  - Starting redis-server..."
    systemctl restart redis-server
fi

for service in "${SERVICES[@]}"; do
    if [ -f "/etc/systemd/system/$service" ]; then
        name=$(basename "$service" .service)
        echo "  - $name..."
        systemctl enable "$name" > /dev/null 2>&1
        
        # Scanner might take longer on first run
        RESTART_TIMEOUT=30s
        if [ "$name" == "transcoder-scanner" ]; then
            RESTART_TIMEOUT=120s
        fi
        
        if ! timeout $RESTART_TIMEOUT systemctl restart "$name"; then
            echo "    WARNING: $name failed to restart within $RESTART_TIMEOUT"
        fi
    fi
done

# 13. Health Check
echo "Performing health check..."
sleep 10
HEALTH_OK=true

# Check Backend (Internal Port)
if ! curl -s http://127.0.0.1:6666/api/queue/status > /dev/null; then
    echo "WARNING: Backend WebUI (port 6666) is not responding. Check logs: journalctl -u transcoder-webui"
    HEALTH_OK=false
fi

# Check Nginx Proxy (Public Port)
if ! curl -s -L --max-time 10 http://127.0.0.1:$WEBUI_PORT/transcoder/ > /dev/null; then
    echo "WARNING: Nginx Proxy (port $WEBUI_PORT) is not responding."
    HEALTH_OK=false
fi

# Check Adserver Proxy
if ! curl -s -L --max-time 10 http://127.0.0.1:$ADSERVER_PORT/ > /dev/null; then
    echo "WARNING: Adserver Proxy (port $ADSERVER_PORT) is not responding."
    HEALTH_OK=false
fi

if [ "$HEALTH_OK" = false ]; then
    echo "  - Checking Nginx service status..."
    systemctl status nginx --no-pager | grep "Active:"
    echo "  - Checking for port $WEBUI_PORT listeners..."
    ss -tulpn | grep ":$WEBUI_PORT"
    echo "  - Checking for port $ADSERVER_PORT listeners..."
    ss -tulpn | grep ":$ADSERVER_PORT"
    echo "  - Last 10 lines of Nginx error log:"
    tail -n 10 /var/log/nginx/error.log
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
    echo "NOTICE: Pipeline updated but health checks failed. See warnings above."
fi
echo "Transcoder Dashboard: http://$(hostname -I | awk '{print $1}'):$WEBUI_PORT/transcoder/"
echo "Adserver Dashboard:   http://$(hostname -I | awk '{print $1}'):$ADSERVER_PORT/"
echo "-------------------------------------------------------"
