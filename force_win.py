# /opt/transcoder/force_win.py
import redis
import json
import uuid
import sys
import os
import config

def force_windows(path):
    if not os.path.exists(path):
        print(f"ERROR: Path does not exist: {path}")
        return

    r = redis.Redis(
        host=config.REDIS_HOST, 
        port=config.REDIS_PORT, 
        password=config.REDIS_PASSWORD, 
        decode_responses=True
    )

    job_id = str(uuid.uuid4())
    job = {
        "job_id": job_id,
        "input_path": os.path.abspath(path),
        "type": "movie" if "movies" in path.lower() else "tv",
        "force_worker": "windows",
        "priority": 9,
        "queued_at": __import__('datetime').datetime.now().isoformat()
    }

    r.rpush("transcode_queue", json.dumps(job))
    print(f"Successfully forced job to Windows Queue:")
    print(f"  ID:   {job_id}")
    print(f"  Path: {path}")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 force_win.py /path/to/media/file.mkv")
        sys.exit(1)
    force_windows(sys.argv[1])
