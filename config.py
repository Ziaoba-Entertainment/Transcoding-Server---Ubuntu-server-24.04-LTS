# config.py
import os
from dotenv import load_dotenv

# Load standardized Redis credentials if available
if os.path.exists("/etc/ziaoba/redis.env"):
    try:
        # Only try to load if we have read access to avoid PermissionError
        if os.access("/etc/ziaoba/redis.env", os.R_OK):
            load_dotenv("/etc/ziaoba/redis.env")
    except Exception:
        pass # Fallback to environment variables already set by systemd

def get_env_int(name, default):
    val = os.environ.get(name)
    if not val or not val.strip():
        return default
    try:
        return int(val)
    except ValueError:
        return default

# --- PATHS ---
# Source directories (where Sonarr/Radarr download to)
SOURCE_BASE_MOVIES = "/srv/media_raw/movies"
SOURCE_BASE_TV = "/srv/media_raw/tv"

# Output directories (Nginx HLS delivery)
OUTPUT_BASE_MOVIES = "/srv/vod/hls/movies"
OUTPUT_BASE_TV = "/srv/vod/hls/tv"
OUTPUT_BASE_ADS = "/srv/vod/ads"

# Archive directories (Original file backup)
ARCHIVE_BASE_MOVIES = "/srv/downloads/movies"
ARCHIVE_BASE_TV = "/srv/downloads/tv"
ARCHIVE_BASE_ADS = "/srv/downloads/ads"

# Logging
LOG_DIR = "/var/log/transcoder"
WORKER_LOG = os.path.join(LOG_DIR, "worker.log")
WEBHOOK_LOG = os.path.join(LOG_DIR, "webhook.log")
SCANNER_LOG = os.path.join(LOG_DIR, "scanner.log")
WEBUI_LOG = os.path.join(LOG_DIR, "webui.log")
WIN_LOG_CHANNEL = "logs:windows"
LOCAL_LOG_CHANNEL = "logs:local"

# --- REDIS SETTINGS ---
REDIS_HOST = os.environ.get("REDIS_HOST", "127.0.0.1")
REDIS_PORT = get_env_int("REDIS_PORT", 6379)
REDIS_PASSWORD = os.environ.get("REDIS_PASSWORD")
REDIS_DB = get_env_int("REDIS_DB", 0)
REDIS_DB_ADS = get_env_int("REDIS_DB_ADS", 1)

# Queue names
TRANSCODE_QUEUE = "transcode_queue"         # webhook_receiver pushes here
LOCAL_QUEUE = "local_transcode_queue"       # transcoder_worker reads this
WINDOWS_QUEUE = "windows_transcode_queue"   # windows worker reads this
QUEUE_NAME = TRANSCODE_QUEUE                # Legacy alias

# Worker detection
WIN_HEARTBEAT_KEY = "worker:windows:heartbeat"
WIN_HEARTBEAT_TTL = 60

# Windows output
WIN_OUTPUT_MOUNT = "/mnt/win_worker"

# Router status key
ROUTER_STATUS_KEY = "router:status"

HISTORY_PREFIX = "job_history:"
ACTIVE_JOB_KEY = "active_transcode_job"
WIN_ACTIVE_JOB_KEY = "active_transcode_job:windows"
PATH_INDEX_PREFIX = "path_index:"
AD_REGISTRY_KEY = "ad_registry"
AD_META_PREFIX = "ad_meta:"
AD_PLAYS_PREFIX = "ad_plays:"
ADVERTISER_INDEX_KEY = "advertiser:index"
ADVERTISER_ADS_PREFIX = "advertiser:ads:"
ADS_DISABLED_KEY = "ads:disabled"

# --- FFMPEG SETTINGS ---
VAAPI_DEVICE = "/dev/dri/renderD128"
FFMPEG_BINARY = "ffmpeg"

