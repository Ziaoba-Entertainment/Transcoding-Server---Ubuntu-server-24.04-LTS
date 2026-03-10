import json
import logging
import os
import shutil
import subprocess
import time
from datetime import datetime
import redis
import config

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

WIN_OUTPUT_DIR = config.WIN_OUTPUT_MOUNT
VOD_HLS_DIR = '/srv/vod/hls'
VOD_ADS_DIR = '/srv/vod/ads'

r = redis.Redis(
    host=config.REDIS_HOST,
    port=config.REDIS_PORT,
    password=getattr(config, 'REDIS_PASSWORD', None) or None,
    db=config.REDIS_DB,
    decode_responses=True
)


def sanitize_segment(name):
    return ''.join(ch if ch.isalnum() or ch in ('-', '_', '.') else '_' for ch in name).strip('_')


def destination_for_job(job):
    job_type = job.get('type')
    input_path = job.get('input_path', '')
    file_name = os.path.splitext(os.path.basename(input_path))[0]
    episode_slug = sanitize_segment(file_name)

    if job_type == 'movie':
        movie_name = sanitize_segment(file_name)
        return os.path.join(VOD_HLS_DIR, 'movies', movie_name)
    if job_type == 'tv':
        rel = os.path.relpath(input_path, config.SOURCE_BASE_TV)
        parts = rel.split(os.sep)
        series = sanitize_segment(parts[0]) if len(parts) > 0 else 'Unknown'
        season = sanitize_segment(parts[1]) if len(parts) > 1 else 'Season_1'
        return os.path.join(VOD_HLS_DIR, 'tv', series, season, episode_slug)
    return os.path.join(VOD_ADS_DIR, job.get('job_id', episode_slug))


def fix_perms(path):
    subprocess.run(['chown', '-R', 'www-data:www-data', path], check=False)
    subprocess.run(['chmod', '-R', '755', path], check=False)


def run():
    logger.info('Windows output watcher started')
    os.makedirs(WIN_OUTPUT_DIR, exist_ok=True)
    while True:
        for entry in os.listdir(WIN_OUTPUT_DIR):
            src_dir = os.path.join(WIN_OUTPUT_DIR, entry)
            if not os.path.isdir(src_dir):
                continue
            meta_file = os.path.join(src_dir, 'job.json')
            if not os.path.exists(meta_file):
                continue

            with open(meta_file, 'r') as f:
                job = json.load(f)

            dst_dir = destination_for_job(job)
            os.makedirs(os.path.dirname(dst_dir), exist_ok=True)
            if os.path.exists(dst_dir):
                shutil.rmtree(dst_dir)
            shutil.copytree(src_dir, dst_dir, dirs_exist_ok=True)
            fix_perms(dst_dir)

            job_id = job.get('job_id')
            if job_id:
                r.hset(f"{config.HISTORY_PREFIX}{job_id}", mapping={
                    'status': 'completed',
                    'worker': 'windows',
                    'output_path': dst_dir,
                    'end_time': datetime.utcnow().isoformat(),
                })
            done_path = os.path.join(src_dir, '.processed')
            with open(done_path, 'w') as f:
                f.write(datetime.utcnow().isoformat())
            logger.info('Imported windows output for %s -> %s', job_id, dst_dir)
        time.sleep(5)


if __name__ == '__main__':
    run()
