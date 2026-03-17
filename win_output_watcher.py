import os
import time
import json
import shutil
import logging
import redis
import config
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from datetime import datetime

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

r = redis.Redis(host=config.REDIS_HOST, port=config.REDIS_PORT, db=config.REDIS_DB, password=config.REDIS_PASSWORD, decode_responses=True)
r_ads = redis.Redis(host=config.REDIS_HOST, port=config.REDIS_PORT, db=config.REDIS_DB_ADS, password=config.REDIS_PASSWORD, decode_responses=True)

class WinOutputHandler(FileSystemEventHandler):
    def on_created(self, event):
        if event.is_directory:
            return
        
        # Windows worker writes a .done file when finished
        if event.src_path.endswith('.done'):
            self.process_done_file(event.src_path)

    def process_done_file(self, done_file_path):
        try:
            with open(done_file_path, 'r') as f:
                job_data = json.loads(f.read())
            
            job_id = job_data.get('job_id')
            output_path = job_data.get('output_path')
            job_type = job_data.get('type')
            
            logger.info(f"Windows job {job_id} finished. Processing output...")
            
            # Move files from Windows share to final destination
            # (Assuming Windows worker wrote to a subfolder in WIN_OUTPUT_MOUNT)
            win_job_dir = os.path.dirname(done_file_path)
            
            if job_type == 'ad':
                dest_dir = os.path.join(config.ADS_OUTPUT_DIR, os.path.basename(win_job_dir))
            else:
                dest_dir = os.path.dirname(output_path)
            
            if not os.path.exists(dest_dir):
                os.makedirs(dest_dir, exist_ok=True)
            
            # Move all files except the .done file
            for item in os.listdir(win_job_dir):
                if item.endswith('.done'): continue
                src = os.path.join(win_job_dir, item)
                dst = os.path.join(dest_dir, item)
                if os.path.isdir(src):
                    shutil.copytree(src, dst, dirs_exist_ok=True)
                else:
                    shutil.copy2(src, dst)
            
            # Update Redis
            history_key = f"{config.HISTORY_PREFIX}{job_id}"
            r.hset(history_key, mapping={
                "status": "completed",
                "completed_at": datetime.now().isoformat(),
                "progress": 100,
                "worker": "windows"
            })
            
            if job_type == 'ad':
                r_ads.hset(f"{config.AD_META_PREFIX}{job_id}", "status", "active")
                r_ads.hset(f"{config.AD_META_PREFIX}{job_id}", "completed_at", datetime.now().isoformat())
            
            # Publish event to transcoder:events channel
            event_payload = {
                "event": "transcoding_completed",
                "job_id": job_id,
                "type": job_type,
                "worker": "windows",
                "timestamp": datetime.now().isoformat()
            }
            r.publish("transcoder:events", json.dumps(event_payload))
            
            logger.info(f"Successfully processed Windows output for job {job_id}")
            
            # Cleanup Windows share folder
            shutil.rmtree(win_job_dir)
            
        except Exception as e:
            logger.error(f"Error processing Windows output: {e}")

def run_watcher():
    if not os.path.exists(config.WIN_OUTPUT_MOUNT):
        os.makedirs(config.WIN_OUTPUT_MOUNT, exist_ok=True)
        
    event_handler = WinOutputHandler()
    observer = Observer()
    observer.schedule(event_handler, config.WIN_OUTPUT_MOUNT, recursive=True)
    observer.start()
    
    logger.info(f"Windows output watcher started on {config.WIN_OUTPUT_MOUNT}")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()

if __name__ == "__main__":
    run_watcher()
