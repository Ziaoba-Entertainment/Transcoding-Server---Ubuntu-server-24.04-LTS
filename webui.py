# webui.py - Stream-Ziaoba Transcoder Dashboard
import os
import json
import time
import logging
import subprocess
from datetime import datetime, timezone, timedelta
from pathlib import Path

import redis
from flask import Flask, render_template, jsonify, request, Response
from flask_cors import CORS
from werkzeug.utils import secure_filename

# --- CONFIGURATION ---
REDIS_HOST = "192.168.0.103"
REDIS_PORT = 6379
REDIS_PASSWORD = "TranscoderRedis2024!"

# Queues
INCOMING_QUEUE = "transcode_queue"
LOCAL_QUEUE = "local_transcode_queue"
WINDOWS_QUEUE = "windows_transcode_queue"

# Paths
VOD_BASE = "/srv/vod/hls"
ADS_BASE = "/srv/vod/ads"
ADS_DOWNLOADS = "/srv/downloads/ads"
LOG_DIR = "/var/log/transcoder"

# Prefixes
HISTORY_PREFIX = "job_history:"
AD_META_PREFIX = "ad_meta:"
AD_PLAYS_PREFIX = "ad_plays:"

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 20 * 1024 * 1024 * 1024  # 20GB
CORS(app, resources={r"/api/*": {"origins": "*"}})

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def get_redis():
    """Helper to get a thread-safe, decoded Redis connection with timeouts."""
    return redis.Redis(
        host=REDIS_HOST,
        port=REDIS_PORT,
        password=REDIS_PASSWORD,
        decode_responses=True,
        socket_connect_timeout=5,
        socket_timeout=5
    )

def verify_hls_output(output_dir):
    """
    Strict filesystem verification for HLS completion.
    Returns (success: bool, status_label: str, message: str)
    """
    try:
        p = Path(output_dir)
        if not p.exists():
            return False, "File Missing", "Directory does not exist"

        master = p / "master.m3u8"
        if not master.exists() or master.stat().st_size < 100:
            return False, "File Missing", "master.m3u8 missing or empty"

        # Check all 4 stream playlists
        missing_streams = []
        for i in range(4):
            pl = p / f"stream_{i}.m3u8"
            if not pl.exists() or pl.stat().st_size < 50:
                missing_streams.append(f"stream_{i}")

        if missing_streams:
            return False, "Partially Complete", f"Missing: {', '.join(missing_streams)}"

        # Check for segments
        ts_files = list(p.glob("*.ts"))
        if len(ts_files) < 16:
            return False, "Incomplete", f"Too few segments ({len(ts_files)})"

        return True, "Completed", f"Verified: {len(ts_files)} segments"
    except Exception as e:
        return False, "Error", str(e)

# --- PAGE ROUTES ---

@app.route('/')
@app.route('/transcoder/')
def index():
    # We'll serve the dashboard.html from the templates folder
    # If it doesn't exist, we'll fall back to a basic string or search for it
    return render_template('dashboard.html')

# --- API QUEUE ENDPOINTS ---

@app.route('/api/queue/status')
def get_queue_status():
    try:
        r = get_redis()
        queues = {
            "transcode_queue": [],
            "local_transcode_queue": [],
            "windows_transcode_queue": []
        }
        
        for q_name in queues.keys():
            raw_items = r.lrange(q_name, 0, -1)
            for i, item in enumerate(raw_items):
                try:
                    data = json.loads(item)
                except:
                    data = {"input_path": item, "job_id": "legacy", "type": "unknown"}
                
                queues[q_name].append({
                    "job_id": data.get("job_id", "N/A"),
                    "pos": i + 1,
                    "filename": os.path.basename(data.get("input_path", "unknown")),
                    "type": data.get("type", "movie"),
                    "worker": "Pending Routing" if q_name == INCOMING_QUEUE else ("Local (VAAPI)" if q_name == LOCAL_QUEUE else "Windows (NVENC)"),
                    "queued_at": data.get("queued_at", "Unknown"),
                    "priority": data.get("priority", 0)
                })

        summary = {
            "incoming": len(queues[INCOMING_QUEUE]),
            "local": len(queues[LOCAL_QUEUE]),
            "windows": len(queues[WINDOWS_QUEUE]),
            "total": sum(len(v) for v in queues.values())
        }
        return jsonify({"queues": queues, "summary": summary})
    except redis.RedisError as e:
        logger.error(f"Redis Error: {e}")
        return jsonify({"error": "Redis unavailable"}), 503

