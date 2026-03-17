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
import config

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 20 * 1024 * 1024 * 1024  # 20GB
CORS(app, resources={r"/api/*": {"origins": "*"}})

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

@app.before_request
def log_request_info():
    logger.info('Request: %s %s', request.method, request.path)

def get_redis():
    """Helper to get a thread-safe, decoded Redis connection with timeouts."""
    return redis.Redis(
        host=config.REDIS_HOST,
        port=config.REDIS_PORT,
        db=config.REDIS_DB,
        password=config.REDIS_PASSWORD,
        decode_responses=True,
        socket_connect_timeout=5,
        socket_timeout=5
    )

def get_redis_ads():
    """Helper to get a thread-safe, decoded Redis connection for Ads (DB 1)."""
    return redis.Redis(
        host=config.REDIS_HOST,
        port=config.REDIS_PORT,
        db=config.REDIS_DB_ADS,
        password=config.REDIS_PASSWORD,
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
def index():
    return render_template('dashboard.html')

# --- API QUEUE ENDPOINTS ---

@app.route('/api/queue/status')
def get_queue_status():
    try:
        r = get_redis()
        queues = {
            config.TRANSCODE_QUEUE: [],
            config.LOCAL_QUEUE: [],
            config.WINDOWS_QUEUE: []
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
                    "worker": "Pending Routing" if q_name == config.TRANSCODE_QUEUE else ("Local (VAAPI)" if q_name == config.LOCAL_QUEUE else "Windows (NVENC)"),
                    "queued_at": data.get("queued_at", "Unknown"),
                    "priority": data.get("priority", 0)
                })

        active_job = None
        active_raw = r.get(config.ACTIVE_JOB_KEY)
        if active_raw:
            try:
                active_job = json.loads(active_raw)
                active_job['filename'] = os.path.basename(active_job.get('input_path', 'unknown'))
                active_job['worker_type'] = 'local'
            except: pass

        win_active_job = None
        win_active_raw = r.get(config.WIN_ACTIVE_JOB_KEY)
        if win_active_raw:
            try:
                win_active_job = json.loads(win_active_raw)
                win_active_job['filename'] = os.path.basename(win_active_job.get('input_path', 'unknown'))
                win_active_job['worker_type'] = 'windows'
            except: pass

        summary = {
            "incoming": len(queues[config.TRANSCODE_QUEUE]),
            "local": len(queues[config.LOCAL_QUEUE]),
            "windows": len(queues[config.WINDOWS_QUEUE]),
            "total": sum(len(v) for v in queues.values()),
            "active": (1 if active_job else 0) + (1 if win_active_job else 0)
        }
        return jsonify({
            "queues": queues, 
            "summary": summary, 
            "active_job": active_job,
            "win_active_job": win_active_job
        })
    except redis.RedisError as e:
        logger.error(f"Redis Error: {e}")
        return jsonify({"error": "Redis unavailable"}), 503

@app.route('/api/queue/job/<job_id>', methods=['DELETE'])
def remove_job(job_id):
    try:
        r = get_redis()
        removed = False
        for q in [config.TRANSCODE_QUEUE, config.LOCAL_QUEUE, config.WINDOWS_QUEUE]:
            items = r.lrange(q, 0, -1)
            for item in items:
                if job_id in item:
                    r.lrem(q, 0, item)
                    removed = True
        return jsonify({"removed": removed, "job_id": job_id})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/queue/move', methods=['POST'])
