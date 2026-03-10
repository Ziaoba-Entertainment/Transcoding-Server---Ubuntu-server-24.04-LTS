@echo off
setlocal EnableExtensions DisableDelayedExpansion

echo =======================================================
echo Windows Transcoder Worker Setup
echo =======================================================

:: 1. Check for Python
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo ERROR: Python is not installed or not in PATH.
    echo Please install Python 3.10+ from python.org
    pause
    exit /b 1
)

:: 2. Install Dependencies
echo Installing Python dependencies...
python -m pip install --upgrade pip
python -m pip install redis psutil requests
if %errorlevel% neq 0 (
    echo ERROR: Failed to install dependencies.
    pause
    exit /b 1
)

:: 3. Create Directories
if not exist "C:\transcoder" mkdir "C:\transcoder"
if not exist "C:\transcoder\logs" mkdir "C:\transcoder\logs"

:: 4. Map Network Drives
echo Mapping network drives...
:: Hardcoded SMB Credentials
set SMB_USER=transcoder
set SMB_PASS=TranscoderSMB2024!
set SOURCE_SHARE=\\192.168.0.103\media_raw
set OUTPUT_SHARE=\\192.168.0.103\win_output

net use Z: /delete /y >nul 2>&1
net use Z: %SOURCE_SHARE% /user:%SMB_USER% %SMB_PASS% /persistent:yes
if %errorlevel% neq 0 echo WARNING: Failed to map Z: drive.

net use Y: /delete /y >nul 2>&1
net use Y: %OUTPUT_SHARE% /user:%SMB_USER% %SMB_PASS% /persistent:yes
if %errorlevel% neq 0 echo WARNING: Failed to map Y: drive.

