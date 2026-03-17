# monitor_redis.py
import redis
import os
import json
import sys
from dotenv import load_dotenv

# Standard location
REDIS_ENV_PATH = "/etc/ziaoba/redis.env"

def monitor():
    print(f"--- Redis Monitoring Check ---")
    
    # 1. Read the file
    if not os.path.exists(REDIS_ENV_PATH):
        print(f"ERROR: Credential file not found at {REDIS_ENV_PATH}")
        return False
    
    try:
        if os.access(REDIS_ENV_PATH, os.R_OK):
            load_dotenv(REDIS_ENV_PATH)
            print(f"SUCCESS: Loaded credentials from {REDIS_ENV_PATH}")
        else:
            print(f"WARNING: No read access to {REDIS_ENV_PATH}, using existing environment variables")
    except Exception as e:
        print(f"WARNING: Failed to read credential file: {e}")
        # Continue anyway, might have env vars set

    host = os.environ.get("REDIS_HOST")
    port = os.environ.get("REDIS_PORT", 6379)
    password = os.environ.get("REDIS_PASSWORD")

    if not host or not password:
        print(f"ERROR: Missing REDIS_HOST or REDIS_PASSWORD in {REDIS_ENV_PATH}")
        return False

    # 2. Validate connection
    try:
        r = redis.Redis(
            host=host,
            port=int(port),
            password=password,
            socket_timeout=5,
            decode_responses=True
        )
        r.ping()
        print(f"SUCCESS: Connected to Redis at {host}:{port}")
    except Exception as e:
        print(f"ERROR: Redis connection failed: {e}")
        return False

    # 3. Check queue health
    queues = ["transcode_queue", "local_transcode_queue", "windows_transcode_queue"]
    print(f"--- Queue Health ---")
    for q in queues:
        try:
            length = r.llen(q)
            print(f"Queue '{q}': {length} jobs")
        except Exception as e:
            print(f"ERROR: Failed to check queue '{q}': {e}")

    # Check worker heartbeats
    try:
        win_hb = r.get("worker:windows:heartbeat")
        if win_hb:
            hb_data = json.loads(win_hb)
            print(f"Windows Worker: Online (Last heartbeat: {hb_data.get('updated_at')})")
        else:
            print(f"Windows Worker: Offline (No heartbeat found)")
    except Exception as e:
        print(f"ERROR: Failed to check worker heartbeats: {e}")

    return True

if __name__ == "__main__":
    if not monitor():
        sys.exit(1)
    sys.exit(0)
