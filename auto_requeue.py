# auto_requeue.py
import redis
import json
import logging
import os
from datetime import datetime
import config

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(os.path.join(config.LOG_DIR, "auto_requeue.log")),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

r = redis.Redis(host=config.REDIS_HOST, port=config.REDIS_PORT, db=config.REDIS_DB, password=config.REDIS_PASSWORD, decode_responses=True)

def requeue_failed_jobs():
    logger.info("Starting auto-requeue of failed jobs...")
    history_keys = r.keys(f"{config.HISTORY_PREFIX}*")
    requeued_count = 0
    
    # Get current queue to avoid double-queuing
    queue_items = r.lrange(config.TRANSCODE_QUEUE, 0, -1)
    queued_paths = set()
    for item in queue_items:
        try:
            data = json.loads(item)
            queued_paths.add(data.get('input_path'))
        except: pass

    # Get active job path
    active_job_raw = r.get(config.ACTIVE_JOB_KEY)
    if active_job_raw:
        try:
            active_data = json.loads(active_job_raw)
            queued_paths.add(active_data.get('input_path'))
        except: pass

    for key in history_keys:
        job_id = key.split(':')[-1]
        hist = r.hgetall(key)
        
        if hist.get('status') == 'failed':
            input_path = hist.get('input_path')
            job_type = hist.get('type')
            
            if not input_path or not os.path.exists(input_path):
                logger.warning(f"Failed job {job_id} input path not found: {input_path}. Skipping.")
                continue
                
            if input_path in queued_paths:
                logger.info(f"Failed job {job_id} is already in queue or processing. Skipping.")
                continue

            # Re-queue
            job_payload = {
                "job_id": job_id,
                "type": job_type,
                "input_path": input_path,
                "status": "queued",
                "queued_at": datetime.now().isoformat()
            }
            
            # Update history status back to queued
            r.hset(key, mapping={
                "status": "queued",
                "queued_at": job_payload["queued_at"],
                "error": "" # Clear previous error
            })
            
            r.rpush(config.TRANSCODE_QUEUE, json.dumps(job_payload))
            logger.info(f"Re-queued failed job {job_id}: {input_path}")
            requeued_count += 1
            queued_paths.add(input_path)

    logger.info(f"Auto-requeue complete. Re-queued {requeued_count} jobs.")
    return requeued_count

if __name__ == "__main__":
    requeue_failed_jobs()
