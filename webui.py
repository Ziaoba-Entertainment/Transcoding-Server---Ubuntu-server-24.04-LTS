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

r = redis.Redis(host=config.REDIS_HOST, port=config.REDIS_PORT, db=config.REDIS_DB, decode_responses=True)
r_ads = redis.Redis(host=config.REDIS_HOST, port=config.REDIS_PORT, db=config.REDIS_DB_ADS, decode_responses=True)

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
                    <p class="text-sm text-slate-400 mb-4" id="active-type"></p>
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
            </div>

            <!-- Filter Bar -->
            <div class="card p-4 rounded-xl shadow-lg flex flex-wrap gap-4 items-end">
                <div class="flex-1 min-w-[200px]">
                    <label class="block text-xs text-slate-400 uppercase font-bold mb-1">Search Filename</label>
                    <input type="text" id="hist-filter-search" oninput="debounceHistorySearch()" class="w-full bg-slate-800 border border-slate-700 rounded px-3 py-2 text-sm focus:outline-none focus:border-indigo-500" placeholder="🔍 Search...">
                </div>
                <div>
                    <label class="block text-xs text-slate-400 uppercase font-bold mb-1">Status</label>
                    <select id="hist-filter-status" onchange="loadHistory()" class="bg-slate-800 border border-slate-700 rounded px-3 py-2 text-sm focus:outline-none focus:border-indigo-500">
                        <option value="all">All Statuses</option>
                        <option value="completed">✅ Completed</option>
                        <option value="failed">❌ Failed</option>
                    </select>
                </div>
                <div>
                    <label class="block text-xs text-slate-400 uppercase font-bold mb-1">Type</label>
                    <select id="hist-filter-type" onchange="loadHistory()" class="bg-slate-800 border border-slate-700 rounded px-3 py-2 text-sm focus:outline-none focus:border-indigo-500">
                        <option value="all">All Types</option>
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
                </div>
                <div onclick="filterAdsByStatus('active')" class="card p-4 rounded-xl cursor-pointer hover:bg-slate-800 transition-colors">
                    <p class="text-[10px] text-green-400 uppercase font-bold">✅ Active</p>
                    <p id="ad-stat-active" class="text-xl font-bold text-green-400">0</p>
                </div>
                <div onclick="filterAdsByStatus('exhausted')" class="card p-4 rounded-xl cursor-pointer hover:bg-slate-800 transition-colors">
                    <p class="text-[10px] text-orange-400 uppercase font-bold">🚫 Exhausted</p>
                    <p id="ad-stat-exhausted" class="text-xl font-bold text-orange-400">0</p>
                </div>
                <div onclick="filterAdsByStatus('processing')" class="card p-4 rounded-xl cursor-pointer hover:bg-slate-800 transition-colors">
                    <p class="text-[10px] text-blue-400 uppercase font-bold">⏳ Processing</p>
                    <p id="ad-stat-processing" class="text-xl font-bold text-blue-400">0</p>
                </div>
                <div class="card p-4 rounded-xl">
                    <p class="text-[10px] text-indigo-400 uppercase font-bold">Plays Today</p>
                    <p id="ad-stat-plays" class="text-xl font-bold text-indigo-400">0</p>
                </div>
            </div>

            <!-- Upload Panel -->
            <div class="card p-6 rounded-xl shadow-lg">
                <h2 class="text-xl font-semibold mb-4">Upload New Advertisement</h2>
                <form id="ad-upload-form" class="space-y-4">
                    <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
                        <div>
                            <label class="block text-sm text-slate-400 mb-1">Ad Description*</label>
                            <input type="text" id="ad-description" required class="w-full bg-slate-800 border border-slate-700 rounded p-2 text-white focus:outline-none focus:border-indigo-500" placeholder="e.g. Summer Sale 30s spot">
                        </div>
                        <div>
                            <label class="block text-sm text-slate-400 mb-1">Advertiser Name*</label>
                            <input type="text" id="ad-advertiser" list="advertiser-suggestions" required class="w-full bg-slate-800 border border-slate-700 rounded p-2 text-white focus:outline-none focus:border-indigo-500" placeholder="e.g. Coca Cola">
                            <datalist id="advertiser-suggestions"></datalist>
                        </div>
                    </div>
                    <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
                        <div>
                            <label class="block text-sm text-slate-400 mb-1">Campaign Name (Optional)</label>
                            <input type="text" id="ad-campaign" class="w-full bg-slate-800 border border-slate-700 rounded p-2 text-white focus:outline-none focus:border-indigo-500" placeholder="e.g. Summer 2024">
                        </div>
                        <div>
                            <label class="block text-sm text-slate-400 mb-1">Max Plays</label>
                            <div class="flex space-x-2">
                                <select id="ad-play-limit-type" onchange="togglePlayLimitInput()" class="bg-slate-800 border border-slate-700 rounded p-2 text-white focus:outline-none focus:border-indigo-500">
                                    <option value="unlimited">Unlimited</option>
                                    <option value="custom">Custom</option>
                                </select>
                                <input type="number" id="ad-max-plays" class="hidden w-full bg-slate-800 border border-slate-700 rounded p-2 text-white focus:outline-none focus:border-indigo-500" placeholder="500" min="1" max="10000000">
                            </div>
                        </div>
                    </div>
                    <div id="play-limit-options" class="hidden space-y-2">
                        <label class="flex items-center space-x-2 text-xs text-slate-400">
                            <input type="checkbox" id="ad-auto-disable" checked class="rounded border-slate-700 bg-slate-800 text-indigo-500 focus:ring-indigo-500">
                            <span>Disable ad automatically when limit reached</span>
                        </label>
                        <label class="flex items-center space-x-2 text-xs text-slate-400">
                            <input type="checkbox" id="ad-notify-limit" checked class="rounded border-slate-700 bg-slate-800 text-indigo-500 focus:ring-indigo-500">
                            <span>Notify when 80% of plays used</span>
                        </label>
                    </div>
                    <div class="flex items-center space-x-4">
                        <input type="file" id="ad-file" accept=".mp4,.mkv,.avi,.mov,.m4v,.ts" class="hidden" onchange="updateFileName()">
                        <button type="button" onclick="document.getElementById('ad-file').click()" class="bg-slate-700 hover:bg-slate-600 px-4 py-2 rounded text-sm font-medium">Choose File</button>
                        <span id="selected-file-name" class="text-sm text-slate-400 italic">No file chosen</span>
                    </div>
                    <div id="upload-progress-container" class="hidden">
                        <div class="w-full bg-slate-700 rounded-full h-2">
                            <div id="upload-progress-bar" class="bg-indigo-500 h-2 rounded-full" style="width: 0%"></div>
                        </div>
                        <p id="upload-status" class="text-[10px] text-slate-400 mt-1">Uploading...</p>
                    </div>
                    <button type="submit" id="ad-upload-btn" class="bg-indigo-600 hover:bg-indigo-500 px-6 py-2 rounded text-sm font-bold transition-colors">Upload & Queue</button>
                </form>
            </div>

            <!-- Ads Filter Bar -->
            <div class="card p-4 rounded-xl shadow-lg flex flex-wrap gap-4 items-end">
                <div class="flex-1 min-w-[200px]">
                    <label class="block text-xs text-slate-400 uppercase font-bold mb-1">Search Ads</label>
                    <input type="text" id="ad-filter-search" oninput="updateDashboard()" class="w-full bg-slate-800 border border-slate-700 rounded px-3 py-2 text-sm focus:outline-none focus:border-indigo-500" placeholder="🔍 Search description, advertiser...">
                </div>
                <div>
                    <label class="block text-xs text-slate-400 uppercase font-bold mb-1">Advertiser</label>
                    <select id="ad-filter-advertiser" onchange="updateDashboard()" class="bg-slate-800 border border-slate-700 rounded px-3 py-2 text-sm focus:outline-none focus:border-indigo-500">
                        <option value="all">All Advertisers</option>
                    </select>
                </div>
                <div>
                    <label class="block text-xs text-slate-400 uppercase font-bold mb-1">Status</label>
                    <select id="ad-filter-status" onchange="updateDashboard()" class="bg-slate-800 border border-slate-700 rounded px-3 py-2 text-sm focus:outline-none focus:border-indigo-500">
                        <option value="all">All Statuses</option>
                        <option value="active">✅ Active</option>
                        <option value="processing">⏳ Processing</option>
                        <option value="exhausted">🚫 Exhausted</option>
                        <option value="failed">❌ Failed</option>
                    </select>
                </div>
                <button onclick="clearAdFilters()" class="bg-slate-700 hover:bg-slate-600 px-4 py-2 rounded text-sm font-bold transition-colors">Clear</button>
            </div>

            <!-- Ads List -->
            <div class="card p-6 rounded-xl shadow-lg">
                <h2 class="text-xl font-semibold mb-4">Managed Advertisements</h2>
                <div class="overflow-x-auto">
                    <table class="w-full text-left">
                        <thead>
                            <tr class="border-b border-slate-700 text-slate-400 text-sm">
                                <th class="pb-2">ID</th>
                                <th class="pb-2">Description</th>
                                <th class="pb-2">Advertiser</th>
                                <th class="pb-2">Campaign</th>
                                <th class="pb-2">Plays</th>
                                <th class="pb-2">Limit</th>
                                <th class="pb-2">Status</th>
                                <th class="pb-2">Uploaded</th>
                                <th class="pb-2">Actions</th>
                            </tr>
                        </thead>
                        <tbody id="ads-body"></tbody>
                    </table>
                </div>
            </div>
        </div>

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
            </div>
            <div id="preview-meta" class="p-4 text-xs text-slate-400 grid grid-cols-2 gap-2"></div>
        </div>
    </div>

    <!-- Edit Limit Modal -->
    <div id="limit-modal" class="fixed inset-0 bg-black/80 flex items-center justify-center hidden z-50 p-4">
        <div class="card w-full max-w-md rounded-xl overflow-hidden">
            <div class="p-4 border-b border-slate-700 flex justify-between items-center">
                <h3 class="font-bold">Edit Play Limit</h3>
                <button onclick="closeLimitModal()" class="text-slate-400 hover:text-white">✕</button>
            </div>
            <div class="p-6 space-y-4">
                <div id="limit-modal-info" class="text-sm text-slate-400 mb-4"></div>
                <label class="flex items-center space-x-2 text-sm">
                    <input type="checkbox" id="modal-limit-enabled" onchange="toggleModalLimitInput()" class="rounded border-slate-700 bg-slate-800 text-indigo-500 focus:ring-indigo-500">
                    <span>Enable play limit</span>
                </label>
                <div id="modal-limit-input-container" class="hidden">
                    <label class="block text-xs text-slate-400 uppercase font-bold mb-1">Max Plays</label>
                    <input type="number" id="modal-max-plays" class="w-full bg-slate-800 border border-slate-700 rounded p-2 text-white focus:outline-none focus:border-indigo-500" min="1">
                </div>
                <label class="flex items-center space-x-2 text-sm">
                    <input type="checkbox" id="modal-auto-disable" class="rounded border-slate-700 bg-slate-800 text-indigo-500 focus:ring-indigo-500">
                    <span>Auto-disable when limit reached</span>
                </label>
                <div class="flex justify-end space-x-3 pt-4">
                    <button onclick="closeLimitModal()" class="px-4 py-2 text-sm text-slate-400 hover:text-white">Cancel</button>
                    <button id="save-limit-btn" class="bg-indigo-600 hover:bg-indigo-500 px-4 py-2 rounded text-sm font-bold transition-colors">Save Changes</button>
                </div>
            </div>
        </div>
    </div>

    <!-- Toast Container -->
    <div id="toast-container" class="fixed bottom-4 right-4 z-50 space-y-2"></div>

    <script>
        let currentTab = 'queue';
        let sortDirections = {};
        let historyPage = 1;
        let historySort = 'newest';
        let historySearchTimeout = null;
        let expandedRows = new Set();

        function showTab(tabId) {
            document.querySelectorAll('.tab-content').forEach(c => c.classList.add('hidden'));
            document.getElementById('content-' + tabId).classList.remove('hidden');
            
            document.querySelectorAll('header button').forEach(b => {
                b.classList.remove('tab-active');
                b.classList.add('text-slate-400');
            });
            document.getElementById('tab-' + tabId).classList.add('tab-active');
            document.getElementById('tab-' + tabId).classList.remove('text-slate-400');
            currentTab = tabId;
        }

        function updateFileName() {
            const file = document.getElementById('ad-file').files[0];
            document.getElementById('selected-file-name').innerText = file ? file.name : 'No file chosen';
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
                            <td class="py-3 text-slate-400">${formatDuration(job.started_at, job.completed_at || job.end_time)}</td>
                            <td class="py-3 space-x-2">
                                <button onclick="toggleRow('${job.job_id}')" class="text-indigo-400 hover:underline">${isExpanded ? 'Collapse' : 'Details'}</button>
                                ${job.status === 'failed' ? `<button onclick="requeueJob('${job.job_id}')" class="text-yellow-400 hover:underline">↺ Requeue</button>` : ''}
                                ${job.status === 'completed' ? `<button onclick="viewOutput('${job.job_id}', '${job.type}')" class="text-green-400 hover:underline">View</button>` : ''}
                            </td>
                        </tr>
                        ${isExpanded ? `
                        <tr class="bg-slate-900/50">
                            <td colspan="6" class="p-4 text-xs space-y-2 border-b border-slate-700">
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
                                    <pre id="logs-${job.job_id}" class="hidden mt-2 bg-black p-4 rounded font-mono text-[10px] text-green-400 overflow-x-auto max-h-64">${job.last_logs || 'No logs available'}</pre>
                                </div>
                            </td>
                        </tr>` : ''}
                    `;
                }).join('');

                // Update Pagination
                const start = (data.pagination.page - 1) * data.pagination.per_page + 1;
                const end = Math.min(data.pagination.page * data.pagination.per_page, data.pagination.total);
                document.getElementById('hist-pagination-info').innerText = `Showing ${data.pagination.total > 0 ? start : 0} to ${end} of ${data.pagination.total} jobs`;
                document.getElementById('hist-prev-page').disabled = data.pagination.page <= 1;
                document.getElementById('hist-next-page').disabled = data.pagination.page >= data.pagination.total_pages;
            });
        }

        function loadAds() {
            fetch('api/ads').then(r => r.json()).then(ads => {
                // Update Stats
                const now = new Date();
                const today = now.toISOString().split('T')[0];
                
                let total = ads.length;
                let active = 0;
                let exhausted = 0;
                let processing = 0;
                let playsToday = 0;

                ads.forEach(ad => {
                    if (ad.status === 'completed') {
                        if (ad.play_limit && ad.play_limit.exhausted) exhausted++;
                        else active++;
                    } else if (['queued', 'processing', 'verifying', 'archiving'].includes(ad.status)) {
                        processing++;
                    }
                    // playsToday calculation would need a separate API or more data
                });

                document.getElementById('ad-stat-total').innerText = total;
                document.getElementById('ad-stat-active').innerText = active;
                document.getElementById('ad-stat-exhausted').innerText = exhausted;
                document.getElementById('ad-stat-processing').innerText = processing;
                
                // Fetch advertisers for autocomplete and filter
                fetch('api/advertisers').then(r => r.json()).then(data => {
                    const datalist = document.getElementById('advertiser-suggestions');
                    datalist.innerHTML = data.advertisers.map(adv => `<option value="${adv.name}">`).join('');
                    
                    const filter = document.getElementById('ad-filter-advertiser');
                    const currentVal = filter.value;
                    filter.innerHTML = '<option value="all">All Advertisers</option>' + 
                        data.advertisers.map(adv => `<option value="${adv.name}" ${adv.name === currentVal ? 'selected' : ''}>${adv.name}</option>`).join('');
                });

                // Filter and Render Table
                const search = document.getElementById('ad-filter-search').value.toLowerCase();
                const advFilter = document.getElementById('ad-filter-advertiser').value;
                const statusFilter = document.getElementById('ad-filter-status').value;

                const filtered = ads.filter(ad => {
                    const matchesSearch = !search || ad.description.toLowerCase().includes(search) || 
                                         (ad.advertiser_name && ad.advertiser_name.toLowerCase().includes(search)) ||
                                         ad.ad_id.toLowerCase().includes(search);
                    const matchesAdv = advFilter === 'all' || ad.advertiser_name === advFilter;
                    const matchesStatus = statusFilter === 'all' || 
                                         (statusFilter === 'active' && ad.status === 'completed' && (!ad.play_limit || !ad.play_limit.exhausted)) ||
                                         (statusFilter === 'exhausted' && ad.play_limit && ad.play_limit.exhausted) ||
                                         (statusFilter === 'processing' && ['queued', 'processing', 'verifying', 'archiving'].includes(ad.status)) ||
                                         (statusFilter === 'failed' && ad.status === 'failed');
                    return matchesSearch && matchesAdv && matchesStatus;
                });

                const adsBody = document.getElementById('ads-body');
                adsBody.innerHTML = filtered.map(ad => {
                    const limit = ad.play_limit || {enabled: false};
                    const plays = limit.current_plays || 0;
                    const max = limit.max_plays || '∞';
                    const percent = limit.enabled ? Math.min(100, (plays / limit.max_plays) * 100) : 0;
                    const isExhausted = limit.enabled && limit.exhausted;

                    return `
                        <tr class="border-b border-slate-800 text-sm hover:bg-slate-800/50">
                            <td class="py-3 font-mono text-indigo-400">${ad.ad_id}</td>
                            <td class="py-3">${ad.description}</td>
                            <td class="py-3 text-slate-400">${ad.advertiser_name || 'Unknown'}</td>
                            <td class="py-3 text-slate-400">${ad.campaign_name || '—'}</td>
                            <td class="py-3">
                                <div class="flex flex-col">
                                    <span>${plays} / ${max}</span>
                                    ${limit.enabled ? `
                                    <div class="w-16 bg-slate-700 h-1 rounded-full mt-1 overflow-hidden">
                                        <div class="h-full ${percent > 80 ? 'bg-red-500' : 'bg-indigo-500'}" style="width: ${percent}%"></div>
                                    </div>` : ''}
                                </div>
                            </td>
                            <td class="py-3">${limit.enabled ? 'Limited' : '∞'}</td>
                            <td class="py-3">
                                <span class="status-badge status-${isExhausted ? 'exhausted' : ad.status}">
                                    ${isExhausted ? '🚫 Exhausted' : (ad.status === 'completed' ? '✅ Active' : ad.status)}
                                </span>
                            </td>
                            <td class="py-3 text-slate-400">${new Date(ad.upload_time).toLocaleDateString()}</td>
                            <td class="py-3 space-x-2">
                                <button onclick="openLimitModal('${ad.ad_id}')" class="text-indigo-400 hover:underline">Limit</button>
                                ${ad.status === 'completed' ? `<button onclick="previewAd('${ad.ad_id}')" class="text-indigo-400 hover:underline">Preview</button>` : ''}
                                ${ad.status === 'failed' ? `<button onclick="retryAd('${ad.ad_id}')" class="text-yellow-400 hover:underline">Retry</button>` : ''}
                                <button onclick="deleteAd('${ad.ad_id}')" class="text-red-400 hover:underline">Delete</button>
                            </td>
                        </tr>
                    `;
                }).join('');
            });
        }

        function formatRelativeTime(isoString) {
            const date = new Date(isoString);
            const now = new Date();
            const diff = Math.floor((now - date) / 1000);
            if (diff < 60) return 'just now';
            if (diff < 3600) return Math.floor(diff / 60) + 'm ago';
            if (diff < 86400) return Math.floor(diff / 3600) + 'h ago';
            return Math.floor(diff / 86400) + 'd ago';
        }

        function formatDuration(start, end) {
            if (!start || !end) return '—';
            const diff = Math.floor((new Date(end) - new Date(start)) / 1000);
            const m = Math.floor(diff / 60);
            const s = diff % 60;
            return `${m}m ${s}s`;
        }

        function toggleRow(jobId) {
            if (expandedRows.has(jobId)) expandedRows.delete(jobId);
            else expandedRows.add(jobId);
            loadHistory();
        }

        function toggleLogs(jobId) {
            const el = document.getElementById('logs-' + jobId);
            el.classList.toggle('hidden');
        }

        function sortHistory(field) {
            if (historySort === field) historySort = field + '_asc';
            else historySort = field;
            loadHistory();
        }

        function changeHistoryPage(delta) {
            historyPage += delta;
            loadHistory();
        }

        function debounceHistorySearch() {
            clearTimeout(historySearchTimeout);
            historySearchTimeout = setTimeout(() => {
                historyPage = 1;
                loadHistory();
            }, 300);
        }

        function filterHistoryByStatus(status) {
            document.getElementById('hist-filter-status').value = status;
            historyPage = 1;
            loadHistory();
        }

        function clearHistoryFilters() {
            document.getElementById('hist-filter-search').value = '';
            document.getElementById('hist-filter-status').value = 'all';
            document.getElementById('hist-filter-type').value = 'all';
            document.getElementById('hist-filter-from').value = '';
            document.getElementById('hist-filter-to').value = '';
            historyPage = 1;
            loadHistory();
        }

        function requeueJob(jobId) {
            if (confirm('Requeue this job for transcoding?')) {
                fetch(`api/job/${jobId}/requeue`, {method: 'POST'})
                    .then(r => r.json())
                    .then(data => {
                        if (data.status === 'queued') {
                            showToast(`Job added back to queue at position ${data.queue_position}`, 'success');
                            loadHistory();
                        } else {
                            showToast(data.error || 'Failed to requeue', 'error');
                        }
                    });
            }
        }

        function showToast(message, type = 'info') {
            const container = document.getElementById('toast-container');
            const toast = document.createElement('div');
            toast.className = `p-4 rounded shadow-lg text-white text-sm transition-opacity duration-500 ${type === 'success' ? 'bg-green-600' : (type === 'error' ? 'bg-red-600' : 'bg-indigo-600')}`;
            toast.innerText = message;
            container.appendChild(toast);
            setTimeout(() => {
                toast.style.opacity = '0';
                setTimeout(() => toast.remove(), 500);
            }, 3000);
        }

        function togglePlayLimitInput() {
            const type = document.getElementById('ad-play-limit-type').value;
            const input = document.getElementById('ad-max-plays');
            const options = document.getElementById('play-limit-options');
            if (type === 'custom') {
                input.classList.remove('hidden');
                options.classList.remove('hidden');
            } else {
                input.classList.add('hidden');
                options.classList.add('hidden');
            }
        }

        function openLimitModal(adId) {
            fetch(`api/ad/${adId}/plays`).then(r => r.json()).then(ad => {
                document.getElementById('limit-modal-info').innerText = `Ad: ${ad.ad_id} — ${ad.description} (${ad.advertiser_name})`;
                document.getElementById('modal-limit-enabled').checked = ad.play_limit_enabled;
                document.getElementById('modal-max-plays').value = ad.max_plays || 500;
                document.getElementById('modal-auto-disable').checked = true; // Default
                
                toggleModalLimitInput();
                
                document.getElementById('save-limit-btn').onclick = () => {
                    const payload = {
                        enabled: document.getElementById('modal-limit-enabled').checked,
                        max_plays: parseInt(document.getElementById('modal-max-plays').value),
                        auto_disable: document.getElementById('modal-auto-disable').checked
                    };
                    fetch(`api/ad/${adId}/play-limit`, {
                        method: 'PATCH',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify(payload)
                    }).then(r => r.json()).then(data => {
                        if (data.updated) {
                            showToast('Play limit updated', 'success');
                            closeLimitModal();
                            loadAds();
                        }
                    });
                };
                
                document.getElementById('limit-modal').classList.remove('hidden');
            });
        }

        function closeLimitModal() {
            document.getElementById('limit-modal').classList.add('hidden');
        }

        function toggleModalLimitInput() {
            const enabled = document.getElementById('modal-limit-enabled').checked;
            document.getElementById('modal-limit-input-container').classList.toggle('hidden', !enabled);
        }

        function filterAdsByStatus(status) {
            document.getElementById('ad-filter-status').value = status;
            updateDashboard();
        }

        function clearAdFilters() {
            document.getElementById('ad-filter-search').value = '';
            document.getElementById('ad-filter-advertiser').value = 'all';
            document.getElementById('ad-filter-status').value = 'all';
            updateDashboard();
        }

        // Ad Upload
        document.getElementById('ad-upload-form').onsubmit = function(e) {
            e.preventDefault();
            const desc = document.getElementById('ad-description').value;
            const file = document.getElementById('ad-file').files[0];
            if (!file) return alert('Choose a file');

            const formData = new FormData();
            formData.append('file', file);
            formData.append('description', desc);

            const container = document.getElementById('upload-progress-container');
            const bar = document.getElementById('upload-progress-bar');
            const status = document.getElementById('upload-status');
            
            container.classList.remove('hidden');
            
            const xhr = new XMLHttpRequest();
            xhr.open('POST', 'upload/ad', true);
            
            xhr.upload.onprogress = function(e) {
                if (e.lengthComputable) {
                    const percent = Math.round((e.loaded / e.total) * 100);
                    bar.style.width = percent + '%';
                    status.innerText = `Uploading: ${percent}%`;
                }
            };

            xhr.onload = function() {
                if (xhr.status === 200) {
                    alert('Upload successful! Job queued.');
                    document.getElementById('ad-upload-form').reset();
                    updateFileName();
                    container.classList.add('hidden');
                    updateDashboard();
                } else {
                    alert('Upload failed: ' + xhr.responseText);
                    container.classList.add('hidden');
                }
            };

            xhr.send(formData);
        };

        function previewAd(adId) {
            const modal = document.getElementById('preview-modal');
            const video = document.getElementById('video-player');
            const title = document.getElementById('preview-title');
            const meta = document.getElementById('preview-meta');
            
            title.innerText = `Preview: ${adId}`;
            modal.classList.remove('hidden');

            fetch(`api/ad/${adId}`).then(r => r.json()).then(ad => {
                meta.innerHTML = `
                    <div>Original: ${ad.original_filename}</div>
                    <div>Uploaded: ${new Date(ad.upload_time).toLocaleString()}</div>
                    <div>Status: ${ad.status}</div>
                `;
            });

            const hlsUrl = `/ads/${adId}/master.m3u8`;
            if (Hls.isSupported()) {
                const hls = new Hls();
                hls.loadSource(hlsUrl);
                hls.attachMedia(video);
                hls.on(Hls.Events.MANIFEST_PARSED, () => video.play());
            } else if (video.canPlayType('application/vnd.apple.mpegurl')) {
                video.src = hlsUrl;
                video.addEventListener('loadedmetadata', () => video.play());
            }
        }

        function closePreview() {
            const modal = document.getElementById('preview-modal');
            const video = document.getElementById('video-player');
            video.pause();
            video.src = "";
            modal.classList.add('hidden');
        }

        function deleteAd(adId) {
            if (confirm(`Delete ${adId} and all associated files?`)) {
                fetch(`api/ad/${adId}`, {method: 'DELETE'}).then(() => updateDashboard());
            }
        }

        function retryAd(adId) {
            fetch(`api/ad/${adId}/retry`, {method: 'POST'}).then(() => updateDashboard());
        }

        function removeJob(jobId) {
            if(confirm('Remove this job from queue?')) {
                fetch('api/remove/' + jobId, {method: 'POST'}).then(() => updateDashboard());
            }
        }

        function retryAllFailed() {
            if(confirm('Retry all failed jobs in history?')) {
                const btn = event.target;
                const originalText = btn.innerText;
                btn.innerText = 'Retrying...';
                btn.disabled = true;
                
                fetch('api/retry_failed', {method: 'POST'})
                    .then(r => r.json())
                    .then(data => {
                        alert(`Successfully re-queued ${data.count} failed jobs.`);
                        updateDashboard();
                    })
                    .catch(err => alert('Retry failed: ' + err))
                    .finally(() => {
                        btn.innerText = originalText;
                        btn.disabled = false;
                    });
            }
        }

        function triggerScan() {
            const btn = event.target;
            const originalText = btn.innerText;
            btn.innerText = 'Scanning...';
            btn.disabled = true;
            
            fetch('api/scan', {method: 'POST'})
                .then(r => r.json())
                .then(data => {
                    alert('Scan triggered successfully.');
                    updateDashboard();
                })
                .catch(err => alert('Scan failed: ' + err))
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
        queue_raw = r.lrange(config.QUEUE_NAME, 0, -1)
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
                webhook_online = True
            s.close()
        except: pass
        
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
        if status_filter != 'all' and status != status_filter: continue
        if type_filter != 'all' and job.get('type') != type_filter: continue
        if search and search not in job.get('input_path', '').lower(): continue
        
        if date_from:
            q_at = job.get('queued_at', '')
            if q_at and q_at < date_from: continue
        if date_to:
            q_at = job.get('queued_at', '')
            if q_at and q_at > date_to + "T23:59:59": continue

        all_jobs.append(job)

    # Sorting
    reverse = True
    sort_key = 'queued_at'
    if sort.endswith('_asc'):
        reverse = False
        sort = sort.replace('_asc', '')
    
    if sort == 'input_path': sort_key = 'input_path'
    elif sort == 'status': sort_key = 'status'
    elif sort == 'end_time': sort_key = 'end_time'

    all_jobs.sort(key=lambda x: x.get(sort_key, ''), reverse=reverse)

    # Pagination
    total = len(all_jobs)
    start = (page - 1) * per_page
    end = start + per_page
    paginated_jobs = all_jobs[start:end]

    return jsonify({
        "jobs": paginated_jobs,
        "counts": counts,
        "pagination": {
            "total": total,
            "page": page,
            "per_page": per_page,
            "total_pages": (total + per_page - 1) // per_page
        }
    })

@app.route('/api/job/<job_id>/requeue', methods=['POST'])
def requeue_job_api(job_id):
    history_key = f"{config.HISTORY_PREFIX}{job_id}"
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
    r.rpush(config.QUEUE_NAME, json.dumps(job_payload))
    
    return jsonify({
        "status": "queued",
        "job_id": job_id,
        "queue_position": r.llen(config.QUEUE_NAME)
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
            r_ads.hset(f"{config.AD_META_PREFIX}{ad_id}", "exhausted", "true")
            if meta.get('auto_disable') == 'true':
                r_ads.sadd(config.ADS_DISABLED_KEY, ad_id)
                logger.info(f"Ad {ad_id} automatically disabled - limit reached: {plays}/{max_plays}")
    
    return jsonify({"ad_id": ad_id, "current_plays": plays})

@app.route('/api/ad/<ad_id>/plays')
def get_ad_plays(ad_id):
    plays = r_ads.get(f"{config.AD_PLAYS_PREFIX}{ad_id}") or 0
    meta = r_ads.hgetall(f"{config.AD_META_PREFIX}{ad_id}")
    if not meta: return jsonify({"error": "Not found"}), 404
    
    return jsonify({
        "ad_id": ad_id,
        "description": meta.get('description'),
        "advertiser_name": meta.get('advertiser_name'),
        "current_plays": int(plays),
        "max_plays": int(meta.get('max_plays', 0)) if meta.get('max_plays') else None,
        "play_limit_enabled": meta.get('play_limit_enabled') == 'true',
        "exhausted": meta.get('exhausted') == 'true'
    })

@app.route('/api/ad/<ad_id>/play-limit', methods=['PATCH'])
def update_ad_play_limit(ad_id):
    data = request.json
    enabled = data.get('enabled', False)
    max_plays = data.get('max_plays', 0)
    auto_disable = data.get('auto_disable', True)

    mapping = {
        "play_limit_enabled": str(enabled).lower(),
        "max_plays": max_plays,
        "auto_disable": str(auto_disable).lower()
    }
    
    # Reset exhausted flag if limit increased or disabled
    current_plays = int(r_ads.get(f"{config.AD_PLAYS_PREFIX}{ad_id}") or 0)
    if not enabled or max_plays > current_plays:
        mapping["exhausted"] = "false"
        r_ads.srem(config.ADS_DISABLED_KEY, ad_id)

    r_ads.hset(f"{config.AD_META_PREFIX}{ad_id}", mapping=mapping)
    return jsonify({"updated": True, "ad_id": ad_id})

@app.route('/api/ads/disabled')
def get_disabled_ads():
    ad_ids = r_ads.smembers(config.ADS_DISABLED_KEY)
    return jsonify({"disabled_ads": list(ad_ids)})

@app.route('/api/ads')
def get_ads():
    ad_ids = r_ads.zrevrange(config.AD_REGISTRY_KEY, 0, -1)
    ads = []
    for ad_id in ad_ids:
        meta = r_ads.hgetall(f"{config.AD_META_PREFIX}{ad_id}")
        if meta:
            # Add play count info
            plays = r_ads.get(f"{config.AD_PLAYS_PREFIX}{ad_id}") or 0
            meta['play_limit'] = {
                "enabled": meta.get('play_limit_enabled') == 'true',
                "max_plays": int(meta.get('max_plays', 0)) if meta.get('max_plays') else None,
                "current_plays": int(plays),
                "exhausted": meta.get('exhausted') == 'true'
            }
            ads.append(meta)
        else:
            # Fallback to file if redis meta missing
            meta_path = os.path.join(config.ARCHIVE_BASE_ADS, ad_id, f"{ad_id}.json")
            if os.path.exists(meta_path):
                with open(meta_path, 'r') as f:
                    ads.append(json.load(f))
    return jsonify(ads)

@app.route('/api/ad/<ad_id>')
def get_ad(ad_id):
    meta_path = os.path.join(config.ARCHIVE_BASE_ADS, ad_id, f"{ad_id}.json")
    if os.path.exists(meta_path):
        with open(meta_path, 'r') as f:
            return jsonify(json.load(f))
    return jsonify({"error": "Not found"}), 404

@app.route('/api/ad/<ad_id>', methods=['DELETE'])
def delete_ad(ad_id):
    # Remove from Redis
    r_ads.zrem(config.AD_REGISTRY_KEY, ad_id)
    r_ads.delete(f"{config.AD_META_PREFIX}{ad_id}")
    r.delete(f"{config.HISTORY_PREFIX}{ad_id}")
    
    # Remove files
    archive_dir = os.path.join(config.ARCHIVE_BASE_ADS, ad_id)
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
        r.rpush(config.QUEUE_NAME, json.dumps(job_payload))
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
    ad_count = r_ads.zcard(config.AD_REGISTRY_KEY)
    next_num = ad_count + 1
    ad_id = f"advert{next_num:04d}"
    
    # Ensure ID is unique
    while r_ads.zscore(config.AD_REGISTRY_KEY, ad_id) is not None:
        next_num += 1
        ad_id = f"advert{next_num:04d}"

    archive_dir = os.path.join(config.ARCHIVE_BASE_ADS, ad_id)
    os.makedirs(archive_dir, exist_ok=True)
    
    input_filename = f"{ad_id}_original{ext}"
    input_path = os.path.join(archive_dir, input_filename)
    file.save(input_path)
    
    upload_time = datetime.now().isoformat()
    
    # Metadata
    ad_data = {
        "ad_id": ad_id,
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
    r.rpush(config.QUEUE_NAME, json.dumps(job_payload))
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
    queue_items = r.lrange(config.QUEUE_NAME, 0, -1)
    for item in queue_items:
        data = json.loads(item)
        if data.get('job_id') == job_id:
            r.lrem(config.QUEUE_NAME, 1, item)
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
