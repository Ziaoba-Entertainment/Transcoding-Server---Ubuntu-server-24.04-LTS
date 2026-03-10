# config.py
import os

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

# --- REDIS SETTINGS ---
REDIS_HOST = '127.0.0.1'
REDIS_PORT = 6379
REDIS_PASSWORD = os.getenv('REDIS_PASSWORD', '')
REDIS_DB = 0
REDIS_DB_ADS = 1
TRANSCODE_QUEUE = 'transcode_queue'
LOCAL_QUEUE = 'local_transcode_queue'
WINDOWS_QUEUE = 'windows_transcode_queue'
QUEUE_NAME = TRANSCODE_QUEUE
HISTORY_PREFIX = "job_history:"
ACTIVE_JOB_KEY = "active_transcode_job"
WIN_HEARTBEAT_KEY = 'worker:windows:heartbeat'
WIN_HEARTBEAT_TTL = 60
WIN_OUTPUT_MOUNT = '/mnt/win_worker'
ROUTER_STATUS_KEY = 'router:status'
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
    "-threads", "0", "-probesize", "50M", "-analyzeduration", "50M",
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
    "-max_muxing_queue_size", "9999",
    "{output_dir}/stream_%v.m3u8"
]

# --- WEB SETTINGS ---
WEBUI_PORT = 6666
WEBHOOK_PORT = 6667

# --- PERMISSIONS ---
SERVICE_USER = "media"
WEB_USER = "www-data"

# --- VIDEO EXTENSIONS ---
VIDEO_EXTENSIONS = ('.mp4', '.mkv', '.avi', '.mov', '.m4v', '.ts')
