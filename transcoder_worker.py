# transcoder_worker.py
import os
import json
import time
import subprocess
import shutil
import logging
import signal
import re
import redis
from datetime import datetime
import config

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(config.WORKER_LOG),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

r = redis.Redis(host=config.REDIS_HOST, port=config.REDIS_PORT, db=config.REDIS_DB, password=config.REDIS_PASSWORD, decode_responses=True)
r_ads = redis.Redis(host=config.REDIS_HOST, port=config.REDIS_PORT, db=config.REDIS_DB_ADS, password=config.REDIS_PASSWORD, decode_responses=True)

class TranscoderWorker:
    def __init__(self):
        self.running = True
        signal.signal(signal.SIGTERM, self.handle_sigterm)
        signal.signal(signal.SIGINT, self.handle_sigterm)

    def handle_sigterm(self, signum, frame):
        logger.info("Received termination signal. Finishing current job...")
        self.running = False

    def sanitize_path(self, path_segment):
        sanitized = re.sub(r'[^\w\s\-\(\).]', '', path_segment)
        sanitized = sanitized.replace(' ', '_')
        return sanitized

    def get_output_dir(self, job_type, input_path, job_id=None):
        if job_type == 'ad':
            return os.path.join(config.OUTPUT_BASE_ADS, job_id)
            
        base_source = config.SOURCE_BASE_MOVIES if job_type == 'movie' else config.SOURCE_BASE_TV
        base_output = config.OUTPUT_BASE_MOVIES if job_type == 'movie' else config.OUTPUT_BASE_TV
        
        rel_path = os.path.relpath(input_path, base_source)
        path_parts = rel_path.split(os.sep)
        sanitized_parts = [self.sanitize_path(p) for p in path_parts[:-1]]
        
        if job_type == 'tv':
            filename = os.path.splitext(path_parts[-1])[0]
            sanitized_parts.append(self.sanitize_path(filename))
        
        return os.path.join(base_output, *sanitized_parts)

    def update_job_status(self, job_id, status, error=None, progress=0, job_type=None):
        history_key = f"{config.HISTORY_PREFIX}{job_id}"
        data = {
            "status": status,
            "last_update": datetime.now().isoformat(),
            "progress": progress,
            "worker": "local"
        }
        if error:
            data["error"] = error
        if status == "completed":
            data["end_time"] = datetime.now().isoformat()
            
        r.hset(history_key, mapping=data)
        
        # If it's an ad, also update the metadata JSON and DB 1
        if job_type == 'ad':
            meta_path = os.path.join(config.ARCHIVE_BASE_ADS, job_id, f"{job_id}.json")
            if os.path.exists(meta_path):
                with open(meta_path, 'r') as f:
                    ad_meta = json.load(f)
                ad_meta['status'] = status
                if error: ad_meta['error'] = error
                if status == 'completed': ad_meta['completed_at'] = data['end_time']
                with open(meta_path, 'w') as f:
                    json.dump(ad_meta, f)
                
                # Sync to Ad Server Registry (DB 1)
                if status == 'completed':
                    r_ads.zadd(config.AD_REGISTRY_KEY, {job_id: time.time()})
                    r_ads.hset(f"{config.AD_META_PREFIX}{job_id}", mapping={
                        "ad_id": job_id,
                        "description": ad_meta.get('description', ''),
                        "status": "completed",
                        "last_update": datetime.now().isoformat()
                    })
                elif status == 'failed':
                    r_ads.hset(f"{config.AD_META_PREFIX}{job_id}", "status", "failed")

        # Update active job info for Web UI
        if status in ["processing", "verifying", "archiving"]:
            active_info = r.hgetall(history_key)
            r.set(config.ACTIVE_JOB_KEY, json.dumps(active_info))
        elif status in ["completed", "failed"]:
            r.delete(config.ACTIVE_JOB_KEY)

    def get_duration(self, input_path):
        try:
            cmd = [
                "ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1", input_path
            ]
            output = subprocess.check_output(cmd).decode().strip()
            return float(output)
        except Exception as e:
            logger.error(f"Failed to get duration for {input_path}: {e}")
            return 0

    def run_ffmpeg(self, job_id, input_path, output_dir, job_type):
        duration = self.get_duration(input_path)
        os.makedirs(output_dir, exist_ok=True)
        
        # Use the optimized template from config
        ffmpeg_cmd = []
        for arg in config.FFMPEG_CMD_TEMPLATE:
            ffmpeg_cmd.append(arg.format(input_path=input_path, output_dir=output_dir))

        logger.info(f"Starting FFmpeg for job {job_id} ({job_type})")
        logger.info(f"Executing: {' '.join(ffmpeg_cmd)}")
        process = subprocess.Popen(ffmpeg_cmd, stderr=subprocess.PIPE, universal_newlines=True)
        
        time_regex = re.compile(r"time=(\d+):(\d+):(\d+\.\d+)")
        log_buffer = []
        
        for line in process.stderr:
            log_buffer.append(line)
            if len(log_buffer) > 100: log_buffer.pop(0)
            
            # Update last logs in Redis for UI
            r.hset(f"{config.HISTORY_PREFIX}{job_id}", "last_logs", "".join(log_buffer[-20:]))
            
            match = time_regex.search(line)
            if match and duration > 0:
                hours, mins, secs = map(float, match.groups())
                current_time = hours * 3600 + mins * 60 + secs
                progress = min(99, int((current_time / duration) * 100))
                self.update_job_status(job_id, "processing", progress=progress, job_type=job_type)

        process.wait()
        if process.returncode != 0:
            error_msg = f"FFmpeg failed with return code {process.returncode}\n"
            error_msg += "Last 50 lines of log:\n"
            error_msg += "".join(log_buffer[-50:])
            logger.error(error_msg)
            # Update history with full error
            r.hset(f"{config.HISTORY_PREFIX}{job_id}", "error", error_msg)
            raise Exception(f"FFmpeg failed with return code {process.returncode}. Check logs for details.")

    def verify_output(self, output_dir):
        master_pl = os.path.join(output_dir, "master.m3u8")
        if not os.path.exists(master_pl):
            return False
        ts_files = [f for f in os.listdir(output_dir) if f.endswith(".ts")]
        return len(ts_files) >= 1 # Ads might be short, just check for at least 1 segment

    def archive_and_cleanup(self, job_type, input_path):
        if job_type == 'ad':
            return # Ads are already in their permanent archive location
            
        base_source = config.SOURCE_BASE_MOVIES if job_type == 'movie' else config.SOURCE_BASE_TV
        base_archive = config.ARCHIVE_BASE_MOVIES if job_type == 'movie' else config.ARCHIVE_BASE_TV
        
        rel_path = os.path.relpath(input_path, base_source)
        archive_path = os.path.join(base_archive, rel_path)
        archive_dir = os.path.dirname(archive_path)
        
        os.makedirs(archive_dir, exist_ok=True)
        logger.info(f"Archiving {input_path} to {archive_path}")
        shutil.copy2(input_path, archive_path)
        
        if os.path.exists(archive_path) and os.path.getsize(archive_path) == os.path.getsize(input_path):
            logger.info(f"Archive verified. Deleting original: {input_path}")
            os.remove(input_path)
            parent = os.path.dirname(input_path)
            while parent != base_source:
                if not os.listdir(parent):
                    os.rmdir(parent)
                    parent = os.path.dirname(parent)
                else:
                    break
        else:
            raise Exception("Archive verification failed")

    def set_permissions(self, path, group, mode):
        subprocess.run(["chgrp", "-R", group, path])
        subprocess.run(["chmod", "-R", str(mode), path])

    def process_job(self, job_data):
        job_id = job_data['job_id']
        job_type = job_data['type']
        input_path = job_data['input_path']
        
        try:
            self.update_job_status(job_id, "processing", job_type=job_type)
            output_dir = self.get_output_dir(job_type, input_path, job_id=job_id)
            
            self.run_ffmpeg(job_id, input_path, output_dir, job_type)
            
            self.update_job_status(job_id, "verifying", job_type=job_type)
            if not self.verify_output(output_dir):
                raise Exception("Output verification failed: master.m3u8 missing or insufficient segments")
            
            self.set_permissions(output_dir, config.WEB_USER, 755)
            
            self.update_job_status(job_id, "archiving", job_type=job_type)
            self.archive_and_cleanup(job_type, input_path)
            
            self.update_job_status(job_id, "completed", progress=100, job_type=job_type)
            logger.info(f"Job {job_id} completed successfully")
            
        except Exception as e:
            logger.exception(f"Job {job_id} failed")
            self.update_job_status(job_id, "failed", error=str(e), job_type=job_type)

    def run(self):
        logger.info(f"Transcoder worker started and waiting for jobs on {config.LOCAL_QUEUE}...")
        while self.running:
            job_json = r.blpop(config.LOCAL_QUEUE, timeout=5)
            if job_json:
                job_data = json.loads(job_json[1])
                logger.info(f"Picked up job: {job_data['job_id']} ({job_data['type']})")
                self.process_job(job_data)
            
if __name__ == "__main__":
    worker = TranscoderWorker()
    worker.run()
