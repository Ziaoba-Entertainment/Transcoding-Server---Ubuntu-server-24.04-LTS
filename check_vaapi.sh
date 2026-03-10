#!/bin/bash
# check_vaapi.sh

echo "=== VAAPI Pre-flight Check ==="

# 1. Check device existence and permissions
if [ -e /dev/dri/renderD128 ]; then
    echo "[OK] /dev/dri/renderD128 exists."
    ls -l /dev/dri/renderD128
else
    echo "[ERROR] /dev/dri/renderD128 NOT found!"
fi

# 2. Check media user groups
if id -nG media | grep -qE "render|video"; then
    echo "[OK] User 'media' is in correct groups."
else
    echo "[WARNING] User 'media' might be missing 'render' or 'video' groups."
fi

# 3. Check vainfo
if command -v vainfo > /dev/null; then
    echo "[INFO] Running vainfo..."
    vainfo --display drm --device /dev/dri/renderD128 | grep -E "VA-API version|H.264|VAEntrypointEncSlice"
else
    echo "[ERROR] vainfo not installed."
fi

# 4. Check FFmpeg VAAPI support
if command -v ffmpeg > /dev/null; then
    echo "[INFO] Checking FFmpeg hwaccels..."
    ffmpeg -hwaccels | grep vaapi
else
    echo "[ERROR] ffmpeg not installed."
fi

# 5. GPU Utilization (AMD)
if [ -e /sys/class/drm/card0/device/gpu_busy_percent ]; then
    echo "[INFO] Current GPU Load: $(cat /sys/class/drm/card0/device/gpu_busy_percent)%"
fi

echo "=== Check Complete ==="
