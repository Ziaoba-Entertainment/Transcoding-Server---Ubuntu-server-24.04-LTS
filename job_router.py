import json
import logging
import time
from datetime import datetime
import redis
import config

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

r = redis.Redis(
    host=config.REDIS_HOST,
    port=config.REDIS_PORT,
    password=getattr(config, 'REDIS_PASSWORD', None) or None,
    db=config.REDIS_DB,
    decode_responses=True
)


def windows_online():
    return bool(r.exists(config.WIN_HEARTBEAT_KEY))


def update_router_status():
    mapping = {
        'windows_online': '1' if windows_online() else '0',
        'local_queue_depth': r.llen(config.LOCAL_QUEUE),
        'windows_queue_depth': r.llen(config.WINDOWS_QUEUE),
        'updated_at': datetime.utcnow().isoformat()
    }
    heartbeat_raw = r.get(config.WIN_HEARTBEAT_KEY)
    if heartbeat_raw:
        try:
            hb = json.loads(heartbeat_raw)
            for key in ['hostname', 'ip', 'gpu_model', 'gpus']:
                if key in hb:
                    mapping[key] = hb[key]
        except Exception:
            pass
    r.hset(config.ROUTER_STATUS_KEY, mapping=mapping)


def run():
    logger.info('Router started')
    while True:
        item = r.blpop(config.TRANSCODE_QUEUE, timeout=2)
        if not item:
            update_router_status()
            continue

        job = json.loads(item[1])
        target_queue = config.WINDOWS_QUEUE if windows_online() else config.LOCAL_QUEUE
        target_worker = 'windows' if target_queue == config.WINDOWS_QUEUE else 'local'

        job['worker'] = target_worker
        r.rpush(target_queue, json.dumps(job))
        r.hset(f"{config.HISTORY_PREFIX}{job['job_id']}", mapping={'worker': target_worker})
        update_router_status()
        logger.info('Routed %s -> %s', job.get('job_id'), target_worker)


if __name__ == '__main__':
    while True:
        try:
            run()
        except Exception as exc:
            logger.exception('Router crashed: %s', exc)
            time.sleep(2)
