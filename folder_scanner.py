# folder_scanner.py
import os
import logging
import redis
import json
import uuid
from datetime import datetime
import argparse
import time
import threading
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
import config

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(config.SCANNER_LOG),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

r = redis.Redis(host=config.REDIS_HOST, port=config.REDIS_PORT, db=config.REDIS_DB, decode_responses=True)

def is_already_processed(file_path):
    # Check history
    history_keys = r.keys(f"{config.HISTORY_PREFIX}*")
    for key in history_keys:
        hist = r.hgetall(key)
        if hist.get('input_path') == file_path:
            if hist.get('status') in ['completed', 'processing', 'queued', 'verifying', 'archiving']:
                return True
    
    # Check queue
    queue_items = r.lrange(config.QUEUE_NAME, 0, -1)
    for item in queue_items:
        data = json.loads(item)
        if data.get('input_path') == file_path:
            return True
            
    return False

def queue_file(job_type, file_path, job_id=None):
    if not job_id:
        job_id = str(uuid.uuid4())
    
    job_payload = {
        "job_id": job_id,
        "type": job_type,
        "input_path": file_path,
        "status": "queued",
        "queued_at": datetime.now().isoformat()
    }
    r.hset(f"{config.HISTORY_PREFIX}{job_id}", mapping=job_payload)
    r.rpush(config.QUEUE_NAME, json.dumps(job_payload))
    logger.info(f"Scanner queued {job_type}: {file_path}")

class AdFolderHandler(FileSystemEventHandler):
    def on_created(self, event):
        if event.is_directory:
            # Wait a bit for files to appear
            time.sleep(2)
            self.check_ad_folder(event.src_path)

    def check_ad_folder(self, folder_path):
        ad_id = os.path.basename(folder_path)
        if not ad_id.startswith("advert"): return
        
        json_path = os.path.join(folder_path, f"{ad_id}.json")
        if os.path.exists(json_path):
            with open(json_path, 'r') as f:
                meta = json.load(f)
            
            video_file = meta.get('input_path')
            if video_file and os.path.exists(video_file):
                if not is_already_processed(video_file):
                    logger.info(f"Watchdog detected new ad folder: {ad_id}")
                    queue_file('ad', video_file, job_id=ad_id)

def scan_ads():
    if not os.path.exists(config.ARCHIVE_BASE_ADS): return
    logger.info("Scanning ads directory...")
    for ad_id in os.listdir(config.ARCHIVE_BASE_ADS):
        folder_path = os.path.join(config.ARCHIVE_BASE_ADS, ad_id)
        if os.path.isdir(folder_path):
            json_path = os.path.join(folder_path, f"{ad_id}.json")
            if os.path.exists(json_path):
                with open(json_path, 'r') as f:
                    meta = json.load(f)
                if meta.get('status') == 'queued' and not is_already_processed(meta['input_path']):
                    queue_file('ad', meta['input_path'], job_id=ad_id)

def scan():
    logger.info("Starting folder scan...")
    
    # Scan Movies
    if os.path.exists(config.SOURCE_BASE_MOVIES):
        for root, _, files in os.walk(config.SOURCE_BASE_MOVIES):
            for file in files:
                if file.lower().endswith(config.VIDEO_EXTENSIONS):
                    full_path = os.path.join(root, file)
                    if not is_already_processed(full_path):
                        queue_file('movie', full_path)

    # Scan TV
    if os.path.exists(config.SOURCE_BASE_TV):
        for root, _, files in os.walk(config.SOURCE_BASE_TV):
            for file in files:
                if file.lower().endswith(config.VIDEO_EXTENSIONS):
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
        # Start Ads Watcher
        observer = Observer()
        observer.schedule(AdFolderHandler(), config.ARCHIVE_BASE_ADS, recursive=False)
        observer.start()
        logger.info(f"Ads Watchdog started on {config.ARCHIVE_BASE_ADS}")

        try:
            while True:
                now = datetime.now()
                # Periodic scan at 2:00 AM
                if now.hour == 2 and now.minute == 0:
                    scan()
                    try:
                        import auto_requeue
                        auto_requeue.requeue_failed_jobs()
                    except Exception as e:
                        logger.error(f"Failed to auto-requeue: {e}")
                    time.sleep(61)
                time.sleep(30)
        except KeyboardInterrupt:
            observer.stop()
        observer.join()
    else:
        scan()
