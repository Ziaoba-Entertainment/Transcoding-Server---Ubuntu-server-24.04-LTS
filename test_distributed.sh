#!/bin/bash
set -u
PASS=0
TOTAL=12

report() {
  if [ "$1" -eq 0 ]; then
    echo "[PASS] $2"
    PASS=$((PASS+1))
  else
    echo "[FAIL] $2"
  fi
}

REDIS_HOST="${REDIS_HOST:-127.0.0.1}"
REDIS_PORT="${REDIS_PORT:-6379}"
REDIS_PASSWORD="${REDIS_PASSWORD:-}"
AUTH_ARGS=()
[ -n "$REDIS_PASSWORD" ] && AUTH_ARGS=(-a "$REDIS_PASSWORD")

# 1 LAN redis
redis-cli -h "$REDIS_HOST" -p "$REDIS_PORT" "${AUTH_ARGS[@]}" PING >/dev/null 2>&1
report $? "Redis accessible from LAN endpoint ($REDIS_HOST:$REDIS_PORT)"

# 2 local redis
redis-cli -h 127.0.0.1 -p "$REDIS_PORT" "${AUTH_ARGS[@]}" PING >/dev/null 2>&1
report $? "Redis accessible locally"

# 3 samba shares
smbclient -L localhost -N >/dev/null 2>&1
report $? "Samba shares listable"

# 4 windows mount
[ -d /mnt/win_worker ]
report $? "/mnt/win_worker exists"

# 5 router service
systemctl is-active --quiet transcoder-router
report $? "transcoder-router service is running"

# 6 watcher service
systemctl is-active --quiet transcoder-win-watcher
report $? "transcoder-win-watcher service is running"

# 7 worker service
systemctl is-active --quiet transcoder-worker
report $? "transcoder-worker service is running"

# 8 heartbeat exists
redis-cli -h "$REDIS_HOST" -p "$REDIS_PORT" "${AUTH_ARGS[@]}" EXISTS worker:windows:heartbeat | grep -q '^1$'
report $? "Windows heartbeat key exists"

# 9 queue depths
echo "Queue depths:"
for q in transcode_queue local_transcode_queue windows_transcode_queue; do
  d=$(redis-cli -h "$REDIS_HOST" -p "$REDIS_PORT" "${AUTH_ARGS[@]}" LLEN "$q" 2>/dev/null)
  echo " - $q: ${d:-ERR}"
done
report 0 "Printed queue depths"

# 10 routing test
TEST_ID="test-router-$(date +%s)"
TEST_JOB="{\"job_id\":\"$TEST_ID\",\"type\":\"movie\",\"input_path\":\"/tmp/test.mkv\",\"status\":\"queued\",\"queued_at\":\"$(date -Iseconds)\"}"
redis-cli -h "$REDIS_HOST" -p "$REDIS_PORT" "${AUTH_ARGS[@]}" RPUSH transcode_queue "$TEST_JOB" >/dev/null
sleep 5
L=$(redis-cli -h "$REDIS_HOST" -p "$REDIS_PORT" "${AUTH_ARGS[@]}" LRANGE local_transcode_queue 0 -1)
W=$(redis-cli -h "$REDIS_HOST" -p "$REDIS_PORT" "${AUTH_ARGS[@]}" LRANGE windows_transcode_queue 0 -1)
if echo "$L$W" | grep -q "$TEST_ID"; then
  report 0 "Router moved test job to target queue"
else
  report 1 "Router did not move test job"
fi
redis-cli -h "$REDIS_HOST" -p "$REDIS_PORT" "${AUTH_ARGS[@]}" LREM transcode_queue 0 "$TEST_JOB" >/dev/null
redis-cli -h "$REDIS_HOST" -p "$REDIS_PORT" "${AUTH_ARGS[@]}" LREM local_transcode_queue 0 "$TEST_JOB" >/dev/null
redis-cli -h "$REDIS_HOST" -p "$REDIS_PORT" "${AUTH_ARGS[@]}" LREM windows_transcode_queue 0 "$TEST_JOB" >/dev/null

# 11 nginx paths not 502
code_hls=$(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1/hls/)
code_ads=$(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1/ads/)
if [ "$code_hls" != "502" ] && [ "$code_ads" != "502" ]; then report 0 "/hls and /ads respond without 502"; else report 1 "/hls or /ads returned 502"; fi

# 12 workers API JSON
curl -sf http://127.0.0.1:6666/api/workers/status | python3 -m json.tool >/dev/null 2>&1
report $? "webui /api/workers/status returns JSON"

echo "Summary: $PASS/$TOTAL tests passed"