# Optimized FFmpeg Template for RX 560 (Pure GPU Pipeline)
FFMPEG_CMD_TEMPLATE = [
    "ffmpeg", "-hide_banner", "-y",
    "-hwaccel", "vaapi", "-hwaccel_device", VAAPI_DEVICE, "-hwaccel_output_format", "vaapi",
    "-threads", "4", "-probesize", "50M", "-analyzeduration", "50M",
    "-fflags", "+igndts", "-avoid_negative_ts", "make_zero",
    "-i", "{input_path}",
    "-filter_complex",
    "[0:v]scale_vaapi=w=iw:h=ih:format=nv12,split=4[v1][v2][v3][v4]; [v1]scale_vaapi=w=1920:h=1080[v1out]; [v2]scale_vaapi=w=1280:h=720[v2out]; [v3]scale_vaapi=w=854:h=480[v3out]; [v4]scale_vaapi=w=640:h=360[v4out]; [0:a:0]aresample=async=1,asplit=4[a1][a2][a3][a4]",
    "-map", "[v1out]", "-c:v:0", "h264_vaapi", "-rc_mode", "VBR", "-b:v:0", "2200k", "-maxrate:v:0", "3300k", "-bufsize:v:0", "4400k", "-bf", "0",
    "-map", "[a1]", "-c:a:0", "aac", "-b:a:0", "128k", "-ar", "48000", "-ac", "2",
    "-map", "[v2out]", "-c:v:1", "h264_vaapi", "-rc_mode", "VBR", "-b:v:1", "1500k", "-maxrate:v:1", "2250k", "-bufsize:v:1", "3000k", "-bf", "0",
    "-map", "[a2]", "-c:a:1", "aac", "-b:a:1", "128k", "-ar", "48000", "-ac", "2",
    "-map", "[v3out]", "-c:v:2", "h264_vaapi", "-rc_mode", "VBR", "-b:v:2", "800k", "-maxrate:v:2", "1200k", "-bufsize:v:2", "1600k", "-bf", "0",
    "-map", "[a3]", "-c:a:2", "aac", "-b:a:2", "128k", "-ar", "48000", "-ac", "2",
    "-map", "[v4out]", "-c:v:3", "h264_vaapi", "-rc_mode", "VBR", "-b:v:3", "600k", "-maxrate:v:3", "900k", "-bufsize:v:3", "1200k", "-bf", "0",
    "-map", "[a4]", "-c:a:3", "aac", "-b:a:3", "128k", "-ar", "48000", "-ac", "2",
    "-g", "48", "-keyint_min", "48",
    "-f", "hls", "-hls_time", "6", "-hls_list_size", "0",
    "-hls_playlist_type", "vod",
    "-hls_segment_type", "mpegts",
    "-hls_flags", "independent_segments+split_by_time",
    "-hls_segment_filename", "{output_dir}/stream_%v_%03d.ts",
    "-master_pl_name", "master.m3u8",
    "-var_stream_map", "v:0,a:0 v:1,a:1 v:2,a:2 v:3,a:3",
    "-max_muxing_queue_size", "1024",
    "{output_dir}/stream_%v.m3u8"
]

# --- WEB SETTINGS ---
WEBUI_PORT = get_env_int("WEBUI_PORT", 6666)
WEBHOOK_PORT = get_env_int("WEBHOOK_PORT", 6667)
AD_ADMIN_URL = os.environ.get("AD_ADMIN_URL", "http://localhost:8089")
ADSERVER_INTERNAL_PORT = get_env_int("ADSERVER_INTERNAL_PORT", 8083)

# --- PUBLIC ACCESS ---
PUBLIC_IP = os.environ.get("PUBLIC_IP", "192.168.0.103")
PUBLIC_PORT = get_env_int("PUBLIC_PORT", 8081)
PUBLIC_DOMAIN = os.environ.get("PUBLIC_DOMAIN", "stream.ziaoba.com")

# --- PERMISSIONS ---
SERVICE_USER = "media"
WEB_USER = "www-data"

# --- UTILS ---
def sanitize_path(path_segment):
    import re
    sanitized = re.sub(r'[^\w\s\-\(\).]', '', path_segment)
    sanitized = sanitized.replace(' ', '_')
    return sanitized

def get_output_dir(job_type, input_path, job_id=None):
    if job_type == 'ad':
        return os.path.join(OUTPUT_BASE_ADS, job_id)
        
    base_source = SOURCE_BASE_MOVIES if job_type == 'movie' else SOURCE_BASE_TV
    base_output = OUTPUT_BASE_MOVIES if job_type == 'movie' else OUTPUT_BASE_TV
    
    try:
        rel_path = os.path.relpath(input_path, base_source)
    except ValueError:
        # If input_path is not under base_source, fallback to a safe folder
        return os.path.join(base_output, "unknown", job_id or "unknown")
        
    path_parts = rel_path.split(os.sep)
    sanitized_parts = [sanitize_path(p) for p in path_parts[:-1]]
    
    if job_type == 'tv':
        filename = os.path.splitext(path_parts[-1])[0]
        sanitized_parts.append(sanitize_path(filename))
    
    return os.path.join(base_output, *sanitized_parts)

# --- VIDEO EXTENSIONS ---
VIDEO_EXTENSIONS = ('.mp4', '.mkv', '.avi', '.mov', '.m4v', '.ts')