def move_job():
    try:
        r = get_redis()
        data = request.json
        job_id = data.get("job_id")
        target = data.get("target") # 'local' or 'windows'
        
        if not job_id or not target:
            return jsonify({"error": "Missing job_id or target"}), 400
            
        # Find and remove from any queue
        found_job = None
        for q in [config.TRANSCODE_QUEUE, config.LOCAL_QUEUE, config.WINDOWS_QUEUE]:
            items = r.lrange(q, 0, -1)
            for item in items:
                if job_id in item:
                    r.lrem(q, 0, item)
                    found_job = json.loads(item)
                    break
            if found_job: break
            
        if not found_job:
            return jsonify({"error": "Job not found in any queue"}), 404
            
        # Update force_worker and push to target
        found_job["force_worker"] = target
        target_queue = config.LOCAL_QUEUE if target == 'local' else config.WINDOWS_QUEUE
        
        # Push to FRONT of target queue for manual priority
        r.lpush(target_queue, json.dumps(found_job))
        
        return jsonify({
            "status": "ok",
            "job_id": job_id,
            "target": target,
            "message": f"Job manually routed to {target}"
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/queue/rebalance', methods=['POST'])
def rebalance_queue():
    try:
        r = get_redis()
        if not r.exists(config.WIN_HEARTBEAT_KEY):
            return jsonify({"error": "Windows worker offline"}), 400
        
        moved = 0
        for _ in range(10):
            job = r.rpop(config.LOCAL_QUEUE)
            if not job: break
            r.lpush(config.WINDOWS_QUEUE, job)
            moved += 1
            
        return jsonify({
            "moved": moved,
            "windows_queue_depth": r.llen(config.WINDOWS_QUEUE),
            "local_queue_depth": r.llen(config.LOCAL_QUEUE)
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# --- API COMPLETED ENDPOINTS ---

@app.route('/api/jobs/completed')
def get_completed_jobs():
    try:
        r = get_redis()
        # Optimization: Only fetch the most recent 100 jobs to speed up loading
        keys = r.keys(f"{config.HISTORY_PREFIX}*")
        
        # Sort keys by timestamp if possible, or just limit
        # Redis keys are not sorted. We'll fetch all but optimize the loop.
        
        jobs = []
        for key in keys:
            data = r.hgetall(key)
            job_id = key.split(":")[-1]
            job_type = data.get("type", "movie")
            
            status = data.get("status", "unknown")
            input_path = data.get("input_path")
            output_path = data.get("output_path")
            
            # Reconstruct output_path if missing for completed jobs
            if not output_path and status == "completed" and input_path:
                try:
                    output_path = config.get_output_dir(job_type, input_path, job_id=job_id)
                    # Save it back to Redis for future requests
                    r.hset(key, "output_path", output_path)
                except Exception as e:
                    logger.error(f"Failed to reconstruct output_path for {job_id}: {e}")

            # Title fallback - be aggressive
            title = data.get("title")
            if not title or title == "Unknown" or title == "unknown":
                if input_path:
                    filename = os.path.basename(input_path)
                    title = os.path.splitext(filename)[0].replace('_', ' ').replace('.', ' ')
                else:
                    title = f"Job {job_id}"

            # Optimization: Cache verification status in Redis to avoid disk I/O on every request
            hls_verified = data.get("hls_verified") == "True"
            v_label = data.get("verification_label")
            
            if not v_label:
                if status == "completed" and output_path:
                    hls_verified, v_label, _ = verify_hls_output(output_path)
                    # Cache it back to Redis
                    r.hset(key, mapping={
                        "hls_verified": str(hls_verified),
                        "verification_label": v_label
                    })
                elif status == "failed":
                    v_label = "Failed"
                elif status == "processing":
                    v_label = "Processing"
                else:
                    v_label = status.capitalize()

            # Calculate duration
            duration = 0
            started_at = data.get("started_at") or data.get("start_time")
            completed_at = data.get("completed_at") or data.get("end_time")
            
            if started_at and completed_at:
                try:
                    start = datetime.fromisoformat(started_at)
                    end = datetime.fromisoformat(completed_at)
                    duration = round((end - start).total_seconds() / 60)
                except: pass

            # Generate URLs
            vod_url = None
            stitched_url = None
            
            if status == "completed":
                # Ensure we have an output_path for URL generation
                current_output_path = output_path
                if not current_output_path and input_path:
                    current_output_path = config.get_output_dir(job_type, input_path, job_id=job_id)

                if current_output_path:
                    if job_type == "ad":
                        # Ads: http://192.168.0.103:8081/ads/advert0004/master.m3u8
                        rel_path = os.path.relpath(current_output_path, config.OUTPUT_BASE_ADS)
                        vod_url = f"http://192.168.0.103:8081/ads/{rel_path}/master.m3u8"
                    else:
                        # Movies/TV: http://192.168.0.103:8081/vod/hls/movies/.../master.m3u8
                        # Relative to /srv/vod/ as per Nginx alias /srv/vod/
                        rel_path = os.path.relpath(current_output_path, "/srv/vod")
                        vod_url = f"http://192.168.0.103:8081/vod/{rel_path}/master.m3u8"
                        
                        # Stitched URL: https://stream.ziaoba.com/playlist/hls/movies/.../master.m3u8
                        if job_type in ["movie", "tv"]:
                            stitched_url = f"https://stream.ziaoba.com/playlist/{rel_path}/master.m3u8"

            jobs.append({
                "job_id": job_id,
                "title": title,
                "filename": os.path.basename(data.get("input_path", "unknown")),
                "type": job_type,
                "status": status,
                "worker": data.get("worker", "unknown"),
                "queued_at": data.get("queued_at"),
                "completed_at": completed_at,
                "duration_minutes": duration,
                "vod_url": vod_url,
                "stitched_url": stitched_url,
                "hls_url": stitched_url, # Keep for backward compatibility if needed
                "hls_verified": hls_verified,
                "verification_label": v_label
            })
        
        # Sort by completed_at desc and limit to 100 for performance
        jobs.sort(key=lambda x: x.get('completed_at', '') or '', reverse=True)
        return jsonify({"jobs": jobs[:100], "total": len(jobs)})
    except Exception as e:
        logger.error(f"Error fetching history: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/scanner/scan', methods=['POST'])
def trigger_scan():
    try:
        subprocess.Popen(["sudo", "systemctl", "start", "transcoder-scanner.service"])
        return jsonify({"status": "Scanner triggered"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# --- API ADVERTISEMENT ENDPOINTS ---

@app.route('/api/ad/upload', methods=['POST'])
def upload_ad():
    try:
        r = get_redis()
        r_ads = get_redis_ads()
        if 'file' not in request.files:
            return jsonify({"error": "No file part"}), 400
        
        file = request.files['file']
        description = request.form.get('description')
        advertiser_name = request.form.get('advertiser_name')
        campaign_name = request.form.get('campaign_name', '')
        max_plays = int(request.form.get('max_plays', 0))

        if not description or not advertiser_name:
            return jsonify({"error": "Missing required fields: description or advertiser_name"}), 400

        ad_id = f"advert{r_ads.zcard(config.AD_REGISTRY_KEY) + 1:04d}"
        upload_dir = os.path.join(config.ARCHIVE_BASE_ADS, ad_id)
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

        # Save to Redis Ads DB
        r_ads.hset(f"{config.AD_META_PREFIX}{ad_id}", mapping=meta)
        r_ads.zadd(config.AD_REGISTRY_KEY, {ad_id: meta['upload_timestamp']})
        r_ads.zadd(config.ADVERTISER_INDEX_KEY, {advertiser_name: 0})
        r_ads.set(f"ad_plays:{ad_id}", 0)

        # Queue job with MAX priority in Main DB
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
        r.hset(f"{config.HISTORY_PREFIX}{job_id}", mapping=job_payload)
        # Push to FRONT of main queue for immediate routing
        r.lpush(config.TRANSCODE_QUEUE, json.dumps(job_payload))

        return jsonify({
            "ad_id": ad_id,
            "status": "queued",
            "message": "Ad uploaded and queued for transcoding",
            "hls_url": f"http://192.168.0.103/playlist/ad/{ad_id}/master.m3u8",
            "estimated_ready_minutes": 5
        })

    except Exception as e:
        logger.error(f"Ad upload failed: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/ads')
def list_ads():
    try:
        r_ads = get_redis_ads()
        ad_ids = r_ads.zrange(config.AD_REGISTRY_KEY, 0, -1)
        ads = []
        for ad_id in ad_ids:
            meta = r_ads.hgetall(f"{config.AD_META_PREFIX}{ad_id}")
            plays = int(r_ads.get(f"{config.AD_PLAYS_PREFIX}{ad_id}") or 0)
            max_p = int(meta.get("max_plays", 0))
            
            hls_path = os.path.join(config.OUTPUT_BASE_ADS, ad_id)
            hls_ready, _, _ = verify_hls_output(hls_path)
            
            ads.append({
                "ad_id": ad_id,
                "description": meta.get("description"),
                "advertiser_name": meta.get("advertiser_name"),
                "campaign_name": meta.get("campaign_name"),
                "hls_url": f"http://192.168.0.103/playlist/ad/{ad_id}/master.m3u8",
                "hls_ready": hls_ready,
                "max_plays": max_p,
                "current_plays": plays,
                "enabled": ad_id not in r_ads.smembers(config.ADS_DISABLED_KEY),
                "exhausted": (plays >= max_p) if max_p > 0 else False,
                "upload_timestamp": float(meta.get("upload_timestamp", 0))
            })
        return jsonify({"ads": ads})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/ad/<ad_id>', methods=['PATCH'])
def update_ad(ad_id):
    try:
        r_ads = get_redis_ads()
        data = request.json
        meta_key = f"{config.AD_META_PREFIX}{ad_id}"
        
        if not r_ads.exists(meta_key):
            return jsonify({"error": "Ad not found"}), 404
            
        allowed = ["description", "advertiser_name", "campaign_name", "max_plays"]
        updates = {k: v for k, v in data.items() if k in allowed}
        
        if "max_plays" in updates:
            try:
                updates["max_plays"] = int(updates["max_plays"])
            except:
                updates["max_plays"] = 0

        if updates:
            r_ads.hset(meta_key, mapping=updates)
            
        if "enabled" in data:
            if data["enabled"]: r_ads.srem(config.ADS_DISABLED_KEY, ad_id)
            else: r_ads.sadd(config.ADS_DISABLED_KEY, ad_id)

        # Update JSON file
        json_path = os.path.join(config.ARCHIVE_BASE_ADS, ad_id, f"{ad_id}.json")
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
        r_ads = get_redis_ads()
        meta = r_ads.hgetall(f"{config.AD_META_PREFIX}{ad_id}")
        if not meta: return jsonify({"error": "Not found"}), 404
        
        plays = r_ads.incr(f"{config.AD_PLAYS_PREFIX}{ad_id}")
        max_p = int(meta.get("max_plays", 0))
        
        exhausted = False
        if max_p > 0 and plays >= max_p:
            exhausted = True
            r_ads.sadd(config.ADS_DISABLED_KEY, ad_id)
            
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
            {"name": "Scanner",             "unit": "transcoder-scanner.service"},
            {"name": "Win Watcher",         "unit": "transcoder-win-watcher.service"},
            {"name": "Nginx Proxy",         "unit": "nginx.service"}
        ]
        
        results = []
        for s in services_to_check:
            stat = get_service_status(s["unit"])
            s.update(stat)
            results.append(s)
            
        # Windows Worker
        ttl = r.ttl(config.WIN_HEARTBEAT_KEY)
        win_status = {"status": "offline", "display": "Offline", "colour": "red"}
        if ttl > 0 or ttl == -1:
            hb_raw = r.get(config.WIN_HEARTBEAT_KEY)
            hb = {}
            if hb_raw:
                try:
                    hb = json.loads(hb_raw)
                except: pass
                
            win_status = {
                "status": "online",
                "display": "Online",
                "colour": "green",
                "ttl": ttl,
                "hostname": hb.get("hostname", "Unknown"),
                "ip": hb.get("ip", "Unknown"),
                "last_seen_seconds": int(time.time() - hb.get("timestamp_unix", time.time())) if hb.get("timestamp_unix") else 0,
                "queue_depth": r.llen(config.WINDOWS_QUEUE),
                "warning": ttl < 25 and ttl != -1,
                "gpus": hb.get("gpus", [])
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

@app.route('/stream')
def stream_logs():
    def generate():
        r = get_redis()
        pubsub = r.pubsub()
        pubsub.subscribe(config.LOCAL_LOG_CHANNEL, config.WIN_LOG_CHANNEL)
        
        while True:
            message = pubsub.get_message(ignore_subscribe_messages=True)
            if message:
                yield f"data: {message['data']}\n\n"
            time.sleep(0.1)
    return Response(generate(), mimetype='text/event-stream')

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=6666, debug=False)
