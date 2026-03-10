# webui.py
import os
import json
import redis
import psutil
import logging
import uuid
import time
import shutil
from flask import Flask, render_template_string, jsonify, Response, request, send_from_directory
from werkzeug.utils import secure_filename
from datetime import datetime
import subprocess
import config

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 20 * 1024 * 1024 * 1024  # 20GB

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(config.WEBUI_LOG),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

r = redis.Redis(host=config.REDIS_HOST, port=config.REDIS_PORT, password=getattr(config, 'REDIS_PASSWORD', None) or None, db=config.REDIS_DB, decode_responses=True)
r_ads = redis.Redis(host=config.REDIS_HOST, port=config.REDIS_PORT, password=getattr(config, 'REDIS_PASSWORD', None) or None, db=config.REDIS_DB_ADS, decode_responses=True)

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Transcoder Dashboard</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <script src="https://cdn.jsdelivr.net/npm/hls.js@latest"></script>
    <style>
        body { background-color: #0f172a; color: #e2e8f0; font-family: 'Inter', sans-serif; }
        .card { background-color: #1e293b; border: 1px solid #334155; }
        .progress-bar { transition: width 0.3s ease-in-out; }
        .tab-active { border-bottom: 2px solid #6366f1; color: #6366f1; }
        .status-badge { font-size: 0.7rem; padding: 0.1rem 0.4rem; border-radius: 0.25rem; font-weight: bold; text-transform: uppercase; }
        .status-queued { background-color: #854d0e; color: #fef08a; }
        .status-processing { background-color: #1e40af; color: #bfdbfe; animation: pulse 2s infinite; }
        .status-verifying { background-color: #1e40af; color: #bfdbfe; }
        .status-archiving { background-color: #1e40af; color: #bfdbfe; }
        .status-completed { background-color: #065f46; color: #a7f3d0; }
        .status-failed { background-color: #991b1b; color: #fecaca; }
        .status-removed { background-color: #475569; color: #cbd5e1; }
        .status-active { background-color: #065f46; color: #a7f3d0; }
        .status-exhausted { background-color: #92400e; color: #fef3c7; }
        @keyframes pulse { 0% { opacity: 1; } 50% { opacity: 0.6; } 100% { opacity: 1; } }
    </style>
</head>
<body class="p-4 md:p-8">
    <div class="max-w-7xl mx-auto">
        <header class="flex justify-between items-center mb-8">
            <h1 class="text-3xl font-bold text-white">Transcoder <span class="text-indigo-500">Pipeline</span></h1>
            <div class="flex space-x-4 text-sm font-medium">
                <button onclick="showTab('queue')" id="tab-queue" class="pb-2 px-1 tab-active">Queue</button>
                <button onclick="showTab('completed')" id="tab-completed" class="pb-2 px-1 text-slate-400 hover:text-white">Completed</button>
                <button onclick="showTab('ads')" id="tab-ads" class="pb-2 px-1 text-slate-400 hover:text-white">Advertisements</button>
                <button onclick="showTab('system')" id="tab-system" class="pb-2 px-1 text-slate-400 hover:text-white">System</button>
                <button onclick="retryAllFailed()" class="bg-yellow-600 hover:bg-yellow-500 px-3 py-1 rounded text-xs font-bold transition-colors ml-4">Retry All Failed</button>
                <button onclick="triggerScan()" class="bg-indigo-600 hover:bg-indigo-500 px-3 py-1 rounded text-xs font-bold transition-colors ml-4">Scan Folders</button>
            </div>
        </header>

        <!-- Tab: Queue -->
        <div id="content-queue" class="tab-content space-y-6">
            <!-- Active Job -->
            <div id="active-job-section" class="card p-6 rounded-xl shadow-lg hidden">
                <h2 class="text-xl font-semibold mb-4 flex items-center">
                    <span class="w-3 h-3 bg-green-500 rounded-full mr-2 animate-pulse"></span>
                    Now Transcoding
                </h2>
                <div id="active-job-details">
                    <p class="text-lg font-medium" id="active-filename"></p>
                    <p class="text-sm text-slate-400" id="active-type"></p>
                    <p class="text-sm text-slate-400 mb-4" id="active-worker"></p>
                    <div class="w-full bg-slate-700 rounded-full h-4 mb-2">
                        <div id="active-progress" class="bg-indigo-500 h-4 rounded-full progress-bar" style="width: 0%"></div>
                    </div>
                    <div class="flex justify-between text-xs text-slate-400 mb-4">
                        <span id="active-percent">0%</span>
                        <span id="active-status">Processing</span>
                    </div>
                    <div class="bg-black p-4 rounded font-mono text-[10px] overflow-hidden">
                        <pre id="active-logs" class="text-green-400 whitespace-pre-wrap"></pre>
                    </div>
                </div>
            </div>

            <div class="grid grid-cols-1 md:grid-cols-5 gap-4">
                <div class="card p-4 rounded-xl">
                    <p class="text-xs text-slate-400 uppercase font-bold">Total Jobs</p>
                    <p id="worker-stat-total" class="text-2xl font-bold">0</p>
                </div>
                <div class="card p-4 rounded-xl">
                    <p class="text-xs text-green-400 uppercase font-bold">✅ Completed</p>
                    <p id="worker-stat-completed" class="text-2xl font-bold text-green-400">0</p>
                </div>
                <div class="card p-4 rounded-xl">
                    <p class="text-xs text-red-400 uppercase font-bold">❌ Failed</p>
                    <p id="worker-stat-failed" class="text-2xl font-bold text-red-400">0</p>
                </div>
                <div class="card p-4 rounded-xl">
                    <p class="text-xs text-indigo-400 uppercase font-bold">🖥 Local</p>
                    <p id="worker-stat-local" class="text-2xl font-bold text-indigo-400">0</p>
                </div>
                <div class="card p-4 rounded-xl">
                    <p class="text-xs text-cyan-400 uppercase font-bold">🪟 Windows</p>
                    <p id="worker-stat-windows" class="text-2xl font-bold text-cyan-400">0</p>
                </div>
            </div>

            <div class="card p-6 rounded-xl shadow-lg">
                <h2 class="text-xl font-semibold mb-4">Pending Queue</h2>
                <div class="overflow-x-auto">
                    <table class="w-full text-left">
                        <thead>
                            <tr class="border-b border-slate-700 text-slate-400 text-sm">
                                <th class="pb-2 cursor-pointer" onclick="sortTable('queue-body', 0)">Pos</th>
                                <th class="pb-2 cursor-pointer" onclick="sortTable('queue-body', 1)">Filename</th>
                                <th class="pb-2 cursor-pointer" onclick="sortTable('queue-body', 2)">Type</th>
                                <th class="pb-2 cursor-pointer" onclick="sortTable('queue-body', 3)">Queued At</th>
                                <th class="pb-2">Worker</th>
                                <th class="pb-2">Action</th>
                            </tr>
                        </thead>
                        <tbody id="queue-body"></tbody>
                    </table>
                </div>
            </div>
        </div>

        <!-- Tab: Completed -->
        <div id="content-completed" class="tab-content hidden space-y-6">
            <!-- Stats Bar -->
            <div class="grid grid-cols-3 gap-4">
                <div onclick="filterHistoryByStatus('all')" class="card p-4 rounded-xl cursor-pointer hover:bg-slate-800 transition-colors">
                    <p class="text-xs text-slate-400 uppercase font-bold">Total Jobs</p>
                    <p id="hist-stat-total" class="text-2xl font-bold">0</p>
                </div>
                <div onclick="filterHistoryByStatus('completed')" class="card p-4 rounded-xl cursor-pointer hover:bg-slate-800 transition-colors">
                    <p class="text-xs text-green-400 uppercase font-bold">✅ Completed</p>
                    <p id="hist-stat-completed" class="text-2xl font-bold text-green-400">0</p>
                </div>
                <div onclick="filterHistoryByStatus('failed')" class="card p-4 rounded-xl cursor-pointer hover:bg-slate-800 transition-colors">
                    <p class="text-xs text-red-400 uppercase font-bold">❌ Failed</p>
                    <p id="hist-stat-failed" class="text-2xl font-bold text-red-400">0</p>
                </div>
@@ -153,50 +178,51 @@ HTML_TEMPLATE = """
                        <option value="movie">🎬 Movie</option>
                        <option value="tv">📺 TV Episode</option>
                        <option value="ad">📢 Advertisement</option>
                    </select>
                </div>
                <div>
                    <label class="block text-xs text-slate-400 uppercase font-bold mb-1">From</label>
                    <input type="date" id="hist-filter-from" onchange="loadHistory()" class="bg-slate-800 border border-slate-700 rounded px-3 py-2 text-sm focus:outline-none focus:border-indigo-500">
                </div>
                <div>
                    <label class="block text-xs text-slate-400 uppercase font-bold mb-1">To</label>
                    <input type="date" id="hist-filter-to" onchange="loadHistory()" class="bg-slate-800 border border-slate-700 rounded px-3 py-2 text-sm focus:outline-none focus:border-indigo-500">
                </div>
                <button onclick="clearHistoryFilters()" class="bg-slate-700 hover:bg-slate-600 px-4 py-2 rounded text-sm font-bold transition-colors">Clear</button>
            </div>

            <div class="card p-6 rounded-xl shadow-lg">
                <div class="overflow-x-auto">
                    <table class="w-full text-left">
                        <thead>
                            <tr class="border-b border-slate-700 text-slate-400 text-sm">
                                <th class="pb-2 w-12">Type</th>
                                <th class="pb-2 cursor-pointer" onclick="sortHistory('input_path')">Title / Filename</th>
                                <th class="pb-2 cursor-pointer" onclick="sortHistory('status')">Status</th>
                                <th class="pb-2 cursor-pointer" onclick="sortHistory('queued_at')">Queued</th>
                                <th class="pb-2">Worker</th>
                                <th class="pb-2 cursor-pointer" onclick="sortHistory('end_time')">Duration</th>
                                <th class="pb-2">Actions</th>
                            </tr>
                        </thead>
                        <tbody id="history-body"></tbody>
                    </table>
                </div>
                <!-- Pagination -->
                <div class="mt-6 flex justify-between items-center text-sm text-slate-400">
                    <div id="hist-pagination-info">Showing 0 to 0 of 0 jobs</div>
                    <div class="flex space-x-2">
                        <button id="hist-prev-page" onclick="changeHistoryPage(-1)" class="px-3 py-1 bg-slate-800 border border-slate-700 rounded disabled:opacity-50">Previous</button>
                        <button id="hist-next-page" onclick="changeHistoryPage(1)" class="px-3 py-1 bg-slate-800 border border-slate-700 rounded disabled:opacity-50">Next</button>
                    </div>
                </div>
            </div>
        </div>

        <!-- Tab: Advertisements -->
        <div id="content-ads" class="tab-content hidden space-y-6">
            <!-- Stats Bar -->
            <div class="grid grid-cols-1 md:grid-cols-5 gap-4">
                <div onclick="filterAdsByStatus('all')" class="card p-4 rounded-xl cursor-pointer hover:bg-slate-800 transition-colors">
                    <p class="text-[10px] text-slate-400 uppercase font-bold">Total Ads</p>
                    <p id="ad-stat-total" class="text-xl font-bold">0</p>
@@ -326,50 +352,59 @@ HTML_TEMPLATE = """

        <!-- Tab: System -->
        <div id="content-system" class="tab-content hidden space-y-6">
            <div class="grid grid-cols-1 md:grid-cols-2 gap-6">
                <div class="card p-6 rounded-xl shadow-lg">
                    <h2 class="text-xl font-semibold mb-4">Services</h2>
                    <div class="space-y-4">
                        <div class="flex justify-between items-center">
                            <span class="text-slate-400">Redis Server</span>
                            <span id="status-redis" class="text-sm">Checking...</span>
                        </div>
                        <div class="flex justify-between items-center">
                            <span class="text-slate-400">Webhook Receiver</span>
                            <span id="status-webhook" class="text-sm">Checking...</span>
                        </div>
                        <div class="flex justify-between items-center">
                            <span class="text-slate-400">Ad Stitching Server</span>
                            <span id="status-adserver" class="text-sm">Checking...</span>
                        </div>
                        <div class="flex justify-between items-center">
                            <span class="text-slate-400">GPU Load (AMD)</span>
                            <span id="status-gpu" class="text-sm">Checking...</span>
                        </div>
                    </div>
                </div>
                <div class="card p-6 rounded-xl shadow-lg">
                    <h2 class="text-xl font-semibold mb-4">Workers</h2>
                    <div id="workers-panel" class="space-y-2 text-sm">
                        <div id="worker-local-line" class="text-slate-300">🟢 Local Worker (RX 560) — Queue: 0 jobs</div>
                        <div id="worker-win-line" class="text-slate-300">🔴 Windows Worker — OFFLINE (fallback to local)</div>
                        <div id="worker-win-meta" class="text-slate-400 text-xs">Last seen: —</div>
                    </div>
                </div>

                <div class="card p-6 rounded-xl shadow-lg">
                    <h2 class="text-xl font-semibold mb-4">Storage</h2>
                    <div class="space-y-4">
                        <div>
                            <p class="text-xs text-slate-400 uppercase font-bold mb-1">VOD Storage (/srv/vod)</p>
                            <div class="w-full bg-slate-700 rounded-full h-2">
                                <div id="disk-vod-bar" class="bg-indigo-500 h-2 rounded-full" style="width: 0%"></div>
                            </div>
                            <p id="disk-vod-text" class="text-xs text-slate-400 mt-1">0% used</p>
                        </div>
                    </div>
                </div>
            </div>
        </div>
    </div>

    <!-- Preview Modal -->
    <div id="preview-modal" class="fixed inset-0 bg-black/80 flex items-center justify-center hidden z-50 p-4">
        <div class="card w-full max-w-4xl rounded-xl overflow-hidden">
            <div class="p-4 border-b border-slate-700 flex justify-between items-center">
                <h3 id="preview-title" class="font-bold">Ad Preview</h3>
                <button onclick="closePreview()" class="text-slate-400 hover:text-white">✕</button>
            </div>
            <div class="aspect-video bg-black">
                <video id="video-player" controls class="w-full h-full"></video>
@@ -437,139 +472,164 @@ HTML_TEMPLATE = """
        }

        function sortTable(tbodyId, colIndex) {
            const tbody = document.getElementById(tbodyId);
            const rows = Array.from(tbody.rows);
            const key = tbodyId + colIndex;
            sortDirections[key] = !sortDirections[key];
            
            rows.sort((a, b) => {
                const valA = a.cells[colIndex].innerText.toLowerCase();
                const valB = b.cells[colIndex].innerText.toLowerCase();
                return sortDirections[key] ? valA.localeCompare(valB) : valB.localeCompare(valA);
            });
            
            rows.forEach(row => tbody.appendChild(row));
        }

        function updateDashboard() {
            fetch('api/status').then(r => r.json()).then(data => {
                // Active Job
                const activeSection = document.getElementById('active-job-section');
                if (data.active_job) {
                    activeSection.classList.remove('hidden');
                    document.getElementById('active-filename').innerText = data.active_job.input_path.split('/').pop();
                    document.getElementById('active-type').innerText = data.active_job.type.toUpperCase();
                    const activeWorker = data.active_job.worker === 'windows' ? '🪟 Windows' : (data.active_job.worker === 'local' ? '🖥 Local' : '—');
                    const gpuNote = data.active_job.worker === 'windows' ? ' (GPU 0: 1080p+720p / GPU 1: 480p+360p)' : '';
                    document.getElementById('active-worker').innerText = `Worker: ${activeWorker}${gpuNote}`;
                    document.getElementById('active-progress').style.width = data.active_job.progress + '%';
                    document.getElementById('active-percent').innerText = data.active_job.progress + '%';
                    document.getElementById('active-status').innerText = data.active_job.status;
                    document.getElementById('active-logs').innerText = data.active_job.last_logs || '';
                } else {
                    activeSection.classList.add('hidden');
                }

                // Queue
                const queueBody = document.getElementById('queue-body');
                queueBody.innerHTML = data.queue.map((job, i) => `
                    <tr class="border-b border-slate-800 text-sm hover:bg-slate-800/50">
                        <td class="py-3 px-1">${i+1}</td>
                        <td class="py-3 truncate max-w-xs">${job.input_path.split('/').pop()}</td>
                        <td class="py-3">${job.type}</td>
                        <td class="py-3 text-slate-400">${new Date(job.queued_at).toLocaleTimeString()}</td>
                        <td class="py-3">${job.worker === 'windows' ? '🪟 Windows' : (job.worker === 'local' ? '🖥 Local' : '—')}</td>
                        <td class="py-3"><button onclick="removeJob('${job.job_id}')" class="text-red-400 hover:underline">Remove</button></td>
                    </tr>
                `).join('');

                // System
                document.getElementById('status-redis').innerText = data.system.redis ? 'Connected' : 'Disconnected';
                document.getElementById('status-redis').className = data.system.redis ? 'text-sm text-green-400' : 'text-sm text-red-400';
                document.getElementById('status-gpu').innerText = data.system.gpu + '%';
                document.getElementById('status-webhook').innerText = data.system.webhook ? 'Online' : 'Offline';
                document.getElementById('status-webhook').className = data.system.webhook ? 'text-sm text-green-400' : 'text-sm text-red-400';
                document.getElementById('status-adserver').innerText = data.system.adserver ? 'Online' : 'Offline';
                document.getElementById('status-adserver').className = data.system.adserver ? 'text-sm text-green-400' : 'text-sm text-red-400';
                document.getElementById('disk-vod-bar').style.width = data.system.disk_vod + '%';
                document.getElementById('disk-vod-text').innerText = data.system.disk_vod + '% used';
            });

                fetch('api/workers/status').then(r => r.json()).then(ws => {
                    document.getElementById('worker-local-line').innerText = `${ws.local_worker.status === 'online' ? '🟢' : '🔴'} Local Worker (RX 560) — Queue: ${ws.local_worker.queue_depth} jobs`;
                    if (ws.windows_worker.status === 'online') {
                        document.getElementById('worker-win-line').innerText = `🟢 Windows Worker — Queue: ${ws.windows_worker.queue_depth} job(s)`;
                        document.getElementById('worker-win-meta').innerText = `${ws.windows_worker.gpu_model || 'GTX 1050 Ti'} x${ws.windows_worker.gpus || 2}  ${ws.windows_worker.hostname || ''}  Heartbeat: ${ws.windows_worker.heartbeat_ttl}s ago`;
                    } else {
                        document.getElementById('worker-win-line').innerText = '🔴 Windows Worker — OFFLINE (fallback to local)';
                        document.getElementById('worker-win-meta').innerText = `Last seen: ${ws.router.last_updated || '—'}`;
                    }
                });

                fetch('api/stats/workers').then(r => r.json()).then(stats => {
                    document.getElementById('worker-stat-total').innerText = stats.total_jobs;
                    document.getElementById('worker-stat-completed').innerText = stats.completed;
                    document.getElementById('worker-stat-failed').innerText = stats.failed;
                    document.getElementById('worker-stat-local').innerText = stats.by_worker.local.total;
                    document.getElementById('worker-stat-windows').innerText = stats.by_worker.windows.total;
                });


            if (currentTab === 'completed') {
                loadHistory();
            }

            if (currentTab === 'ads') {
                loadAds();
            }
        }

        function loadHistory() {
            const status = document.getElementById('hist-filter-status').value;
            const type = document.getElementById('hist-filter-type').value;
            const search = document.getElementById('hist-filter-search').value;
            const from = document.getElementById('hist-filter-from').value;
            const to = document.getElementById('hist-filter-to').value;

            const params = new URLSearchParams({
                status: status,
                type: type,
                search: search,
                date_from: from,
                date_to: to,
                page: historyPage,
                sort: historySort
            });

            fetch('api/jobs/history?' + params.toString()).then(r => r.json()).then(data => {
                // Update Stats
                document.getElementById('hist-stat-total').innerText = data.counts.total;
                document.getElementById('hist-stat-completed').innerText = data.counts.completed;
                document.getElementById('hist-stat-failed').innerText = data.counts.failed;

                // Update Table
                const body = document.getElementById('history-body');
                body.innerHTML = data.jobs.map(job => {
                    const filename = job.input_path.split('/').pop();
                    const typeIcon = job.type === 'movie' ? '🎬' : (job.type === 'tv' ? '📺' : '📢');
                    const isExpanded = expandedRows.has(job.job_id);
                    
                    return `
                        <tr class="border-b border-slate-800 text-sm hover:bg-slate-800/50">
                            <td class="py-3 px-1 text-lg" title="${job.type}">${typeIcon}</td>
                            <td class="py-3 truncate max-w-md" title="${job.input_path}">${filename}</td>
                            <td class="py-3">
                                <span class="status-badge status-${job.status}">${job.status}</span>
                            </td>
                            <td class="py-3 text-slate-400" title="${new Date(job.queued_at).toLocaleString()}">${formatRelativeTime(job.queued_at)}</td>
                            <td class="py-3">${job.worker === 'windows' ? '🪟 Windows' : (job.worker === 'local' ? '🖥 Local' : '—')}</td>
                            <td class="py-3 text-slate-400">${formatDuration(job.started_at, job.completed_at || job.end_time)}</td>
                            <td class="py-3 space-x-2">
                                <button onclick="toggleRow('${job.job_id}')" class="text-indigo-400 hover:underline">${isExpanded ? 'Collapse' : 'Details'}</button>
                                ${job.status === 'failed' ? `<button onclick="requeueJob('${job.job_id}')" class="text-yellow-400 hover:underline">↺ Requeue</button>` : ''}
                                ${job.status === 'completed' ? `<button onclick="viewOutput('${job.job_id}', '${job.type}')" class="text-green-400 hover:underline">View</button>` : ''}
                            </td>
                        </tr>
                        ${isExpanded ? `
                        <tr class="bg-slate-900/50">
                            <td colspan="7" class="p-4 text-xs space-y-2 border-b border-slate-700">
                                <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
                                    <div>
                                        <p class="text-slate-500 font-bold uppercase">Input Path</p>
                                        <p class="font-mono break-all">${job.input_path}</p>
                                    </div>
                                    <div>
                                        <p class="text-slate-500 font-bold uppercase">Output Path</p>
                                        <p class="font-mono break-all">${job.output_path || 'N/A'}</p>
                                    </div>
                                    <div>
                                        <p class="text-slate-500 font-bold uppercase">Timestamps</p>
                                        <p>Started: ${job.started_at ? new Date(job.started_at).toLocaleString() : 'N/A'}</p>
                                        <p>Finished: ${job.completed_at || job.end_time ? new Date(job.completed_at || job.end_time).toLocaleString() : 'N/A'}</p>
                                        ${job.requeue_count ? `<p class="text-yellow-500">Requeued ${job.requeue_count} times — last attempt: ${new Date(job.requeued_at).toLocaleString()}</p>` : ''}
                                    </div>
                                    ${job.error ? `
                                    <div>
                                        <p class="text-red-500 font-bold uppercase">Error Message</p>
                                        <pre class="bg-black/50 p-2 rounded mt-1 whitespace-pre-wrap text-red-400">${job.error}</pre>
                                    </div>` : ''}
                                </div>
                                <div class="mt-4">
                                    <button onclick="toggleLogs('${job.job_id}')" class="text-slate-400 hover:text-white flex items-center">
                                        <span class="mr-1">▶</span> Full FFmpeg Log
                                    </button>
@@ -978,51 +1038,51 @@ HTML_TEMPLATE = """
                .finally(() => {
                    btn.innerText = originalText;
                    btn.disabled = false;
                });
        }

        setInterval(updateDashboard, 5000);
        updateDashboard();
    </script>
</body>
</html>
"""

@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE)

@app.route('/api/status')
def status():
    try:
        # Active Job
        active_job_raw = r.get(config.ACTIVE_JOB_KEY)
        active_job = json.loads(active_job_raw) if active_job_raw else None
        
        # Queue
        queue_raw = r.lrange(config.TRANSCODE_QUEUE, 0, -1)
        queue = [json.loads(item) for item in queue_raw]
        
        # System Status
        gpu_busy = 0
        try:
            for i in range(5):
                path = f"/sys/class/drm/card{i}/device/gpu_busy_percent"
                if os.path.exists(path):
                    with open(path, 'r') as f:
                        gpu_busy = int(f.read().strip())
                    break
        except: pass

        disk_vod = 0
        if os.path.exists('/srv/vod'):
            try:
                disk_vod = psutil.disk_usage('/srv/vod').percent
            except: pass
        
        webhook_online = False
        try:
            import socket
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(0.5)
            if s.connect_ex(('127.0.0.1', config.WEBHOOK_PORT)) == 0:
@@ -1033,50 +1093,122 @@ def status():
        adserver_online = False
        try:
            import socket
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(0.5)
            if s.connect_ex(('127.0.0.1', 8082)) == 0:
                adserver_online = True
            s.close()
        except: pass

        return jsonify({
            "active_job": active_job,
            "queue": queue,
            "system": {
                "redis": True,
                "gpu": gpu_busy,
                "webhook": webhook_online,
                "adserver": adserver_online,
                "disk_vod": disk_vod
            }
        })
    except Exception as e:
        logger.error(f"Error in status API: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/api/workers/status')
def workers_status():
    try:
        router = r.hgetall(config.ROUTER_STATUS_KEY)
        windows_heartbeat_raw = r.get(config.WIN_HEARTBEAT_KEY)
        windows_ttl = r.ttl(config.WIN_HEARTBEAT_KEY)
        active_job_raw = r.get(config.ACTIVE_JOB_KEY)
        active_job = json.loads(active_job_raw) if active_job_raw else {}

        return jsonify({
            "local_worker": {
                "status": "online",
                "queue_depth": r.llen(config.LOCAL_QUEUE),
                "current_job": active_job.get('job_id') if active_job.get('worker') == 'local' else None
            },
            "windows_worker": {
                "status": "online" if windows_heartbeat_raw else "offline",
                "heartbeat_ttl": max(windows_ttl, 0) if windows_ttl is not None else 0,
                "hostname": router.get('hostname') or None,
                "ip": router.get('ip') or None,
                "gpu_model": router.get('gpu_model') or 'GTX 1050 Ti',
                "gpus": int(router.get('gpus', 2) or 2),
                "queue_depth": int(router.get('windows_queue_depth', 0) or 0)
            },
            "router": {
                "running": bool(router),
                "last_updated": router.get('updated_at')
            }
        })
    except Exception as e:
        logger.error(f"Error in workers status API: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/stats/workers')
def stats_workers():
    try:
        history_keys = r.keys(f"{config.HISTORY_PREFIX}*")
        by_worker = {
            "local": {"total": 0, "completed": 0, "failed": 0},
            "windows": {"total": 0, "completed": 0, "failed": 0},
            "unknown": {"total": 0, "completed": 0, "failed": 0}
        }
        total_jobs = completed = failed = 0

        for key in history_keys:
            job = r.hgetall(key)
            if not job:
                continue
            total_jobs += 1
            status = job.get('status')
            worker = job.get('worker', 'unknown')
            if worker not in by_worker:
                worker = 'unknown'
            by_worker[worker]['total'] += 1
            if status == 'completed':
                completed += 1
                by_worker[worker]['completed'] += 1
            elif status == 'failed':
                failed += 1
                by_worker[worker]['failed'] += 1

        return jsonify({
            "total_jobs": total_jobs,
            "completed": completed,
            "failed": failed,
            "by_worker": by_worker
        })
    except Exception as e:
        logger.error(f"Error in worker stats API: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/jobs/history')
def get_history():
    status_filter = request.args.get('status', 'all')
    type_filter = request.args.get('type', 'all')
    search = request.args.get('search', '').lower()
    date_from = request.args.get('date_from')
    date_to = request.args.get('date_to')
    page = int(request.args.get('page', 1))
    per_page = int(request.args.get('per_page', 20))
    sort = request.args.get('sort', 'newest')

    history_keys = r.keys(f"{config.HISTORY_PREFIX}*")
    all_jobs = []
    counts = {"total": 0, "completed": 0, "failed": 0}

    for key in history_keys:
        job = r.hgetall(key)
        if not job: continue
        
        status = job.get('status')
        if status == 'completed': counts['completed'] += 1
        elif status == 'failed': counts['failed'] += 1
        counts['total'] += 1

        # Apply Filters
@@ -1129,56 +1261,56 @@ def requeue_job_api(job_id):
    job = r.hgetall(history_key)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    
    input_path = job.get('input_path')
    if not input_path or not os.path.exists(input_path):
        return jsonify({"error": "Input file no longer exists"}), 400

    # Update history
    requeue_count = int(job.get('requeue_count', 0)) + 1
    r.hset(history_key, mapping={
        "status": "queued",
        "requeue_count": requeue_count,
        "requeued_at": datetime.now().isoformat(),
        "error": "" # Clear old error
    })

    # Add back to queue
    job_payload = {
        "job_id": job_id,
        "type": job.get('type'),
        "input_path": input_path,
        "status": "queued",
        "queued_at": datetime.now().isoformat()
    }
    r.rpush(config.TRANSCODE_QUEUE, json.dumps(job_payload))
    
    return jsonify({
        "status": "queued",
        "job_id": job_id,
        "queue_position": r.llen(config.TRANSCODE_QUEUE)
    })

@app.route('/api/advertisers')
def get_advertisers():
    adv_names = r_ads.smembers(config.ADVERTISER_INDEX_KEY)
    advertisers = []
    for name in adv_names:
        ad_ids = r_ads.smembers(f"{config.ADVERTISER_ADS_PREFIX}{name}")
        advertisers.append({
            "name": name,
            "ad_count": len(ad_ids)
        })
    return jsonify({"advertisers": sorted(advertisers, key=lambda x: x['name'])})

@app.route('/api/ad/<ad_id>/play', methods=['POST'])
def record_ad_play(ad_id):
    # Increment counter
    plays = r_ads.incr(f"{config.AD_PLAYS_PREFIX}{ad_id}")
    
    # Check limit
    meta = r_ads.hgetall(f"{config.AD_META_PREFIX}{ad_id}")
    if meta and meta.get('play_limit_enabled') == 'true':
        max_plays = int(meta.get('max_plays', 0))
        if max_plays > 0 and plays >= max_plays:
            # Mark as exhausted
@@ -1276,51 +1408,51 @@ def delete_ad(ad_id):
    vod_dir = os.path.join(config.OUTPUT_BASE_ADS, ad_id)
    
    if os.path.exists(archive_dir): shutil.rmtree(archive_dir)
    if os.path.exists(vod_dir): shutil.rmtree(vod_dir)
    
    return jsonify({"status": "ok"})

@app.route('/api/ad/<ad_id>/retry', methods=['POST'])
def retry_ad(ad_id):
    meta_path = os.path.join(config.ARCHIVE_BASE_ADS, ad_id, f"{ad_id}.json")
    if os.path.exists(meta_path):
        with open(meta_path, 'r') as f:
            ad_data = json.load(f)
        
        ad_data['status'] = 'queued'
        with open(meta_path, 'w') as f:
            json.dump(ad_data, f)
            
        job_payload = {
            "job_id": ad_id,
            "type": "ad",
            "input_path": ad_data['input_path'],
            "status": "queued",
            "queued_at": datetime.now().isoformat()
        }
        r.rpush(config.TRANSCODE_QUEUE, json.dumps(job_payload))
        return jsonify({"status": "ok"})
    return jsonify({"error": "Not found"}), 404

@app.route('/upload/ad', methods=['POST'])
def upload_ad():
    if 'file' not in request.files:
        return "No file part", 400
    file = request.files['file']
    description = request.form.get('description', 'No description')
    advertiser = request.form.get('advertiser', 'Unknown')
    campaign = request.form.get('campaign', '')
    
    # Play limit settings
    play_limit_type = request.form.get('play_limit_type', 'unlimited')
    max_plays = request.form.get('max_plays', 0)
    auto_disable = request.form.get('auto_disable', 'true')
    
    if file.filename == '':
        return "No selected file", 400
        
    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in config.VIDEO_EXTENSIONS:
        return "Invalid file type", 400

    # Get next ID
@@ -1348,72 +1480,72 @@ def upload_ad():
        "description": description,
        "advertiser_name": advertiser,
        "campaign_name": campaign,
        "original_filename": file.filename,
        "upload_time": upload_time,
        "status": "queued",
        "input_path": input_path,
        "output_path": os.path.join(config.OUTPUT_BASE_ADS, ad_id),
        "play_limit_enabled": "true" if play_limit_type == 'custom' else "false",
        "max_plays": max_plays if play_limit_type == 'custom' else 0,
        "auto_disable": auto_disable,
        "exhausted": "false"
    }
    
    with open(os.path.join(archive_dir, f"{ad_id}.json"), 'w') as f:
        json.dump(ad_data, f)
        
    # Queue job
    job_payload = {
        "job_id": ad_id,
        "type": "ad",
        "input_path": input_path,
        "status": "queued",
        "queued_at": upload_time
    }
    r.rpush(config.TRANSCODE_QUEUE, json.dumps(job_payload))
    r_ads.zadd(config.AD_REGISTRY_KEY, {ad_id: time.time()})
    
    # Update Redis Meta
    r_ads.hset(f"{config.AD_META_PREFIX}{ad_id}", mapping=ad_data)
    
    # Update Advertiser Index
    r_ads.sadd(config.ADVERTISER_INDEX_KEY, advertiser)
    r_ads.sadd(f"{config.ADVERTISER_ADS_PREFIX}{advertiser}", ad_id)
    
    # Initialize play counter
    r_ads.setnx(f"{config.AD_PLAYS_PREFIX}{ad_id}", 0)
    
    return jsonify({"ad_id": ad_id, "status": "queued"})

@app.route('/api/remove/<job_id>', methods=['POST'])
def remove_job(job_id):
    queue_items = r.lrange(config.TRANSCODE_QUEUE, 0, -1)
    for item in queue_items:
        data = json.loads(item)
        if data.get('job_id') == job_id:
            r.lrem(config.TRANSCODE_QUEUE, 1, item)
            break
    r.hset(f"{config.HISTORY_PREFIX}{job_id}", "status", "removed")
    return jsonify({"status": "ok"})

@app.route('/api/scan', methods=['POST'])
def trigger_scan():
    try:
        # Run the scanner script in the background
        subprocess.Popen([os.path.join(os.path.dirname(__file__), "venv/bin/python"), 
                         os.path.join(os.path.dirname(__file__), "folder_scanner.py"), "--once"])
        return jsonify({"status": "scan_triggered"})
    except Exception as e:
        logger.error(f"Failed to trigger scan: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/retry_failed', methods=['POST'])
def retry_failed():
    try:
        import auto_requeue
        count = auto_requeue.requeue_failed_jobs()
        return jsonify({"status": "ok", "count": count})
    except Exception as e:
        logger.error(f"Failed to retry failed jobs: {e}")
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    logger.info(f"Starting Web UI on port {config.WEBUI_PORT}")
    app.run(host='0.0.0.0', port=config.WEBUI_PORT)