@app.route('/api/queue/job/<job_id>', methods=['DELETE'])
def remove_job(job_id):
    try:
        r = get_redis()
        removed = False
        for q in [INCOMING_QUEUE, LOCAL_QUEUE, WINDOWS_QUEUE]:
            items = r.lrange(q, 0, -1)
            for item in items:
                if job_id in item:
                    r.lrem(q, 0, item)
                    removed = True
        return jsonify({"removed": removed, "job_id": job_id})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/queue/rebalance', methods=['POST'])
def rebalance_queue():
    try:
        r = get_redis()
        if not r.exists("worker:windows:heartbeat"):
            return jsonify({"error": "Windows worker offline"}), 400
        
        moved = 0
        for _ in range(10):
            job = r.rpop(LOCAL_QUEUE)
            if not job: break
            r.lpush(WINDOWS_QUEUE, job)
            moved += 1
            
        return jsonify({
            "moved": moved,
            "windows_queue_depth": r.llen(WINDOWS_QUEUE),
            "local_queue_depth": r.llen(LOCAL_QUEUE)
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# --- API COMPLETED ENDPOINTS ---

@app.route('/api/jobs/completed')
def get_completed_jobs():
    try:
        r = get_redis()
        keys = r.keys(f"{HISTORY_PREFIX}*")
        jobs = []
        for key in keys:
            data = r.hgetall(key)
            job_id = key.split(":")[-1]
            
            status = data.get("status", "unknown")
            output_path = data.get("output_path")
            
            hls_verified = False
            v_label = "Unknown"
            if status == "completed" and output_path:
                hls_verified, v_label, _ = verify_hls_output(output_path)
            elif status == "failed":
                v_label = "Failed"

            # Calculate duration
            duration = 0
            if "started_at" in data and "completed_at" in data:
                try:
                    start = datetime.fromisoformat(data["started_at"])
                    end = datetime.fromisoformat(data["completed_at"])
                    duration = round((end - start).total_seconds() / 60)
                except: pass

            jobs.append({
                "job_id": job_id,
                "title": data.get("title", "Unknown"),
                "filename": os.path.basename(data.get("input_path", "unknown")),
                "type": data.get("type", "movie"),
                "status": status,
                "worker": data.get("worker", "unknown"),
                "queued_at": data.get("queued_at"),
                "completed_at": data.get("completed_at"),
                "duration_minutes": duration,
                "hls_url": f"http://192.168.0.103/hls/{data.get('type')}s/{os.path.basename(os.path.dirname(output_path or ''))}/master.m3u8" if output_path else None,
                "hls_verified": hls_verified,
                "verification_label": v_label
            })
        
        # Sort by completed_at desc
        jobs.sort(key=lambda x: x.get('completed_at', ''), reverse=True)
        return jsonify({"jobs": jobs, "total": len(jobs)})
    except Exception as e:
        logger.error(f"Error fetching history: {e}")
        return jsonify({"error": str(e)}), 500

# --- API ADVERTISEMENT ENDPOINTS ---

@app.route('/api/ad/upload', methods=['POST'])
def upload_ad():
    try:
        r = get_redis()
        if 'file' not in request.files:
            return jsonify({"error": "No file part"}), 400
        
        file = request.files['file']
        description = request.form.get('description')
        advertiser_name = request.form.get('advertiser_name')
        campaign_name = request.form.get('campaign_name', '')
        max_plays = int(request.form.get('max_plays', 0))

        if not description or not advertiser_name:
            return jsonify({"error": "Missing required fields: description or advertiser_name"}), 400

        ad_id = f"advert{r.zcard('ad_registry') + 1:04d}"
        upload_dir = os.path.join(ADS_DOWNLOADS, ad_id)
        os.makedirs(upload_dir, exist_ok=True)

        filename = secure_filename(file.filename)
        input_path = os.path.join(upload_dir, filename)
        file.save(input_path)

        # Create metadata
        meta = {
            "ad_id": ad_id,
            "description": description,
            "advertiser_name": advertiser_name,
            "campaign_name": campaign_name,
            "upload_timestamp": time.time(),
            "original_filename": filename,
            "input_path": input_path,
            "max_plays": max_plays,
            "status": "queued"
        }

        # Save to Redis
        r.hset(f"ad_meta:{ad_id}", mapping=meta)
        r.zadd("ad_registry", {ad_id: meta['upload_timestamp']})
        r.zadd("advertiser:index", {advertiser_name: 0})
        r.set(f"ad_processing:{ad_id}", "queued")
        r.set(f"ad_plays:{ad_id}", 0)

        # Queue job with HIGH priority
        job_id = ad_id
        job_payload = {
            "job_id": job_id,
            "type": "ad",
            "input_path": input_path,
            "priority": 10,
            "ad_id": ad_id,
            "status": "queued",
            "queued_at": datetime.now().isoformat()
        }
        r.hset(f"{HISTORY_PREFIX}{job_id}", mapping=job_payload)
        # Push to FRONT of local queue for immediate processing
        r.lpush(LOCAL_QUEUE, json.dumps(job_payload))

        return jsonify({
            "ad_id": ad_id,
            "status": "queued",
            "message": "Ad uploaded and queued for transcoding",
            "hls_url": f"http://192.168.0.103/ads/{ad_id}/master.m3u8",
            "estimated_ready_minutes": 5
        })

    except Exception as e:
        logger.error(f"Ad upload failed: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/ads')
def list_ads():
    try:
        r = get_redis()
        ad_ids = r.zrange("ad_registry", 0, -1)
        ads = []
        for ad_id in ad_ids:
            meta = r.hgetall(f"{AD_META_PREFIX}{ad_id}")
            plays = int(r.get(f"{AD_PLAYS_PREFIX}{ad_id}") or 0)
            max_p = int(meta.get("max_plays", 0))
            
            hls_path = os.path.join(ADS_BASE, ad_id)
            hls_ready, _, _ = verify_hls_output(hls_path)
            
            ads.append({
                "ad_id": ad_id,
                "description": meta.get("description"),
                "advertiser_name": meta.get("advertiser_name"),
                "campaign_name": meta.get("campaign_name"),
                "hls_url": f"http://192.168.0.103/ads/{ad_id}/master.m3u8",
                "hls_ready": hls_ready,
                "max_plays": max_p,
                "current_plays": plays,
                "enabled": ad_id not in r.smembers("ads:disabled"),
                "exhausted": (plays >= max_p) if max_p > 0 else False,
                "upload_timestamp": float(meta.get("upload_timestamp", 0))
            })
        return jsonify({"ads": ads})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/ad/<ad_id>', methods=['PATCH'])
def update_ad(ad_id):
    try:
        r = get_redis()
        data = request.json
        meta_key = f"{AD_META_PREFIX}{ad_id}"
        
        if not r.exists(meta_key):
            return jsonify({"error": "Ad not found"}), 404
            
        allowed = ["description", "advertiser_name", "campaign_name", "max_plays"]
        updates = {k: v for k, v in data.items() if k in allowed}
        if updates:
            r.hset(meta_key, mapping=updates)
            
        if "enabled" in data:
            if data["enabled"]: r.srem("ads:disabled", ad_id)
            else: r.sadd("ads:disabled", ad_id)

        # Update JSON file
        json_path = os.path.join(ADS_DOWNLOADS, ad_id, f"{ad_id}.json")
        if os.path.exists(json_path):
            with open(json_path, 'r') as f:
                file_data = json.load(f)
            file_data.update(updates)
            with open(json_path, 'w') as f:
                json.dump(file_data, f, indent=4)

        return jsonify({"ad_id": ad_id, "status": "ok", "updated": list(updates.keys())})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/ad/<ad_id>/play', methods=['POST'])
def record_play(ad_id):
    try:
        r = get_redis()
        meta = r.hgetall(f"{AD_META_PREFIX}{ad_id}")
        if not meta: return jsonify({"error": "Not found"}), 404
        
        plays = r.incr(f"{AD_PLAYS_PREFIX}{ad_id}")
        max_p = int(meta.get("max_plays", 0))
        
        exhausted = False
        if max_p > 0 and plays >= max_p:
            exhausted = True
            r.sadd("ads:disabled", ad_id)
            
        return jsonify({"ad_id": ad_id, "plays": plays, "exhausted": exhausted})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# --- API SYSTEM ENDPOINTS ---

def get_service_status(unit_name):
    try:
        result = subprocess.run(
            ["systemctl", "is-active", unit_name],
            capture_output=True, text=True, timeout=5
        )
        status = result.stdout.strip()
        display_map = {
            "active":      ("Running",      "green"),
            "inactive":    ("Stopped",      "yellow"),
            "failed":      ("Failed",       "red"),
            "activating":  ("Starting...",  "yellow"),
            "deactivating":("Stopping...",  "yellow"),
        }
        display, colour = display_map.get(status, ("Unknown", "grey"))
        return {"status": status, "display": display, "colour": colour}
    except:
        return {"status": "error", "display": "Error", "colour": "red"}

@app.route('/api/services/status')
def system_status():
    try:
        r = get_redis()
        services_to_check = [
            {"name": "Webhook Receiver",    "unit": "transcoder-webhook.service"},
            {"name": "Job Router",          "unit": "transcoder-router.service"},
            {"name": "Local Worker",        "unit": "transcoder-worker.service"},
            {"name": "Ad Stitching Server", "unit": "transcoder-ad-stitcher.service"},
            {"name": "Ad Admin Panel",      "unit": "transcoder-ad-admin.service"},
            {"name": "Nginx Proxy",         "unit": "nginx.service"}
        ]
        
        results = []
        for s in services_to_check:
            stat = get_service_status(s["unit"])
            s.update(stat)
            results.append(s)
            
        # Windows Worker
        ttl = r.ttl("worker:windows:heartbeat")
        win_status = {"status": "offline", "display": "Offline", "colour": "red"}
        if ttl > 0:
            hb = json.loads(r.get("worker:windows:heartbeat") or "{}")
            win_status = {
                "status": "online",
                "display": "Online",
                "colour": "green",
                "ttl": ttl,
                "hostname": hb.get("hostname"),
                "ip": hb.get("ip"),
                "last_seen_seconds": int(time.time() - hb.get("timestamp_unix", time.time())),
                "queue_depth": r.llen(WINDOWS_QUEUE),
                "warning": ttl < 25
            }
            
        return jsonify({
            "services": results,
            "windows_worker": win_status,
            "redis": {"status": "connected", "display": "Connected", "colour": "green"},
            "timestamp": datetime.now(timezone.utc).isoformat()
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/service/<unit_name>/restart', methods=['POST'])
def restart_service(unit_name):
    ALLOWED = {
        "transcoder-webhook.service", "transcoder-router.service",
        "transcoder-worker.service", "transcoder-ad-stitcher.service",
        "transcoder-ad-admin.service", "nginx.service", "transcoder-webui.service",
        "transcoder-scanner.service", "transcoder-win-watcher.service"
    }
    if unit_name not in ALLOWED:
        return jsonify({"error": "Forbidden"}), 403
    try:
        subprocess.run(["sudo", "systemctl", "restart", unit_name], timeout=15)
        return jsonify({"restarted": True, "unit": unit_name})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/transcoder/stream')
def stream_logs():
    def generate():
        log_path = "/var/log/transcoder/worker.log"
        if not os.path.exists(log_path):
            yield "data: Log file not found\n\n"
            return
        
        with open(log_path, "r") as f:
            f.seek(0, 2) # Go to end
            while True:
                line = f.readline()
                if not line:
                    time.sleep(0.5)
                    continue
                yield f"data: {line}\n\n"
    return Response(generate(), mimetype='text/event-stream')

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=6666, debug=False)
