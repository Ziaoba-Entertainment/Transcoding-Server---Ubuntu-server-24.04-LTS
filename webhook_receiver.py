# webhook_receiver.py
import os
import uuid
import json
import logging
from flask import Flask, request, jsonify
import redis
from datetime import datetime
import config

app = Flask(__name__)

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(config.WEBHOOK_LOG),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

r = redis.Redis(host=config.REDIS_HOST, port=config.REDIS_PORT, db=config.REDIS_DB, password=config.REDIS_PASSWORD, decode_responses=True)

def queue_job(job_type, input_path):
    if not os.path.exists(input_path):
        logger.error(f"File not found: {input_path}")
        return None, "File not found"

    # Check if already in history or queue using path index
    existing_job_id = r.get(f"{config.PATH_INDEX_PREFIX}{input_path}")
    if existing_job_id:
        hist = r.hgetall(f"{config.HISTORY_PREFIX}{existing_job_id}")
        if hist and hist.get('status') not in ['failed']:
            return existing_job_id, "Job already exists"

    # Generate a clean title from filename
    filename = os.path.basename(input_path)
    title = os.path.splitext(filename)[0].replace('_', ' ').replace('.', ' ')

    job_id = str(uuid.uuid4())
    job_payload = {
        "job_id": job_id,
        "title": title,
        "type": job_type,
        "input_path": input_path,
        "status": "queued",
        "queued_at": datetime.now().isoformat()
    }
    
    # Store in history and path index
    r.hset(f"{config.HISTORY_PREFIX}{job_id}", mapping=job_payload)
    r.set(f"{config.PATH_INDEX_PREFIX}{input_path}", job_id)
    
    # Push to queue
    r.rpush(config.TRANSCODE_QUEUE, json.dumps(job_payload))
    
    # Publish event to transcoder:events channel
    event_payload = {
        "event": "job_queued",
        "job_id": job_id,
        "type": job_type,
        "input_path": input_path,
        "timestamp": datetime.now().isoformat()
    }
    r.publish("transcoder:events", json.dumps(event_payload))
    
    logger.info(f"Queued {job_type} job {job_id} for {input_path}")
    return job_id, None

@app.route('/webhook/radarr', methods=['POST'])
def radarr_webhook():
    data = request.json
    logger.info(f"Received Radarr webhook: {data.get('eventType')}")
    
    if data.get('eventType') in ['Download', 'MovieFileRenamed']:
        movie_path = data.get('movieFile', {}).get('path')
        if movie_path:
            job_id, error = queue_job('movie', movie_path)
            if error:
                return jsonify({"status": "ignored", "reason": error}), 200
            return jsonify({"status": "queued", "job_id": job_id}), 200
            
    return jsonify({"status": "ignored"}), 200

@app.route('/webhook/sonarr', methods=['POST'])
def sonarr_webhook():
    data = request.json
    logger.info(f"Received Sonarr webhook: {data.get('eventType')}")
    
    if data.get('eventType') in ['Download', 'EpisodeFileRenamed']:
        episode_path = data.get('episodeFile', {}).get('path')
        if episode_path:
            job_id, error = queue_job('tv', episode_path)
            if error:
                return jsonify({"status": "ignored", "reason": error}), 200
            return jsonify({"status": "queued", "job_id": job_id}), 200
            
    return jsonify({"status": "ignored"}), 200

@app.route('/webhook/rescan', methods=['POST'])
def rescan_webhook():
    data = request.json
    path = data.get('path')
    job_type = data.get('type', 'movie')
    if path:
        job_id, error = queue_job(job_type, path)
        if error:
            return jsonify({"error": error}), 400
        return jsonify({"status": "queued", "job_id": job_id}), 200
    return jsonify({"error": "Path required"}), 400

@app.route('/health', methods=['GET'])
def health():
    return jsonify({"status": "ok"}), 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=config.WEBHOOK_PORT)