:: 5. Write windows_worker.py using PowerShell Here-String
echo Writing windows_worker.py...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$content = @'`nimport os`nimport time`nimport json`nimport redis`nimport logging`nimport subprocess`nimport socket`nimport uuid`nimport shutil`nfrom datetime import datetime`nfrom concurrent.futures import ThreadPoolExecutor`n`n# HARDCODED CREDENTIALS`nREDIS_HOST = '192.168.0.103'`nREDIS_PORT = 6379`nREDIS_PASSWORD = 'TranscoderRedis2024!'`n`n# REDIS KEYS`nWINDOWS_QUEUE = 'windows_transcode_queue'`nWIN_HEARTBEAT_KEY = 'worker:windows:heartbeat'`nHISTORY_PREFIX = 'job_history:'`n`n# PATHS`nLOG_FILE = r'C:\transcoder\logs\worker.log'`nFFMPEG_PATH = 'ffmpeg.exe'  # Assumes ffmpeg is in PATH`n`n# STREAM CONFIGURATION`nSTREAMS = [`n    {'name':'1080p','gpu':0,'w':1920,'h':1080,'vb':'2500k','maxr':'3750k','bufs':'5000k','idx':0},`n    {'name':'720p', 'gpu':1,'w':1280,'h':720, 'vb':'1500k','maxr':'2250k','bufs':'3000k','idx':1},`n    {'name':'480p', 'gpu':1,'w':854, 'h':480, 'vb':'800k', 'maxr':'1200k','bufs':'1600k','idx':2},`n    {'name':'360p', 'gpu':0,'w':640, 'h':360, 'vb':'500k', 'maxr':'750k', 'bufs':'1000k','idx':3},`n]`n`n# Setup logging`nlogging.basicConfig(`n    level=logging.INFO,`n    format='%%(asctime)s [%%(levelname)s] %%(message)s',`n    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()]`n)`nlogger = logging.getLogger(__name__)`n`nr = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, password=REDIS_PASSWORD, decode_responses=True)`n`ndef send_heartbeat():`n    try:`n        hb_data = {`n            'hostname': socket.gethostname(),`n            'ip': socket.gethostbyname(socket.gethostname()),`n            'gpus': 2,`n            'updated_at': datetime.now().isoformat()`n        }`n        r.set(WIN_HEARTBEAT_KEY, json.dumps(hb_data), ex=45)`n    except Exception as e:`n        logger.error(f'Heartbeat failed: {e}')`n`ndef transcode_stream(stream, input_path, output_dir):`n    try:`n        stream_name = stream['name']`n        stream_idx = stream['idx']`n        gpu_id = stream['gpu']`n        `n        out_m3u8 = os.path.join(output_dir, f'stream_{stream_idx}.m3u8')`n        out_ts = os.path.join(output_dir, f'stream_{stream_idx}_%%03d.ts')`n        `n        cmd = [`n            FFMPEG_PATH, '-y', '-hwaccel', 'cuda', '-hwaccel_device', str(gpu_id),`n            '-i', input_path,`n            '-map', '0:v:0', '-map', '0:a:0',`n            '-c:v', 'h264_nvenc', '-gpu', str(gpu_id),`n            '-preset', 'p4', '-tune', 'hq',`n            '-b:v', stream['vb'], '-maxrate', stream['maxr'], '-bufsize', stream['bufs'],`n            '-s', f'{stream[\"w\"]}x{stream[\"h\"]}',`n            '-c:a', 'aac', '-b:a', '128k', '-ac', '2',`n            '-f', 'hls', '-hls_time', '6', '-hls_list_size', '0',`n            '-hls_segment_filename', out_ts,`n            out_m3u8`n        ]`n        `n        logger.info(f'Starting stream {stream_name} on GPU {gpu_id}')`n        subprocess.run(cmd, check=True, capture_output=True)`n        return True`n    except Exception as e:`n        logger.error(f'Stream {stream[\"name\"]} failed: {e}')`n        return False`n`ndef process_job(job):`n    job_id = job['job_id']`n    input_path = job['input_path']`n    `n    # Map local paths (Z: for source, Y: for output)`n    # Input: /srv/media_raw/movies/Title_(2024)/file.mkv -> Z:\movies\Title_(2024)\file.mkv`n    rel_input = input_path.replace('/srv/media_raw/', '').replace('/', '\\\\')`n    local_input = os.path.join('Z:', rel_input)`n    `n    # Output directory on Y:`n    # We follow the same structure as master`n    folder_name = os.path.basename(os.path.dirname(input_path))`n    if job['type'] == 'movie':`n        local_output_dir = os.path.join('Y:', 'movies', folder_name)`n    else:`n        local_output_dir = os.path.join('Y:', 'tv', folder_name) # simplified`n        `n    if not os.path.exists(local_output_dir):`n        os.makedirs(local_output_dir, exist_ok=True)`n        `n    logger.info(f'Processing job {job_id}: {local_input}')`n    r.hset(f'{HISTORY_PREFIX}{job_id}', 'status', 'processing')`n    r.hset(f'{HISTORY_PREFIX}{job_id}', 'worker', 'windows')`n    r.hset(f'{HISTORY_PREFIX}{job_id}', 'started_at', datetime.now().isoformat())`n    `n    with ThreadPoolExecutor(max_workers=4) as executor:`n        futures = [executor.submit(transcode_stream, s, local_input, local_output_dir) for s in STREAMS]`n        results = [f.result() for f in futures]`n        `n    if all(results):`n        # Write master.m3u8`n        master_content = '#EXTM3U\\n#EXT-X-VERSION:3\\n'`n        for s in STREAMS:`n            bandwidth = int(s['vb'].replace('k', '')) * 1000`n            master_content += f'#EXT-X-STREAM-INF:BANDWIDTH={bandwidth},RESOLUTION={s[\"w\"]}x{s[\"h\"]}\\nstream_{s[\"idx\"]}.m3u8\\n'`n            `n        with open(os.path.join(local_output_dir, 'master.m3u8'), 'w') as f:`n            f.write(master_content)`n            `n        # Write completion flag`n        flag_data = {`n            'job_id': job_id,`n            'status': 'completed',`n            'completed_at': datetime.now().isoformat(),`n            'worker': 'windows'`n        }`n        with open(os.path.join(local_output_dir, 'transcode_complete.flag'), 'w') as f:`n            f.write(json.dumps(flag_data))`n            `n        r.hset(f'{HISTORY_PREFIX}{job_id}', 'status', 'completed')`n        r.hset(f'{HISTORY_PREFIX}{job_id}', 'completed_at', datetime.now().isoformat())`n        logger.info(f'Job {job_id} completed successfully')`n    else:`n        r.hset(f'{HISTORY_PREFIX}{job_id}', 'status', 'failed')`n        r.hset(f'{HISTORY_PREFIX}{job_id}', 'error', 'One or more streams failed')`n        logger.error(f'Job {job_id} failed')`n`ndef main():`n    logger.info('Windows Worker started')`n    while True:`n        send_heartbeat()`n        `n        # Check for drives`n        if not os.path.exists('Z:'):`n            logger.warning('Z: drive missing, attempting to remap...')`n            os.system('net use Z: \\\\\\\\192.168.0.103\\\\media_raw /user:transcoder TranscoderSMB2024! /persistent:yes')`n        if not os.path.exists('Y:'):`n            logger.warning('Y: drive missing, attempting to remap...')`n            os.system('net use Y: \\\\\\\\192.168.0.103\\\\win_output /user:transcoder TranscoderSMB2024! /persistent:yes')`n            `n        job_raw = r.blpop(WINDOWS_QUEUE, timeout=10)`n        if job_raw:`n            try:`n                job = json.loads(job_raw[1])`n                process_job(job)`n            except Exception as e:`n                logger.error(f'Error processing job: {e}')`n        time.sleep(1)`n`nif __name__ == \"__main__\":`n    main()`n'@; `
   [System.IO.File]::WriteAllText('C:\transcoder\windows_worker.py', $content)"

:: 6. Validate Syntax
echo Validating Python syntax...
python -c "import ast; ast.parse(open(r'C:\transcoder\windows_worker.py').read()); print('Syntax OK')"
if %errorlevel% neq 0 (
    echo ERROR: Python syntax validation failed.
    pause
    exit /b 1
)

echo.
echo Setup complete!
echo To start the worker, run: python C:\transcoder\windows_worker.py
echo.
pause
