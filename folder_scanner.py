# folder_scanner.py
import os
import logging
import redis
import json
import uuid
import re
from datetime import datetime
import argparse
import time
import threading
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# HARDCODED CREDENTIALS
REDIS_HOST = "192.168.0.103"
REDIS_PORT = 6379
REDIS_PASSWORD = "TranscoderRedis2024!"

# REDIS KEYS
TRANSCODE_QUEUE = "transcode_queue"
HISTORY_PREFIX = "job_history:"
SKIPPED_FOLDERS = "skipped_folders"

# DIRECTORIES
SOURCE_BASE_MOVIES = "/srv/media_raw/movies"
SOURCE_BASE_TV = "/srv/media_raw/tv"
ARCHIVE_BASE_ADS = "/srv/vod/ads"
LOG_DIR = "/var/log/transcoder"
SCANNER_LOG = os.path.join(LOG_DIR, "scanner.log")

# VIDEO EXTENSIONS
VIDEO_EXTENSIONS = ('.mkv', '.mp4', '.avi', '.mov', '.m4v', '.ts')

# VALID MOVIE FOLDER REGEX
VALID_MOVIE_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_]*_\(\d{4}\)$')

# Setup logging
if not os.path.exists(LOG_DIR):
    os.makedirs(LOG_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(SCANNER_LOG),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, password=REDIS_PASSWORD, decode_responses=True)

def is_valid_movie_folder(folder_name):
    return bool(VALID_MOVIE_RE.match(folder_name))

def is_already_processed(file_path):
    # Check history
    history_keys = r.keys(f"{HISTORY_PREFIX}*")
    for key in history_keys:
        hist = r.hgetall(key)
        if hist.get('input_path') == file_path:
            if hist.get('status') in ['completed', 'processing', 'queued', 'verifying', 'archiving']:
                return True
    
    # Check main queue
    queue_items = r.lrange(TRANSCODE_QUEUE, 0, -1)
    for item in queue_items:
        try:
            data = json.loads(item)
            if data.get('input_path') == file_path:
                return True
        except:
            continue
            
    return False

def queue_file(job_type, file_path, job_id=None):
    if not job_id:
        job_id = str(uuid.uuid4())
    
    job_payload = {
        "job_id": job_id,
        "type": job_type,
        "input_path": file_path,
        "status": "queued",
        "queued_at": datetime.now().isoformat(),
        "priority": 5
    }
    r.hset(f"{HISTORY_PREFIX}{job_id}", mapping=job_payload)
    r.rpush(TRANSCODE_QUEUE, json.dumps(job_payload))
    logger.info(f"Scanner queued {job_type}: {file_path}")

class AdFolderHandler(FileSystemEventHandler):
    def on_created(self, event):
        if event.is_directory:
            time.sleep(2)
            self.check_ad_folder(event.src_path)

    def check_ad_folder(self, folder_path):
        ad_id = os.path.basename(folder_path)
        if not ad_id.startswith("advert"): return
        
        json_path = os.path.join(folder_path, f"{ad_id}.json")
        if os.path.exists(json_path):
            try:
                with open(json_path, 'r') as f:
                    meta = json.load(f)
                
                video_file = meta.get('input_path')
                if video_file and os.path.exists(video_file):
                    if not is_already_processed(video_file):
                        logger.info(f"Watchdog detected new ad folder: {ad_id}")
                        queue_file('ad', video_file, job_id=ad_id)
            except Exception as e:
                logger.error(f"Failed to process ad folder {ad_id}: {e}")

def scan_ads():
    if not os.path.exists(ARCHIVE_BASE_ADS): return
    logger.info("Scanning ads directory...")
    for ad_id in os.listdir(ARCHIVE_BASE_ADS):
        folder_path = os.path.join(ARCHIVE_BASE_ADS, ad_id)
        if os.path.isdir(folder_path):
            json_path = os.path.join(folder_path, f"{ad_id}.json")
            if os.path.exists(json_path):
                try:
                    with open(json_path, 'r') as f:
                        meta = json.load(f)
                    if meta.get('status') == 'queued' and not is_already_processed(meta['input_path']):
                        queue_file('ad', meta['input_path'], job_id=ad_id)
                except:
                    continue

def scan():
    logger.info("Starting folder scan...")
    
    # Scan Movies
    if os.path.exists(SOURCE_BASE_MOVIES):
        for folder in os.listdir(SOURCE_BASE_MOVIES):
            folder_path = os.path.join(SOURCE_BASE_MOVIES, folder)
            if os.path.isdir(folder_path):
                if not is_valid_movie_folder(folder):
                    logger.warning(f"Skipping invalid movie folder: {folder}")
                    r.sadd(SKIPPED_FOLDERS, folder_path)
                    continue
                
                # Valid folder, scan for files
                for root, _, files in os.walk(folder_path):
                    for file in files:
                        if file.lower().endswith(VIDEO_EXTENSIONS):
                            full_path = os.path.join(root, file)
                            if not is_already_processed(full_path):
                                queue_file('movie', full_path)

    # Scan TV
    if os.path.exists(SOURCE_BASE_TV):
        for root, _, files in os.walk(SOURCE_BASE_TV):
            for file in files:
                if file.lower().endswith(VIDEO_EXTENSIONS):
                    full_path = os.path.join(root, file)
                    if not is_already_processed(full_path):
                        queue_file('tv', full_path)
    
    # Scan Ads
    scan_ads()
    
    logger.info("Scan complete.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--daemon", action="store_true")
    args = parser.parse_args()

    if args.once:
        scan()
    elif args.daemon:
        observer = Observer()
        if os.path.exists(ARCHIVE_BASE_ADS):
            observer.schedule(AdFolderHandler(), ARCHIVE_BASE_ADS, recursive=False)
            observer.start()
            logger.info(f"Ads Watchdog started on {ARCHIVE_BASE_ADS}")

        try:
            while True:
                now = datetime.now()
                if now.hour == 2 and now.minute == 0:
                    scan()
                    time.sleep(61)
                time.sleep(30)
        except KeyboardInterrupt:
            observer.stop()
        observer.join()
    else:
        scan()
