# Developer Brief: Stream-Ziaoba Transcoder Pipeline

This document provides a technical overview of the transcoder pipeline architecture, network configuration, and API surface for developers maintaining or extending the system.

## 1. System Architecture & Network
The system follows a distributed master-worker architecture.

| Component | Host | IP Address | Role |
|-----------|------|------------|------|
| **Ubuntu Master** | stream-ziaoba | `192.168.0.103` | API, Redis, Nginx, Local VAAPI Worker |
| **Windows Worker** | DESKTOP-WIN | `192.168.0.21` | Remote NVENC Worker (2x GTX 1050 Ti) |

### Port Mapping
| Port | Service | Access | Description |
|------|---------|--------|-------------|
| **80** | **Ad Server (Public)** | External | Public HLS Delivery (Stitched) |
| **8081** | **Transcoder WebUI** | External | Management Dashboard & Direct HLS |
| **88** | **Ad Admin (Nginx)** | External | Entry for Ad Server Admin UI |
| **6666** | **Flask WebUI** | Localhost | Internal dashboard backend |
| **8083** | **Ad Middleware** | Localhost | Core ad-stitching logic (FastAPI) |
| **8089** | **Ad Admin Backend** | Localhost | Ad Server Admin API (FastAPI) |
| **6379** | **Redis** | LAN | DB 0: Transcoder, DB 1: Ad Server |
| **445** | **Samba** | LAN | SMB shares for Windows worker access |

---

## 2. Nginx Configuration
**File Path:** `/etc/nginx/sites-available/mediaserver` (Port 8081)

Nginx acts as a reverse proxy and static file server.
- **Port 8081:** Listens for management traffic.
- **/transcoder/**: Proxies to Flask on `127.0.0.1:6666`.
- **/hls/**: Serves static `.m3u8` and `.ts` files from `/srv/vod/hls/`.
- **/ads/**: Serves static ad content from `/srv/vod/ads/`.
- **/stream/**: Proxies to Ad Middleware on `127.0.0.1:8083`.
- **/admin/**: Proxies to Ad Admin Backend on `127.0.0.1:88` (Nginx) or `8089` (FastAPI).

---

## 3. HLS URL Structure (Public)
The system uses the Ad Server as the primary public entry point for HLS content to ensure ad injection.

- **Movies:** `http://192.168.0.103/playlist/movie/{id}/master.m3u8`
- **TV:** `http://192.168.0.103/playlist/tv/{id}/master.m3u8`
- **Ads:** `http://192.168.0.103/playlist/ad/{id}/master.m3u8`
All API endpoints are prefixed with `/api`.

### Queue Management
- `GET /api/queue/status`: Returns JSON of all three queues (`transcode_queue`, `local_transcode_queue`, `windows_transcode_queue`).
- `DELETE /api/queue/job/<job_id>`: Removes a job from any queue.
- `POST /api/queue/rebalance`: Moves up to 10 jobs from local to windows queue if windows worker is online.

### Job History & Verification
- `GET /api/jobs/completed`: Returns list of all jobs with `hls_verified` status based on filesystem checks.

### Advertisement Management
- `GET /api/ads`: Lists all registered ads with play counts and HLS status.
- `POST /api/ad/upload`: Handles multipart form upload, metadata creation, and high-priority queuing.
- `PATCH /api/ad/<ad_id>`: Updates ad metadata (description, max_plays, enabled status).
- `POST /api/ad/<ad_id>/play`: Increments play count and checks for exhaustion.

### System & Services
- `GET /api/services/status`: Returns real-time status (systemctl) of all 8 services + Windows heartbeat.
- `POST /api/service/<unit_name>/restart`: Restarts a whitelisted systemd service (requires sudoers config).
- `GET /transcoder/stream`: SSE (Server-Sent Events) stream for live `worker.log` tailing.

---

## 4. Redis Schema
- **Queues (Lists):** `transcode_queue` (Incoming), `local_transcode_queue` (VAAPI), `windows_transcode_queue` (NVENC).
- **History (Hashes):** `job_history:{job_id}` - Stores status, worker, timestamps, and output paths.
- **Ads (ZSet/Hash):** `ad_registry` (Sorted by time), `ad_meta:{ad_id}` (Metadata), `ad_plays:{ad_id}` (Counter).
- **Heartbeat (String):** `worker:windows:heartbeat` (TTL 90s) - JSON status from Windows machine.

---

## 5. Maintenance & Deployment
**Tool:** `install.sh`

The maintenance script handles:
1. **Backups:** Copies `/opt/transcoder` to `/opt/transcoder_backups/` before updates.
2. **Permissions:** Enforces `media:www-data` ownership on VOD directories.
3. **Sudoers:** Deploys `/etc/sudoers.d/transcoder` for passwordless service restarts.
4. **Health Check:** Verifies port 8081 and Redis connectivity post-install.

### Common Commands
- **Update System:** `sudo bash install.sh`
- **Check Logs:** `tail -f /var/log/transcoder/worker.log`
- **Force Windows Job:** `python3 /opt/transcoder/force_win.py /path/to/file.mkv`

---

## 6. Developer Sync Checklist
When syncing new configurations:
1. Ensure `mediaserver.conf` is symlinked to `sites-enabled`.
2. Verify `WEBUI_PORT=8081` is set in `install.sh`.
3. Check that `webui.py` is binding to `0.0.0.0:6666`.
4. Confirm `transcoder.sudoers` is present in `/etc/sudoers.d/`.
