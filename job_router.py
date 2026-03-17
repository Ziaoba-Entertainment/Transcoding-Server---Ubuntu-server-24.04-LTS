import redis
import json
import time
import logging
from datetime import datetime
import config

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
                host=config.REDIS_HOST,
                port=config.REDIS_PORT,
                password=config.REDIS_PASSWORD,
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
            "windows_online": str(r.exists(config.WIN_HEARTBEAT_KEY)).lower(),
            "local_queue_depth": r.llen(config.LOCAL_QUEUE),
            "windows_queue_depth": r.llen(config.WINDOWS_QUEUE),
            "transcode_queue_depth": r.llen(config.TRANSCODE_QUEUE),
            "updated_at": datetime.now().isoformat()
        }
        r.hset(config.ROUTER_STATUS_KEY, mapping=status)
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
            r.rpush(config.WINDOWS_QUEUE, json.dumps(job))
            logger.info(f"FORCE WINDOWS: job {job_id} pushed to windows_transcode_queue")
            return
        elif force_worker == "local":
            r.rpush(config.LOCAL_QUEUE, json.dumps(job))
            logger.info(f"FORCE LOCAL: job {job_id} pushed to local_transcode_queue")
            return

        # 2. Standard Routing Logic
        is_ad = job_type == "ad" or priority >= 8
        windows_online = r.exists(config.WIN_HEARTBEAT_KEY)

        if is_ad:
            # Ads always go to LOCAL queue at FRONT for fastest processing
            r.lpush(config.LOCAL_QUEUE, json.dumps(job))
            logger.info(f"AD PRIORITY: job {job_id} pushed to FRONT of local queue")
        elif windows_online:
            # Check if Windows worker is already busy or has a deep queue
            # We allow up to 3 jobs in the windows queue to keep it "loaded"
            win_queue_depth = r.llen(config.WINDOWS_QUEUE)
            is_busy = r.exists(config.WIN_ACTIVE_JOB_KEY)
            
            if is_busy and win_queue_depth >= 2:
                # If busy AND already has 2+ jobs waiting (total 3+), route to local
                r.rpush(config.LOCAL_QUEUE, json.dumps(job))
                logger.info(f"WINDOWS LOADED ({win_queue_depth} waiting): Routing job {job_id} to local_transcode_queue")
            else:
                r.rpush(config.WINDOWS_QUEUE, json.dumps(job))
                logger.info(f"Routing job {job_id} [{job_type}] to windows_transcode_queue (Depth: {win_queue_depth})")
        else:
            r.rpush(config.LOCAL_QUEUE, json.dumps(job))
            logger.info(f"Routing job {job_id} [{job_type}] to local_transcode_queue (windows offline)")
            
    except Exception as e:
        logger.error(f"Error routing job: {e}")
        # If routing fails, try to put it back in the main queue
        r.rpush(config.TRANSCODE_QUEUE, json.dumps(job))

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
            job_raw = r.blpop(config.TRANSCODE_QUEUE, timeout=2)
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
