import redis
import json
import time
import logging
from datetime import datetime

# HARDCODED CREDENTIALS
REDIS_HOST = "192.168.0.103"
REDIS_PORT = 6379
REDIS_PASSWORD = "TranscoderRedis2024!"

# REDIS KEYS
TRANSCODE_QUEUE = "transcode_queue"
LOCAL_QUEUE = "local_transcode_queue"
WINDOWS_QUEUE = "windows_transcode_queue"
WIN_HEARTBEAT_KEY = "worker:windows:heartbeat"
ROUTER_STATUS_KEY = "router:status"

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

def get_redis_client():
    backoff = 1
    while True:
        try:
            r = redis.Redis(
                host=REDIS_HOST,
                port=REDIS_PORT,
                password=REDIS_PASSWORD,
                decode_responses=True,
                socket_timeout=5,
                socket_connect_timeout=5
            )
            r.ping()
            return r
        except redis.ConnectionError as e:
            logger.error(f"Redis connection failed: {e}. Retrying in {backoff}s...")
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)

def update_router_status(r):
    try:
        status = {
            "windows_online": str(r.exists(WIN_HEARTBEAT_KEY)).lower(),
            "local_queue_depth": r.llen(LOCAL_QUEUE),
            "windows_queue_depth": r.llen(WINDOWS_QUEUE),
            "transcode_queue_depth": r.llen(TRANSCODE_QUEUE),
            "updated_at": datetime.now().isoformat()
        }
        r.hset(ROUTER_STATUS_KEY, mapping=status)
    except Exception as e:
        logger.error(f"Failed to update router status: {e}")

def route_job(r, job):
    try:
        job_id = job.get("job_id", "unknown")
        job_type = job.get("type", "unknown")
        priority = job.get("priority", 0)
        force_worker = job.get("force_worker")
        
        # 1. Handle explicit worker override
        if force_worker == "windows":
            r.rpush(WINDOWS_QUEUE, json.dumps(job))
            logger.info(f"FORCE WINDOWS: job {job_id} pushed to windows_transcode_queue")
            return
        elif force_worker == "local":
            r.rpush(LOCAL_QUEUE, json.dumps(job))
            logger.info(f"FORCE LOCAL: job {job_id} pushed to local_transcode_queue")
            return

        # 2. Standard Routing Logic
        is_ad = job_type == "ad" or priority >= 8
        windows_online = r.exists(WIN_HEARTBEAT_KEY)

        if is_ad:
            # Ads always go to LOCAL queue at FRONT for fastest processing
            r.lpush(LOCAL_QUEUE, json.dumps(job))
            logger.info(f"AD PRIORITY: job {job_id} pushed to FRONT of local queue")
        elif windows_online:
            r.rpush(WINDOWS_QUEUE, json.dumps(job))
            logger.info(f"Routing job {job_id} [{job_type}] to windows_transcode_queue")
        else:
            r.rpush(LOCAL_QUEUE, json.dumps(job))
            logger.info(f"Routing job {job_id} [{job_type}] to local_transcode_queue (windows offline)")
            
    except Exception as e:
        logger.error(f"Error routing job: {e}")
        # If routing fails, try to put it back in the main queue
        r.rpush(TRANSCODE_QUEUE, json.dumps(job))

def main():
    logger.info("Job Router starting...")
    r = get_redis_client()
    last_status_update = 0
    
    while True:
        try:
            # Update status every 10 seconds
            if time.time() - last_status_update > 10:
                update_router_status(r)
                last_status_update = time.time()
                
            # Wait for jobs in the main queue
            job_raw = r.blpop(TRANSCODE_QUEUE, timeout=2)
            if job_raw:
                try:
                    job = json.loads(job_raw[1])
                    route_job(r, job)
                except json.JSONDecodeError:
                    logger.error(f"Malformed job JSON: {job_raw[1]}")
                    
        except redis.ConnectionError:
            logger.error("Redis connection lost in main loop. Reconnecting...")
            r = get_redis_client()
        except Exception as e:
            logger.error(f"Unexpected error in main loop: {e}")
            time.sleep(5)

if __name__ == "__main__":
    main()
