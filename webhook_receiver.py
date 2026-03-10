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

r = redis.Redis(host=config.REDIS_HOST, port=config.REDIS_PORT, db=config.REDIS_DB, decode_responses=True)

def queue_job(job_type, input_path):
    if not os.path.exists(input_path):
        logger.error(f"File not found: {input_path}")
        return None, "File not found"

    # Check if already in history or queue
    history_keys = r.keys(f"{config.HISTORY_PREFIX}*")
    for key in history_keys:
        hist = r.hgetall(key)
        if hist.get('input_path') == input_path and hist.get('status') not in ['failed']:
            return hist.get('job_id'), "Job already exists"

    job_id = str(uuid.uuid4())
    job_payload = {
        "job_id": job_id,
        "type": job_type,
        "input_path": input_path,
        "status": "queued",
        "queued_at": datetime.now().isoformat()
    }
    
    # Store in history
    r.hset(f"{config.HISTORY_PREFIX}{job_id}", mapping=job_payload)
    
    # Push to queue
    r.rpush(config.QUEUE_NAME, json.dumps(job_payload))
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
