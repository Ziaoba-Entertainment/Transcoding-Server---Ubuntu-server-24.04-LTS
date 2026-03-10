#!/bin/bash
# install.sh

set -e

echo "Starting Transcoder Pipeline Installation..."

# 1. Install System Dependencies
echo "Installing system dependencies..."
sudo apt-get update
sudo apt-get install -y ffmpeg redis-server python3-pip python3-venv libva-drm2 mesa-va-drivers vainfo nginx

# 2. Create Media User if not exists
if ! id "media" &>/dev/null; then
    echo "Creating media user..."
    sudo useradd -m -s /bin/bash media
fi

# Add media user to video and render groups for GPU access
sudo usermod -aG video media
sudo usermod -aG render media
sudo usermod -aG www-data media

# 3. Create Directory Structure
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
)

for dir in "${DIRS[@]}"; do
    sudo mkdir -p "$dir"
done

# Set permissions
sudo chown -R media:www-data /srv/vod
sudo chown -R media:media /srv/media_raw /srv/downloads /var/log/transcoder /opt/transcoder
sudo chmod -R 775 /srv/vod
sudo chmod -R 755 /srv/media_raw /srv/downloads /var/log/transcoder

# 4. Install Python Dependencies
echo "Installing Python dependencies in virtual environment..."
sudo -u media python3 -m venv /opt/transcoder/venv
sudo -u media /opt/transcoder/venv/bin/pip install flask redis psutil werkzeug python-magic watchdog

# 5. Copy Scripts to /opt/transcoder
echo "Copying scripts to /opt/transcoder..."
sudo cp config.py transcoder_worker.py webhook_receiver.py folder_scanner.py webui.py auto_requeue.py check_vaapi.sh /opt/transcoder/
sudo chmod +x /opt/transcoder/check_vaapi.sh
sudo chown -R media:media /opt/transcoder

# 6. Install Systemd Services and Nginx Config
echo "Installing systemd services and Nginx config..."
sudo cp transcoder-worker.service /etc/systemd/system/
sudo cp transcoder-webhook.service /etc/systemd/system/
sudo cp transcoder-webui.service /etc/systemd/system/
sudo cp transcoder-scanner.service /etc/systemd/system/

# Configure Nginx
sudo mkdir -p /etc/nginx/snippets
sudo cp proxy-params.conf /etc/nginx/snippets/proxy-params.conf
sudo cp mediaserver.conf /etc/nginx/sites-available/mediaserver
sudo rm -f /etc/nginx/sites-enabled/default
sudo ln -sf /etc/nginx/sites-available/mediaserver /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl restart nginx

sudo systemctl daemon-reload

# 7. Enable and Start Services
echo "Configuring firewall..."
if command -v ufw > /dev/null; then
    # Essential
    sudo ufw allow 22/tcp
    sudo ufw allow 80/tcp
    sudo ufw allow 443/tcp
    
    # Media Services (Direct Access)
    sudo ufw allow 7878/tcp
    sudo ufw allow 8989/tcp
    sudo ufw allow 9696/tcp
    sudo ufw allow 8080/tcp
    sudo ufw allow 8081/tcp
    
    # Cleanup old ports
    sudo ufw delete allow 6666/tcp
    sudo ufw delete allow 6667/tcp
    sudo ufw delete allow 8082/tcp
    
    # Ensure UFW is actually enabled
    sudo ufw --force enable
fi

echo "Starting services..."
sudo systemctl enable redis-server
sudo systemctl start redis-server

sudo systemctl enable transcoder-webhook
sudo systemctl restart transcoder-webhook

sudo systemctl enable transcoder-webui
sudo systemctl restart transcoder-webui

sudo systemctl enable transcoder-scanner
sudo systemctl restart transcoder-scanner

sudo systemctl enable transcoder-worker
sudo systemctl restart transcoder-worker

# 8. Verify Local Connectivity
echo "Verifying local Web UI connectivity via Nginx..."
sleep 2
if curl -s http://127.0.0.1/transcoder/ > /dev/null; then
    echo "Web UI is responding via Nginx on port 80."
else
    echo "ERROR: Web UI is not responding via Nginx. Check Nginx logs: /var/log/nginx/mediaserver_error.log"
fi

# 9. Verify VAAPI
echo "Verifying VAAPI hardware acceleration..."
/opt/transcoder/check_vaapi.sh

echo "-------------------------------------------------------"
echo "Installation Complete!"
echo "Dashboard: http://$(hostname -I | awk '{print $1}')/transcoder/"
echo "Radarr: http://$(hostname -I | awk '{print $1}')/radarr"
echo "Sonarr: http://$(hostname -I | awk '{print $1}')/sonarr"
echo "Prowlarr: http://$(hostname -I | awk '{print $1}')/prowlarr"
echo "-------------------------------------------------------"
