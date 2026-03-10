#!/bin/bash

echo "=== Distributed Transcoder Test ==="

# 1. Check Redis
echo "Checking Redis connection..."
redis-cli ping > /dev/null 2>&1
if [ $? -ne 0 ]; then
    echo "ERROR: Redis not running!"
    exit 1
fi
echo "OK: Redis is running."

# 2. Clear queues for testing
echo "Clearing test queues..."
redis-cli del transcode_queue local_transcode_queue windows_transcode_queue worker:windows:heartbeat > /dev/null

# 3. Start Router in background (if not running)
pgrep -f job_router.py > /dev/null
if [ $? -ne 0 ]; then
    echo "Starting job_router.py..."
    python3 job_router.py &
    ROUTER_PID=$!
    sleep 2
else
    echo "Router already running."
fi

# 4. Test Routing to Local (Windows Offline)
echo "Testing routing to LOCAL (Windows Offline)..."
JOB_ID="test_local_$(date +%s)"
redis-cli rpush transcode_queue "{\"job_id\": \"$JOB_ID\", \"type\": \"movie\", \"input_path\": \"/tmp/test.mkv\"}" > /dev/null
sleep 2

LEN=$(redis-cli llen local_transcode_queue)
if [ "$LEN" -eq "1" ]; then
    echo "SUCCESS: Job routed to local_transcode_queue"
else
    echo "FAILED: Job not found in local_transcode_queue (Len: $LEN)"
fi

# 5. Test Routing to Windows (Windows Online)
echo "Testing routing to WINDOWS (Windows Online)..."
redis-cli setex worker:windows:heartbeat 60 "online" > /dev/null
JOB_ID_WIN="test_win_$(date +%s)"
redis-cli rpush transcode_queue "{\"job_id\": \"$JOB_ID_WIN\", \"type\": \"movie\", \"input_path\": \"/tmp/test_win.mkv\"}" > /dev/null
sleep 2

LEN_WIN=$(redis-cli llen windows_transcode_queue)
if [ "$LEN_WIN" -eq "1" ]; then
    echo "SUCCESS: Job routed to windows_transcode_queue"
else
    echo "FAILED: Job not found in windows_transcode_queue (Len: $LEN_WIN)"
fi

# Cleanup
if [ ! -z "$ROUTER_PID" ]; then
    kill $ROUTER_PID
fi

echo "=== Test Complete ==="
