# folder_scanner.py
import os
import logging
import redis
import json
import uuid
import re
from datetime import datetime
from pathlib import Path
import argparse
import time
import threading
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
import config

# Setup logging
if not os.path.exists(config.LOG_DIR):
    os.makedirs(config.LOG_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(config.SCANNER_LOG),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

r = redis.Redis(host=config.REDIS_HOST, port=config.REDIS_PORT, db=config.REDIS_DB, password=config.REDIS_PASSWORD, decode_responses=True)

# VALID MOVIE FOLDER REGEX - More lenient to support spaces and common naming
VALID_MOVIE_RE = re.compile(r'.* \(\d{4}\)$')
SKIPPED_FOLDERS = "skipped_folders"

def is_valid_movie_folder(folder_name):
    return bool(VALID_MOVIE_RE.match(folder_name))

def is_already_processed(file_path):
    # Check path index
    existing_job_id = r.get(f"{config.PATH_INDEX_PREFIX}{file_path}")
    if existing_job_id:
        hist = r.hgetall(f"{config.HISTORY_PREFIX}{existing_job_id}")
        if hist and hist.get('status') in ['completed', 'processing', 'queued', 'verifying', 'archiving']:
            return True
    
    # Check main queue (fallback/safety)
    queue_items = r.lrange(config.TRANSCODE_QUEUE, 0, -1)
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
    
    # Generate a clean title from filename
    filename = os.path.basename(file_path)
    title = os.path.splitext(filename)[0].replace('_', ' ').replace('.', ' ')
    
    job_payload = {
        "job_id": job_id,
        "title": title,
        "type": job_type,
        "input_path": file_path,
        "status": "queued",
        "queued_at": datetime.now().isoformat(),
        "priority": 5
    }
    r.hset(f"{config.HISTORY_PREFIX}{job_id}", mapping=job_payload)
    r.set(f"{config.PATH_INDEX_PREFIX}{file_path}", job_id)
    
    # Ads go to the FRONT of the queue for priority processing
    if job_type == 'ad':
        r.lpush(config.TRANSCODE_QUEUE, json.dumps(job_payload))
    else:
        r.rpush(config.TRANSCODE_QUEUE, json.dumps(job_payload))
        
    logger.info(f"Scanner queued {job_type}: {file_path} (Title: {title})")

def verify_hls_package(output_dir):
    """Checks if HLS package is complete with master.m3u8 and .ts files."""
    try:
        p = Path(output_dir)
        if not p.exists(): return False
        master = p / "master.m3u8"
        if not master.exists(): return False
        ts_files = list(p.glob("*.ts"))
        if len(ts_files) < 1: return False
        return True
    except:
        return False

def cleanup_incomplete_outputs():
    """Scans history for completed jobs and verifies their output. Requeues if broken."""
    logger.info("Starting output cleanup scan...")
    keys = r.keys(f"{config.HISTORY_PREFIX}*")
    requeued_count = 0
    
    for key in keys:
        data = r.hgetall(key)
        if data.get("status") == "completed":
            output_path = data.get("output_path")
            if not output_path or not verify_hls_package(output_path):
                job_id = key.split(":")[-1]
                input_path = data.get("input_path")
                job_type = data.get("type", "movie")
                
                if input_path and os.path.exists(input_path):
                    logger.warning(f"Cleanup: Job {job_id} has incomplete output at {output_path}. Requeuing...")
                    
                    # Delete broken output if it exists
                    if output_path and os.path.exists(output_path):
                        try:
                            import shutil
                            shutil.rmtree(output_path)
                        except Exception as e:
                            logger.error(f"Failed to delete broken output {output_path}: {e}")
                    
                    # Remove from history to allow re-scan or manually requeue
                    r.delete(key)
                    r.delete(f"{config.PATH_INDEX_PREFIX}{input_path}")
                    
                    # Manually requeue now
                    queue_file(job_type, input_path, job_id=job_id)
                    requeued_count += 1
                else:
                    logger.error(f"Cleanup: Job {job_id} output broken and input missing. Marking as failed.")
                    r.hset(key, "status", "failed")
                    r.hset(key, "error", "Output incomplete and source file missing")

    if requeued_count > 0:
        logger.info(f"Cleanup complete. Requeued {requeued_count} jobs.")
    else:
        logger.info("Cleanup complete. No broken packages found.")

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
    if not os.path.exists(config.OUTPUT_BASE_ADS): return
    logger.info("Scanning ads directory...")
    for ad_id in os.listdir(config.OUTPUT_BASE_ADS):
        folder_path = os.path.join(config.OUTPUT_BASE_ADS, ad_id)
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
    
    # 1. Cleanup broken outputs first
    cleanup_incomplete_outputs()
    
    # 2. Scan Movies
    if os.path.exists(config.SOURCE_BASE_MOVIES):
        for folder in os.listdir(config.SOURCE_BASE_MOVIES):
            folder_path = os.path.join(config.SOURCE_BASE_MOVIES, folder)
            if os.path.isdir(folder_path):
                if not is_valid_movie_folder(folder):
                    logger.warning(f"Skipping invalid movie folder: {folder}")
                    r.sadd(SKIPPED_FOLDERS, folder_path)
                    continue
                
                # Valid folder, scan for files
                for root, _, files in os.walk(folder_path):
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
        observer = Observer()
        if os.path.exists(config.OUTPUT_BASE_ADS):
            observer.schedule(AdFolderHandler(), config.OUTPUT_BASE_ADS, recursive=False)
            observer.start()
            logger.info(f"Ads Watchdog started on {config.OUTPUT_BASE_ADS}")

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
