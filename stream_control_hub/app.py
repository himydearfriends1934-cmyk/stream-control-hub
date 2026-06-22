from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import uuid
from contextlib import suppress
from pathlib import Path
from typing import Any

import requests
from flask import Flask, jsonify, request
from werkzeug.utils import secure_filename


ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = ROOT / "config"
DATA_DIR = Path(os.environ.get("STREAM_HUB_DATA_DIR", str(ROOT / "data")))
MEDIA_DIR = DATA_DIR / "media"
WORK_DIR = DATA_DIR / "work"
NODES_FILE = Path(os.environ.get("STREAM_HUB_NODES_FILE", str(CONFIG_DIR / "nodes.json")))
PORT = int(os.environ.get("STREAM_HUB_PORT", "8788"))
SOURCE_REPO = os.environ.get(
    "STREAM_HUB_SOURCE_REPO",
    "https://github.com/himydearfriends1934-cmyk/istanbul-stream-dashboard.git",
)
SOURCE_BRANCH = os.environ.get("STREAM_HUB_SOURCE_BRANCH", "main")
ALLOWED_MEDIA_EXTENSIONS = {".mp4", ".mov", ".mkv", ".m4v", ".webm"}
NODE_UPLOAD_CHUNK_BYTES = int(os.environ.get("STREAM_HUB_NODE_UPLOAD_CHUNK_BYTES", str(8 * 1024 ** 2)))
NODE_PUBLIC_UPLOAD_CHUNK_BYTES = int(os.environ.get("STREAM_HUB_NODE_PUBLIC_UPLOAD_CHUNK_BYTES", str(16 * 1024 ** 2)))
NODE_UPLOAD_TIMEOUT_SECONDS = int(os.environ.get("STREAM_HUB_NODE_UPLOAD_TIMEOUT_SECONDS", "300"))
NODE_PUBLIC_UPLOAD_TTL_SECONDS = int(os.environ.get("STREAM_HUB_NODE_PUBLIC_UPLOAD_TTL_SECONDS", "900"))
NODE_UPLOAD_RETRIES = int(os.environ.get("STREAM_HUB_NODE_UPLOAD_RETRIES", "2"))
NODE_UPLOAD_PROBE_BYTES = int(os.environ.get("STREAM_HUB_NODE_UPLOAD_PROBE_BYTES", str(256 * 1024)))
NODE_UPLOAD_PROBE_TIMEOUT_SECONDS = int(os.environ.get("STREAM_HUB_NODE_UPLOAD_PROBE_TIMEOUT_SECONDS", "12"))
MIN_PUBLIC_UPLOAD_BYTES_PER_SECOND = int(os.environ.get("STREAM_HUB_MIN_PUBLIC_UPLOAD_BYTES_PER_SECOND", str(32 * 1024)))
MIN_FREE_AFTER_UPLOAD_BYTES = int(os.environ.get("STREAM_HUB_MIN_FREE_AFTER_UPLOAD_BYTES", str(2 * 1024 ** 3)))
UPLOAD_POLICY_NAME = os.environ.get("STREAM_HUB_UPLOAD_POLICY_NAME", "safe-stable-fast-v1")
PUSH_AUDIT_LOG = DATA_DIR / "push_audit.jsonl"
PUSH_AUDIT_LOG_MAX_BYTES = int(os.environ.get("STREAM_HUB_PUSH_AUDIT_LOG_MAX_BYTES", str(5 * 1024 ** 2)))

APP = Flask(__name__)
APP.config["MAX_CONTENT_LENGTH"] = int(os.environ.get("STREAM_HUB_MAX_UPLOAD_BYTES", str(200 * 1024 ** 3)))


HTML = r"""
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Stream Control Hub</title>
  <style>
    :root {
      --bg: #0c1110;
      --panel: #13201c;
      --panel-2: #192b25;
      --line: #31594c;
      --text: #effdf6;
      --muted: #9fc8b8;
      --accent: #36d399;
      --accent-2: #54c6eb;
      --bad: #fb7185;
      --warn: #fbbf24;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      color: var(--text);
      background:
        radial-gradient(circle at 12% 10%, rgba(54, 211, 153, 0.16), transparent 28%),
        radial-gradient(circle at 88% 0%, rgba(84, 198, 235, 0.14), transparent 24%),
        linear-gradient(135deg, #08100d, #111917 45%, #090d0c);
      font-family: "Segoe UI", "Microsoft YaHei", sans-serif;
    }
    .wrap { max-width: 1500px; margin: 0 auto; padding: 18px; }
    .hero {
      border: 1px solid var(--line);
      border-radius: 18px;
      padding: 18px;
      background: rgba(19, 32, 28, 0.88);
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 16px;
      align-items: center;
    }
    h1 { margin: 0; font-size: 30px; letter-spacing: -0.03em; }
    p { color: var(--muted); margin: 8px 0 0; line-height: 1.6; }
    .grid { display: grid; grid-template-columns: minmax(620px, 1fr) minmax(390px, 420px); gap: 12px; margin-top: 12px; align-items: start; }
    .side-stack { display: grid; gap: 12px; align-content: start; }
    .bottom-section { grid-column: 1 / -1; display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
    .card {
      border: 1px solid var(--line);
      border-radius: 16px;
      padding: 12px;
      background: rgba(19, 32, 28, 0.9);
      box-shadow: 0 18px 60px rgba(0,0,0,0.18);
    }
    .card h2 { margin: 0 0 12px; font-size: 18px; }
    .actions { display: flex; flex-wrap: wrap; gap: 8px; }
    button, input, select {
      border: 1px solid var(--line);
      border-radius: 10px;
      padding: 10px 12px;
      background: var(--panel-2);
      color: var(--text);
      font: inherit;
    }
    button { cursor: pointer; font-weight: 800; }
    button.primary { background: linear-gradient(135deg, var(--accent), var(--accent-2)); color: #04100c; border: none; }
    button.danger { background: #6f1d2d; color: #ffe4ea; }
    button.tiny { padding: 7px 8px; font-size: 12px; border-radius: 8px; white-space: nowrap; }
    button:disabled { opacity: 0.55; cursor: not-allowed; }
    input[type=file] { width: 100%; }
    .media-list, .log { display: grid; gap: 10px; }
    .node, .media {
      display: grid;
      grid-template-columns: auto 1fr auto;
      gap: 10px;
      align-items: center;
      padding: 10px;
      border-radius: 12px;
      border: 1px solid rgba(49, 89, 76, 0.8);
      background: rgba(25, 43, 37, 0.78);
    }
    .command-strip {
      margin-top: 12px;
      border-color: rgba(251, 191, 36, 0.45);
      background:
        radial-gradient(circle at 8% 0%, rgba(251, 191, 36, 0.14), transparent 24%),
        rgba(25, 35, 27, 0.95);
    }
    .command-head {
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: flex-start;
      margin-bottom: 10px;
    }
    .command-head h2 { margin: 0 0 4px; font-size: 18px; }
    .command-head p { margin: 0; font-size: 13px; }
    .command-grid {
      display: grid;
      grid-template-columns: 1.1fr 1.35fr 1.45fr 1.25fr auto;
      gap: 10px;
      align-items: end;
    }
    .command-field { display: grid; gap: 5px; min-width: 0; }
    .command-field label { color: var(--muted); font-size: 12px; font-weight: 800; }
    .command-field input,
    .command-field select { min-width: 0; padding: 8px 10px; }
    .command-pair { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
    .command-actions {
      display: grid;
      grid-template-columns: 1fr;
      gap: 7px;
      min-width: 136px;
    }
    .command-actions button { padding: 8px 10px; }
    .tune-output {
      grid-column: 1 / -1;
      min-height: 70px;
      max-height: 140px;
      margin-top: 2px;
    }
    .monitor-card { min-height: 620px; }
    .monitor-heading {
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: flex-start;
    }
    .monitor-heading p { margin: 0; font-size: 13px; }
    .node-monitor {
      min-height: 540px;
      border-radius: 16px;
      padding: 12px;
      border: 1px solid rgba(54, 211, 153, 0.35);
      background:
        linear-gradient(rgba(54, 211, 153, 0.035) 1px, transparent 1px),
        linear-gradient(90deg, rgba(54, 211, 153, 0.035) 1px, transparent 1px),
        radial-gradient(circle at 10% 0%, rgba(54, 211, 153, 0.18), transparent 26%),
        radial-gradient(circle at 100% 12%, rgba(84, 198, 235, 0.12), transparent 24%),
        #07110e;
      background-size: 24px 24px, 24px 24px, auto, auto, auto;
      box-shadow: inset 0 0 42px rgba(54, 211, 153, 0.06);
    }
    .monitor-hero {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 10px;
      align-items: start;
      margin-bottom: 10px;
      padding-bottom: 10px;
      border-bottom: 1px solid rgba(49, 89, 76, 0.55);
    }
    .monitor-hero h3 { margin: 0; font-size: 24px; letter-spacing: -0.03em; }
    .monitor-hero small { color: var(--muted); display: block; margin-top: 4px; }
    .health-strip { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 8px; margin-bottom: 10px; }
    .health-donut {
      display: grid;
      grid-template-columns: 58px minmax(0, 1fr);
      gap: 8px;
      align-items: center;
      padding: 8px;
      border: 1px solid rgba(49, 89, 76, 0.75);
      border-radius: 12px;
      background: rgba(8, 17, 14, 0.38);
      min-width: 0;
    }
    .donut {
      width: 54px;
      height: 54px;
      border-radius: 50%;
      display: grid;
      place-items: center;
      background:
        radial-gradient(circle at center, #07110e 0 54%, transparent 55%),
        conic-gradient(var(--donut-color, var(--accent)) var(--value, 0%), rgba(255,255,255,0.08) 0);
      box-shadow: inset 0 0 14px rgba(0,0,0,0.24);
      font-size: 12px;
      font-weight: 900;
    }
    .donut-info small { color: var(--muted); display: block; font-size: 12px; }
    .donut-info strong { display: block; font-size: 16px; line-height: 1.2; margin-top: 2px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .network-panel {
      display: grid;
      grid-template-columns: 1.2fr 1fr;
      gap: 8px;
      margin-bottom: 8px;
    }
    .network-live { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 6px; }
    .monitor-panel-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
    .monitor-panel {
      border: 1px solid rgba(49, 89, 76, 0.75);
      border-radius: 12px;
      padding: 9px;
      background: rgba(9, 17, 14, 0.58);
    }
    .monitor-panel h4 { margin: 0 0 6px; font-size: 14px; color: #d6fff0; }
    .node-table-card { min-height: 0; }
    .node-table-toolbar {
      display: flex;
      justify-content: space-between;
      gap: 10px;
      align-items: center;
      margin-bottom: 10px;
    }
    .node-table-toolbar p { margin: 0; font-size: 13px; }
    .node-table {
      display: grid;
      gap: 7px;
      max-height: 430px;
      overflow: auto;
      padding-right: 3px;
    }
    .node-table-head,
    .node-row {
      display: grid;
      grid-template-columns: 26px minmax(0, 1fr) 58px 72px 136px;
      gap: 8px;
      align-items: center;
    }
    .node-table-head {
      position: sticky;
      top: 0;
      z-index: 1;
      padding: 7px 10px;
      color: var(--muted);
      font-size: 12px;
      background: rgba(19, 32, 28, 0.96);
      border-bottom: 1px solid rgba(49, 89, 76, 0.55);
    }
    .node-row {
      min-height: 50px;
      padding: 7px 9px;
      border: 1px solid rgba(49, 89, 76, 0.75);
      border-radius: 12px;
      background: rgba(25, 43, 37, 0.72);
      cursor: pointer;
      transition: border-color 0.16s ease, transform 0.16s ease, background 0.16s ease;
    }
    .node-row:hover,
    .node-row.selected {
      border-color: rgba(54, 211, 153, 0.85);
      background: rgba(20, 55, 43, 0.86);
    }
    .node-row.selected { box-shadow: 0 0 0 1px rgba(54, 211, 153, 0.22); }
    .node-name strong { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .node-name small { color: var(--muted); display: block; margin-top: 2px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .node-state { display: inline-flex; align-items: center; gap: 6px; font-size: 12px; font-weight: 800; }
    .dot { width: 9px; height: 9px; border-radius: 999px; background: #fbbf24; box-shadow: 0 0 16px rgba(251, 191, 36, 0.35); }
    .dot.ok { background: var(--accent); box-shadow: 0 0 16px rgba(54, 211, 153, 0.45); }
    .dot.bad { background: var(--danger); box-shadow: 0 0 16px rgba(251, 113, 133, 0.4); }
    .row-actions { display: grid; grid-template-columns: 1fr 1fr; gap: 6px; }
    .empty-state {
      min-height: 220px;
      display: grid;
      place-items: center;
      color: var(--muted);
      text-align: center;
      border: 1px dashed rgba(49, 89, 76, 0.8);
      border-radius: 14px;
    }
    .node-detail {
      display: grid;
      grid-template-columns: 1.05fr 1.15fr 0.9fr;
      gap: 12px;
      padding: 14px;
      border-radius: 16px;
      border: 1px solid rgba(49, 89, 76, 0.85);
      background: rgba(25, 43, 37, 0.78);
    }
    .node-title {
      display: flex;
      justify-content: space-between;
      gap: 10px;
      align-items: flex-start;
      margin-bottom: 10px;
    }
    .node-title strong { display: block; font-size: 18px; }
    .node-title small { color: var(--muted); display: block; margin-top: 3px; }
    .metric-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 6px; }
    .metric {
      border: 1px solid rgba(49, 89, 76, 0.75);
      border-radius: 10px;
      padding: 8px;
      background: rgba(8, 17, 14, 0.35);
    }
    .metric small, .mini-table small { color: var(--muted); display: block; font-size: 12px; }
    .metric strong { display: block; font-size: 20px; margin-top: 2px; }
    .bar {
      height: 8px;
      border-radius: 999px;
      background: rgba(255,255,255,0.08);
      overflow: hidden;
      margin-top: 8px;
    }
    .bar > span {
      display: block;
      height: 100%;
      width: var(--value, 0%);
      background: linear-gradient(90deg, var(--accent), var(--accent-2));
    }
    .mini-table { display: grid; gap: 8px; }
    .mini-row {
      display: grid;
      grid-template-columns: 112px minmax(0, 1fr);
      gap: 8px;
      padding: 5px 0;
      border-bottom: 1px solid rgba(49, 89, 76, 0.4);
    }
    .mini-row:last-child { border-bottom: none; }
    .mono { font-family: "Cascadia Mono", "Consolas", monospace; word-break: break-word; }
    .compact-card { padding: 10px; }
    .node strong, .media strong { display: block; }
    .node small, .media small { color: var(--muted); }
    .resource-card { display: grid; gap: 10px; }
    .resource-card .split { grid-template-columns: 1fr; }
    .resource-card .actions { display: grid; grid-template-columns: 1fr; }
    .resource-card pre { min-height: 96px; max-height: 190px; overflow: auto; }
    .resource-card .media-list { max-height: 260px; overflow: auto; padding-right: 3px; }
    .pill {
      display: inline-flex;
      padding: 5px 8px;
      border-radius: 999px;
      background: rgba(54, 211, 153, 0.14);
      color: #b7f7dc;
      font-size: 12px;
      font-weight: 800;
    }
    .pill.bad { background: rgba(251, 113, 133, 0.14); color: #fecdd3; }
    .pill.warn { background: rgba(251, 191, 36, 0.15); color: #fde68a; }
    pre {
      white-space: pre-wrap;
      word-break: break-word;
      margin: 0;
      min-height: 120px;
      max-height: 360px;
      overflow: auto;
      padding: 12px;
      border-radius: 12px;
      background: #09110e;
      border: 1px solid var(--line);
      color: #c9f7e7;
    }
    .split { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
    @media (max-width: 1080px) {
      .grid, .split, .hero, .node-detail, .bottom-section, .health-strip, .network-panel, .network-live, .monitor-panel-grid, .command-grid { grid-template-columns: 1fr; }
      .bottom-section { grid-column: auto; }
      .monitor-card, .node-table-card { min-height: auto; }
      .node-monitor { min-height: 420px; }
      .node-table { max-height: none; }
      .node-table-head { display: none; }
      .node-row { grid-template-columns: 24px minmax(0, 1fr); }
      .node-state, .row-actions { grid-column: 2; }
    }
  </style>
</head>
<body>
  <div class="wrap">
    <section class="hero">
      <div>
        <h1>Stream Control Hub</h1>
        <p>本地总控台：集中监控 VPS 推流节点，本地上传资源，再选择推送到一台或多台 VPS。升级面板时不触碰正在运行的 FFmpeg 推流。</p>
      </div>
      <div class="actions">
        <button class="primary" id="refreshBtn">刷新状态</button>
        <button id="policyBtn">Upload Policy</button>
        <button id="auditBtn">Push Audit</button>
      </div>
    </section>

    <section class="card command-strip">
      <div class="command-head">
        <div>
          <h2>开播指挥条 / Smart Start</h2>
          <p>和右侧 VPS 节点表联动：右侧选中哪台，这里就控制哪台。先核对目标节点，再填直播码、选视频、调优、开播。</p>
        </div>
        <span class="pill warn">核对节点后再开播</span>
      </div>
      <div class="command-grid">
        <div class="command-field">
          <label>当前控制节点</label>
          <input id="streamNodeInput" type="text" readonly value="选择右侧 VPS 节点">
          <small id="streamNodeHint" class="mono">等待选择节点</small>
        </div>
        <div class="command-field">
          <label>服务器视频</label>
          <select id="streamVideoSelect">
            <option value="">先选择节点...</option>
          </select>
        </div>
        <div class="command-field">
          <label>YouTube Stream Key</label>
          <input id="streamKeyInput" type="password" autocomplete="off" placeholder="粘贴直播码，只会转发到当前节点">
        </div>
        <div class="command-field">
          <label>输出 / 自适应</label>
          <div class="command-pair">
            <select id="streamOutputModeInput">
              <option value="direct">直接推 YouTube</option>
              <option value="local_relay">本地中继</option>
            </select>
            <select id="adaptiveModeInput">
              <option value="auto">自动调优</option>
              <option value="off">固定参数</option>
            </select>
          </div>
        </div>
        <div class="command-actions">
          <button id="previewTuneBtn">预览调优</button>
          <button class="primary" id="smartStartBtn">Smart Start</button>
        </div>
        <div class="command-field">
          <label>RTMP 地址</label>
          <input id="streamUrlInput" type="text" value="rtmp://a.rtmp.youtube.com/live2">
        </div>
        <div class="command-field">
          <label>分辨率 / FPS</label>
          <div class="command-pair">
            <input id="resolutionInput" type="text" value="1280x720" placeholder="分辨率">
            <input id="fpsInput" type="number" value="30" min="15" max="60" placeholder="FPS">
          </div>
        </div>
        <div class="command-field">
          <label>码率</label>
          <div class="command-pair">
            <input id="videoBitrateInput" type="number" value="4500" min="800" placeholder="视频 kbps">
            <input id="audioBitrateInput" type="number" value="192" min="64" placeholder="音频 kbps">
          </div>
        </div>
        <div class="command-field">
          <label>编码 / 关键帧</label>
          <div class="command-pair">
            <input id="presetInput" type="text" value="veryfast" placeholder="preset">
            <input id="keyframeInput" type="number" value="2" min="1" max="4" placeholder="关键帧秒">
          </div>
        </div>
        <div class="command-actions">
          <button id="applyTuneBtn">应用推荐</button>
        </div>
        <pre id="tuneBox" class="tune-output">选择右侧节点和服务器视频后，可以预览推荐参数；Smart Start 会停止重复推流并启动一个干净 FFmpeg。</pre>
      </div>
    </section>

    <section class="grid">
      <div class="card monitor-card">
        <div class="monitor-heading">
          <div>
            <h2>节点监控屏</h2>
            <p>点击右侧 VPS 节点，左侧集中显示健康状态、网络吞吐、推流码率和节点配置。</p>
          </div>
          <span class="pill">live view</span>
        </div>
        <div class="node-monitor" id="nodeMonitor">
          <div class="empty-state">正在读取节点状态...</div>
        </div>
      </div>

      <div class="side-stack">
        <div class="card node-table-card">
          <div class="node-table-toolbar">
            <div>
              <h2>VPS 节点表</h2>
              <p>一屏预留约 10 台：在线、推流、重启推流、重启 VPS。</p>
            </div>
            <span class="pill warn">protected</span>
          </div>
          <div class="node-table" id="nodeList">加载中...</div>
        </div>

        <div class="card resource-card">
          <h2>本地资源与推送</h2>
          <div class="split">
            <div>
              <input id="mediaInput" type="file" accept=".mp4,.mov,.mkv,.m4v,.webm">
              <div class="actions" style="margin-top: 8px;">
                <button class="primary" id="uploadBtn">上传到总控台</button>
                <button id="pushSelectedBtn">推送到选中 VPS</button>
              </div>
            </div>
            <pre id="uploadBox">先把视频上传到总控台，再选择 VPS 推送。</pre>
          </div>
          <div class="media-list" id="mediaList">加载中...</div>
        </div>
      </div>

      <div class="card">
        <h2>策略 / 审计 / 操作日志</h2>
        <pre id="updateBox">点击 Upload Policy 或 Push Audit 查看系统规则与最近推送记录。</pre>
        <div style="height: 10px;"></div>
        <pre id="logBox">就绪。</pre>
      </div>

      <div class="bottom-section">
        <div class="card compact-card">
          <h2>GitHub 更新</h2>
          <p>低频维护功能放在底部，不占用节点监控主视野。</p>
          <div class="actions">
            <button id="checkUpdatesBtn">检查 GitHub 更新</button>
            <button class="primary" id="upgradeSelectedBtn">更新选中节点</button>
          </div>
        </div>
        <div class="card compact-card">
          <h2>当前策略</h2>
          <p>上传链路固定为 safe-stable-fast：公网 probe、速度阈值、失败切内网、审计脱敏。</p>
        </div>
      </div>
    </section>
  </div>

  <script>
    const refs = {
      nodeList: document.getElementById("nodeList"),
      nodeMonitor: document.getElementById("nodeMonitor"),
      mediaList: document.getElementById("mediaList"),
      refreshBtn: document.getElementById("refreshBtn"),
      checkUpdatesBtn: document.getElementById("checkUpdatesBtn"),
      policyBtn: document.getElementById("policyBtn"),
      auditBtn: document.getElementById("auditBtn"),
      upgradeSelectedBtn: document.getElementById("upgradeSelectedBtn"),
      mediaInput: document.getElementById("mediaInput"),
      uploadBtn: document.getElementById("uploadBtn"),
      pushSelectedBtn: document.getElementById("pushSelectedBtn"),
      streamNodeInput: document.getElementById("streamNodeInput"),
      streamNodeHint: document.getElementById("streamNodeHint"),
      streamVideoSelect: document.getElementById("streamVideoSelect"),
      streamKeyInput: document.getElementById("streamKeyInput"),
      streamUrlInput: document.getElementById("streamUrlInput"),
      streamOutputModeInput: document.getElementById("streamOutputModeInput"),
      adaptiveModeInput: document.getElementById("adaptiveModeInput"),
      resolutionInput: document.getElementById("resolutionInput"),
      fpsInput: document.getElementById("fpsInput"),
      videoBitrateInput: document.getElementById("videoBitrateInput"),
      audioBitrateInput: document.getElementById("audioBitrateInput"),
      presetInput: document.getElementById("presetInput"),
      keyframeInput: document.getElementById("keyframeInput"),
      previewTuneBtn: document.getElementById("previewTuneBtn"),
      smartStartBtn: document.getElementById("smartStartBtn"),
      applyTuneBtn: document.getElementById("applyTuneBtn"),
      tuneBox: document.getElementById("tuneBox"),
      updateBox: document.getElementById("updateBox"),
      uploadBox: document.getElementById("uploadBox"),
      logBox: document.getElementById("logBox"),
    };
    let nodes = [];
    let media = [];
    let selectedNodeId = "";
    let lastTuneRecommendation = null;

    function selectedNodeIds() {
      return [...document.querySelectorAll("[data-node-check]:checked")].map((el) => el.value);
    }

    function selectedMediaName() {
      const checked = document.querySelector("[data-media-check]:checked");
      return checked ? checked.value : "";
    }

    function log(message) {
      refs.logBox.textContent = `${new Date().toLocaleTimeString()} ${message}\n${refs.logBox.textContent}`.trim();
    }

    function nodeStatusPill(node) {
      if (node.enabled === false) return `<span class="pill warn">disabled</span>`;
      if (!node.health?.ok) return `<span class="pill bad">offline</span>`;
      return `<span class="pill">online</span>`;
    }

    function escapeHtml(value) {
      return String(value ?? "")
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#39;");
    }

    function stateDot(ok, warn = false) {
      return `<span class="dot ${ok ? "ok" : warn ? "" : "bad"}"></span>`;
    }

    function fmtBytes(bytes) {
      const units = ["B", "KB", "MB", "GB", "TB"];
      let value = Number(bytes || 0);
      let index = 0;
      while (value >= 1024 && index < units.length - 1) {
        value /= 1024;
        index += 1;
      }
      return index ? `${value.toFixed(1)} ${units[index]}` : `${Math.round(value)} B`;
    }

    function fmtRate(bytesPerSecond) {
      return `${fmtBytes(bytesPerSecond)}/s`;
    }

    function pct(value) {
      return Math.max(0, Math.min(100, Number(value || 0)));
    }

    function metric(label, value, percent) {
      const hasPercent = percent !== undefined && percent !== null;
      return `
        <div class="metric">
          <small>${escapeHtml(label)}</small>
          <strong>${escapeHtml(value)}</strong>
          ${hasPercent ? `<div class="bar" style="--value:${pct(percent)}%"><span></span></div>` : ""}
        </div>
      `;
    }

    function donut(label, value, percent, color = "var(--accent)") {
      const safePercent = pct(percent);
      return `
        <div class="health-donut">
          <div class="donut" style="--value:${safePercent}%; --donut-color:${color};">${Math.round(safePercent)}%</div>
          <div class="donut-info">
            <small>${escapeHtml(label)}</small>
            <strong>${escapeHtml(value)}</strong>
          </div>
        </div>
      `;
    }

    function miniRow(label, value) {
      return `<div class="mini-row"><small>${escapeHtml(label)}</small><span>${escapeHtml(value)}</span></div>`;
    }

    function miniRowHtml(label, html) {
      return `<div class="mini-row"><small>${escapeHtml(label)}</small><span>${html}</span></div>`;
    }

    function nodeOnline(node) {
      return Boolean(node.enabled !== false && node.health?.ok);
    }

    function nodeStreaming(node) {
      return Boolean(node.health?.stream?.running);
    }

    function selectedNode() {
      return nodes.find((node) => String(node.id) === String(selectedNodeId)) || nodes[0] || null;
    }

    function renderMonitor(node) {
      if (!node) {
        return `<div class="empty-state">还没有配置节点。把 VPS 节点加入 config/nodes.json 后会显示在这里。</div>`;
      }
      const h = node.health || {};
      const stream = h.stream || {};
      const adaptive = stream.adaptive || {};
      const autoRestart = stream.auto_restart || {};
      const relay = stream.relay || {};
      const tuning = stream.tuning || {};
      const config = h.stream_config || {};
      const net = h.net || {};
      const quota = h.quota || {};
      const agent = h.agent || {};
      const transfer = h.transfer || {};
      const publicUpload = h.public_upload || {};
      const videos = h.videos || [];
      const loadText = Array.isArray(h.load_avg) ? h.load_avg.join(" / ") : (h.load_avg || "--");
      const loadOne = Array.isArray(h.load_avg) ? Number(h.load_avg[0] || 0) : Number(String(h.load_avg || "0").split("/")[0] || 0);
      const loadPercent = h.cpu_count ? Math.min(100, (loadOne / Math.max(1, Number(h.cpu_count))) * 100) : 0;
      const bitrate = stream.current_bitrate_label || (stream.current_bitrate_kbps ? `${stream.current_bitrate_kbps} Kbps` : "未知");
      const processText = stream.processes?.length ? `${stream.processes.length} 个进程` : "未检测到";
      const videoList = videos.length
        ? videos.slice(0, 6).map((item) => `${escapeHtml(item.name)} (${escapeHtml(fmtBytes(item.size))})`).join("<br>")
        : "服务器暂无视频";
      const processList = stream.processes?.length
        ? stream.processes.slice(0, 4).map((item) => {
            const pid = item.pid || item.PID || "-";
            const cpu = item.cpu_percent !== undefined ? ` CPU ${Number(item.cpu_percent || 0).toFixed(1)}%` : "";
            return `${escapeHtml(pid)}${escapeHtml(cpu)}`;
          }).join("<br>")
        : "未检测到 FFmpeg 进程";

      return `
        <div class="monitor-hero">
          <div>
            <h3>${escapeHtml(node.name || node.id)}</h3>
            <small>${escapeHtml(h.hostname || node.id)} · ${escapeHtml(h.platform || "未知系统")}</small>
            <small class="mono">${escapeHtml(node.base_url || "")}</small>
          </div>
          ${nodeStatusPill(node)}
        </div>

        <div class="health-strip">
          ${donut("CPU", `${Number(h.cpu_percent || 0).toFixed(1)}%`, h.cpu_percent)}
          ${donut("内存", `${Number(h.memory?.percent || 0).toFixed(1)}%`, h.memory?.percent)}
          ${donut("硬盘", `${Number(h.disk?.percent || 0).toFixed(1)}%`, h.disk?.percent)}
          ${donut("负载", loadText, loadPercent, "#fbbf24")}
          ${donut("推流", stream.running ? "运行中" : "未推流", stream.running ? 100 : 0, stream.running ? "var(--accent)" : "var(--danger)")}
        </div>

        <div class="network-panel">
          <div class="monitor-panel">
            <h4>网络实时</h4>
            <div class="network-live">
              ${metric("实时上传", fmtRate(net.current_upload_bps || 0))}
              ${metric("实时下载", fmtRate(net.current_download_bps || 0))}
            </div>
            <div class="mini-table" style="margin-top: 10px;">
              ${miniRow("速率标签", net.rate_label || "--")}
              ${miniRow("上传策略", "公网优先 / 慢速回落内网 / 分块重试")}
            </div>
          </div>

          <div class="monitor-panel">
            <h4>网络累计</h4>
            <div class="metric-grid">
              ${metric("累计发送", fmtBytes(net.bytes_sent || 0))}
              ${metric("累计接收", fmtBytes(net.bytes_recv || 0))}
              ${metric("流量占用", `${Number(quota.total_percent || 0).toFixed(2)}%`, quota.total_percent)}
              ${metric("剩余额度", fmtBytes(quota.remaining || 0))}
            </div>
            <div class="mini-table" style="margin-top: 10px;">
              ${miniRow("总额度", fmtBytes(quota.limit || 0))}
              ${miniRow("已用总量", fmtBytes(quota.total_used || 0))}
            </div>
          </div>
        </div>

        <div class="monitor-panel-grid">
          <div class="monitor-panel">
            <h4>机器详情</h4>
            <div class="mini-table">
              ${miniRow("逻辑核心", h.cpu_count || "--")}
              ${miniRow("系统在线", h.uptime || "--")}
              ${miniRow("面板在线", h.app_uptime || "--")}
              ${miniRow("内存用量", `${fmtBytes(h.memory?.used || 0)} / ${fmtBytes(h.memory?.total || 0)}`)}
              ${miniRow("硬盘用量", `${fmtBytes(h.disk?.used || 0)} / ${fmtBytes(h.disk?.total || 0)}`)}
            </div>
          </div>

          <div class="monitor-panel">
            <h4>客户端 Agent</h4>
            <div class="metric-grid">
              ${metric("运行模式", agent.mode || "dashboard-compatible")}
              ${metric("Headless", agent.headless ? "开启" : "兼容面板")}
              ${metric("Agent 版本", agent.version || "--")}
              ${metric("公网窗口", publicUpload.enabled ? "开启" : "关闭")}
            </div>
            <div class="mini-table" style="margin-top: 10px;">
              ${miniRow("Agent 名称", agent.name || h.hostname || node.id || "--")}
              ${miniRow("控制端", agent.control_hub || "--")}
              ${miniRow("窗口来源", publicUpload.public_origin || "--")}
              ${miniRow("窗口原因", publicUpload.last_reason || "--")}
            </div>
          </div>

          <div class="monitor-panel">
            <h4>传输监控</h4>
            <div class="metric-grid">
              ${metric("活跃上传", `${transfer.active_upload_count || 0}`)}
              ${metric("已收总量", fmtBytes(transfer.bytes_received_total || 0))}
              ${metric("已收分块", `${transfer.chunks_received_total || 0}`)}
              ${metric("完成上传", `${transfer.completed_uploads_total || 0}`)}
            </div>
            <div class="mini-table" style="margin-top: 10px;">
              ${miniRow("最后事件", transfer.last_event || "--")}
              ${miniRow("最后路由", transfer.last_route || "--")}
              ${miniRow("最近错误", transfer.last_error || "无")}
              ${miniRow("最后更新时间", transfer.last_event_at_label || "--")}
              ${miniRow("最近测速", transfer.last_probe?.elapsed_ms ? `${transfer.last_probe.elapsed_ms} ms / ${fmtBytes(transfer.last_probe.size || 0)}` : "--")}
            </div>
          </div>

          <div class="monitor-panel">
            <h4>推流引擎</h4>
            <div class="metric-grid">
              ${metric("FFmpeg", stream.running ? "运行中" : "未运行")}
              ${metric("进程", processText)}
              ${metric("视频数", `${videos.length}`)}
              ${metric("直播码", config.has_stream_key ? "已保存" : "未保存")}
            </div>
            <div class="mini-table" style="margin-top: 10px;">
              ${miniRow("自动重启", autoRestart.enabled ? `开启 · ${autoRestart.last_error || "正常"}` : "关闭")}
              ${miniRow("智能调参", adaptive.enabled ? `${adaptive.status || "idle"} · ${adaptive.last_error || "正常"}` : "关闭")}
              ${miniRow("本地中继", relay.enabled ? `${relay.mode || "relay"} · ${relay.reachable ? "可达" : "不可达"}` : relay.message || "关闭")}
              ${miniRow("FIFO 缓冲", tuning.fifo_enabled ? `${tuning.fifo_timeshift_seconds || 0}s / queue ${tuning.fifo_queue_size || 0}` : "关闭")}
              ${miniRowHtml("FFmpeg PID", `<span class="mono">${processList}</span>`)}
            </div>
          </div>

          <div class="monitor-panel">
            <h4>节点资源</h4>
            <div class="mini-table" style="margin-top: 10px;">
              ${miniRow("节点 ID", node.id || "--")}
              ${miniRow("启用状态", node.enabled === false ? "已禁用" : "已启用")}
              ${miniRow("健康采集", h.ok ? "正常" : (h.message || "不可达"))}
              ${miniRowHtml("服务器视频", `<span class="mono">${videoList}</span>`)}
            </div>
          </div>
        </div>
      `;
    }

    function renderNodeRow(node, checkedIds) {
      const h = node.health || {};
      const online = nodeOnline(node);
      const streaming = nodeStreaming(node);
      const selected = String(node.id) === String(selectedNodeId);
      const checked = checkedIds.has(String(node.id));
      return `
        <div class="node-row ${selected ? "selected" : ""}" data-node-row data-node-id="${escapeHtml(node.id)}">
          <input data-node-check type="checkbox" value="${escapeHtml(node.id)}" ${checked ? "checked" : ""} ${node.enabled === false ? "disabled" : ""} title="选中后可推送资源或升级">
          <span class="node-name">
            <strong>${escapeHtml(node.name || node.id)}</strong>
            <small>${escapeHtml(h.hostname || node.id)} · ${escapeHtml(h.platform || "未知")}</small>
          </span>
          <span class="node-state">${stateDot(online, node.enabled === false)}${online ? "在线" : node.enabled === false ? "禁用" : "离线"}</span>
          <span class="node-state">${stateDot(streaming, online)}${streaming ? "推流中" : "未推流"}</span>
          <span class="row-actions">
            <button class="tiny" data-node-action="restart-stream" data-node-id="${escapeHtml(node.id)}" ${online ? "" : "disabled"}>重启推流</button>
            <button class="tiny danger" data-node-action="reboot-vps" data-node-id="${escapeHtml(node.id)}" ${online ? "" : "disabled"}>重启 VPS</button>
          </span>
        </div>
      `;
    }

    function renderNodes() {
      const checkedIds = new Set(selectedNodeIds().map(String));
      if (!nodes.length) {
        refs.nodeMonitor.innerHTML = renderMonitor(null);
        refs.nodeList.innerHTML = `<div class="empty-state">还没有配置节点。</div>`;
        return;
      }
      if (!nodes.some((node) => String(node.id) === String(selectedNodeId))) {
        selectedNodeId = String(nodes[0].id || "");
      }
      refs.nodeMonitor.innerHTML = renderMonitor(selectedNode());
      refs.nodeList.innerHTML = `
        <div class="node-table-head">
          <span></span>
          <span>节点</span>
          <span>在线</span>
          <span>推流</span>
          <span>操作</span>
        </div>
        ${nodes.map((node) => renderNodeRow(node, checkedIds)).join("")}
      `;
    }

    function renderMedia() {
      refs.mediaList.innerHTML = media.length ? media.map((item) => `
        <label class="media">
          <input data-media-check type="radio" name="media" value="${escapeHtml(item.name)}">
          <span>
            <strong>${escapeHtml(item.name)}</strong>
            <small>${escapeHtml(item.size_label)} | ${escapeHtml(item.modified_label)}</small>
          </span>
          <span class="pill">local</span>
        </label>
      `).join("") : "本地资源库还没有视频。";
    }

    function renderStreamControls() {
      const node = selectedNode();
      const h = node?.health || {};
      const videos = h.videos || [];
      refs.streamNodeInput.value = node ? `${node.name || node.id} (${node.id})` : "选择右侧 VPS 节点";
      refs.streamNodeHint.textContent = node
        ? `${h.ok ? "在线" : "离线"} / ${h.agent?.mode || "旧客户端"} / ${h.stream?.running ? "推流中" : "未推流"}`
        : "等待选择节点";
      refs.streamVideoSelect.innerHTML = videos.length ? videos.map((item) => `
        <option value="${escapeHtml(item.video_path || item.path || item.name)}">${escapeHtml(item.name || item.video_path || item.path)} (${escapeHtml(fmtBytes(item.size || 0))})</option>
      `).join("") : `<option value="">该节点暂无服务器视频，请先推送视频</option>`;
      const config = h.stream_config || {};
      if (config.stream_url && !refs.streamUrlInput.dataset.userEdited) refs.streamUrlInput.value = config.stream_url;
      if (config.stream_output_mode) refs.streamOutputModeInput.value = config.stream_output_mode;
      if (config.adaptive_mode) refs.adaptiveModeInput.value = config.adaptive_mode;
      if (config.resolution) refs.resolutionInput.value = config.resolution;
      if (config.fps) refs.fpsInput.value = config.fps;
      if (config.video_bitrate) refs.videoBitrateInput.value = config.video_bitrate;
      if (config.audio_bitrate) refs.audioBitrateInput.value = config.audio_bitrate;
      if (config.preset && config.preset !== "copy") refs.presetInput.value = config.preset;
      if (config.keyframe_seconds) refs.keyframeInput.value = config.keyframe_seconds;
    }

    function streamPayload({ includeKey = true } = {}) {
      const payload = {
        node_id: selectedNodeId,
        stream_url: refs.streamUrlInput.value.trim(),
        stream_key: includeKey ? refs.streamKeyInput.value.trim() : "",
        video_path: refs.streamVideoSelect.value,
        copy_mode: refs.tuneBox.dataset.copyMode === "1",
        adaptive_mode: refs.adaptiveModeInput.value || "auto",
        stream_output_mode: refs.streamOutputModeInput.value || "direct",
        preset: refs.presetInput.value.trim() || "veryfast",
        video_bitrate: Number(refs.videoBitrateInput.value || 4500),
        audio_bitrate: Number(refs.audioBitrateInput.value || 192),
        fps: Number(refs.fpsInput.value || 30),
        resolution: refs.resolutionInput.value.trim() || "1280x720",
        keyframe_seconds: Number(refs.keyframeInput.value || 2),
      };
      if (payload.copy_mode) payload.preset = "copy";
      return payload;
    }

    function applyTuneRecommendation(data) {
      const recommendation = data?.recommendation || {};
      lastTuneRecommendation = data;
      if (typeof recommendation.copy_mode === "boolean") {
        // Copy mode is safe to pass through the backend even though this UI keeps controls simple.
        refs.tuneBox.dataset.copyMode = recommendation.copy_mode ? "1" : "0";
      }
      if (recommendation.preset && recommendation.preset !== "copy") refs.presetInput.value = recommendation.preset;
      if (recommendation.video_bitrate) refs.videoBitrateInput.value = recommendation.video_bitrate;
      if (recommendation.audio_bitrate) refs.audioBitrateInput.value = recommendation.audio_bitrate;
      if (recommendation.fps) refs.fpsInput.value = recommendation.fps;
      if (recommendation.resolution) refs.resolutionInput.value = recommendation.resolution;
      if (recommendation.keyframe_seconds) refs.keyframeInput.value = recommendation.keyframe_seconds;
    }

    function renderTuneRecommendation(data) {
      const recommendation = data?.recommendation || {};
      const bounds = data?.quality_bounds || {};
      const maxQuality = bounds.max_quality || recommendation || {};
      const minQuality = bounds.min_quality || {};
      const analysis = data?.analysis || {};
      const source = analysis.source || {};
      const reasons = [...(analysis.reasons || []), ...(analysis.warnings || [])];
      const fmtTarget = (target) => (
        target && Object.keys(target).length
          ? `${target.resolution || "--"} / ${target.fps || "--"}fps / ${target.video_bitrate || "--"}k / ${target.preset || "--"} / 关键帧 ${target.keyframe_seconds || "--"} 秒`
          : "--"
      );
      refs.tuneBox.textContent = [
        `智能评分：${analysis.score || "--"}/100`,
        `策略：${recommendation.strategy === "copy" ? "Copy passthrough" : "Transcode"}`,
        `最高稳定质量：${fmtTarget(maxQuality)}`,
        `最低保底质量：${fmtTarget(minQuality)}`,
        `当前启动建议：${fmtTarget(recommendation)}`,
        `环境：CPU ${analysis.cpu_percent?.toFixed ? analysis.cpu_percent.toFixed(0) : "--"}% / ${analysis.cpu_count || "--"} 核，内存可用 ${analysis.memory_available_mb || "--"} MB`,
        `运行：speed ${analysis.ffmpeg_speed ? analysis.ffmpeg_speed.toFixed(2) + "x" : "未知"}，当前码率 ${analysis.current_stream_bitrate_kbps ? analysis.current_stream_bitrate_kbps.toFixed(0) + " kbps" : "未知"}，网络预算 ${analysis.network_budget_kbps ? analysis.network_budget_kbps + " kbps" : "待开播后校正"}`,
        `源视频：${source.width || "--"}x${source.height || "--"} / ${source.fps ? source.fps.toFixed(0) + "fps" : "未知"}`,
        "",
        ...(reasons.length ? reasons : ["当前环境没有明显风险，推荐值偏保守。"]),
      ].join("\n");
    }

    async function refreshAll() {
      refs.refreshBtn.disabled = true;
      try {
        const [nodeResp, mediaResp] = await Promise.all([
          fetch("/api/nodes"),
          fetch("/api/media"),
        ]);
        nodes = await nodeResp.json();
        media = await mediaResp.json();
        renderNodes();
        renderMedia();
        renderStreamControls();
        log("状态已刷新");
      } finally {
        refs.refreshBtn.disabled = false;
      }
    }

    async function uploadMedia() {
      const file = refs.mediaInput.files[0];
      if (!file) {
        refs.uploadBox.textContent = "请先选择一个视频文件。";
        return;
      }
      refs.uploadBtn.disabled = true;
      refs.uploadBox.textContent = `正在上传到本地总控台：${file.name}`;
      try {
        const form = new FormData();
        form.append("file", file, file.name);
        const resp = await fetch("/api/media/upload", { method: "POST", body: form });
        const data = await resp.json();
        refs.uploadBox.textContent = JSON.stringify(data, null, 2);
        await refreshAll();
      } finally {
        refs.uploadBtn.disabled = false;
      }
    }

    async function checkUpdates() {
      refs.updateBox.textContent = "正在检查 GitHub...";
      const resp = await fetch("/api/github/check", { method: "POST" });
      const data = await resp.json();
      refs.updateBox.textContent = JSON.stringify(data, null, 2);
    }

    async function showPolicy() {
      refs.updateBox.textContent = "Loading upload policy...";
      const resp = await fetch("/api/policy");
      const data = await resp.json();
      refs.updateBox.textContent = JSON.stringify(data, null, 2);
    }

    async function showAudit() {
      refs.updateBox.textContent = "Loading push audit...";
      const resp = await fetch("/api/push-audit?limit=20");
      const data = await resp.json();
      refs.updateBox.textContent = JSON.stringify(data, null, 2);
    }

    async function pushSelectedMedia() {
      const node_ids = selectedNodeIds();
      const media_name = selectedMediaName();
      if (!node_ids.length || !media_name) {
        refs.uploadBox.textContent = "请选择至少一个节点和一个本地资源。";
        return;
      }
      refs.pushSelectedBtn.disabled = true;
      try {
        const resp = await fetch("/api/media/push", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ node_ids, media_name }),
        });
        refs.uploadBox.textContent = JSON.stringify(await resp.json(), null, 2);
      } finally {
        refs.pushSelectedBtn.disabled = false;
      }
    }

    async function previewTune() {
      const payload = streamPayload({ includeKey: false });
      if (!payload.node_id || !payload.video_path) {
        refs.tuneBox.textContent = "请先选择右侧节点，并选择该节点服务器视频。";
        return;
      }
      refs.previewTuneBtn.disabled = true;
      refs.tuneBox.textContent = "正在让节点分析 CPU / 内存 / 网络 / 视频源...";
      try {
        const data = await postNodeAction("/api/nodes/stream/recommend", payload);
        lastTuneRecommendation = data;
        if (data.ok) {
          renderTuneRecommendation(data);
        } else {
          refs.tuneBox.textContent = data.message || "智能调优失败";
        }
      } finally {
        refs.previewTuneBtn.disabled = false;
      }
    }

    function applyLastTune() {
      if (!lastTuneRecommendation?.ok) {
        refs.tuneBox.textContent = "还没有可应用的推荐参数，请先点“预览智能调优”。";
        return;
      }
      applyTuneRecommendation(lastTuneRecommendation);
      renderTuneRecommendation(lastTuneRecommendation);
      log("已应用智能调优推荐参数到开播表单");
    }

    async function smartStart() {
      const payload = streamPayload({ includeKey: true });
      const relayMode = payload.stream_output_mode === "local_relay";
      if (!payload.node_id || !payload.video_path || (!relayMode && !payload.stream_key)) {
        refs.tuneBox.textContent = relayMode
          ? "请先选择节点和服务器视频，并确认本地中继可用。"
          : "请先选择节点、服务器视频，并粘贴 YouTube 直播码。";
        return;
      }
      refs.smartStartBtn.disabled = true;
      refs.tuneBox.textContent = "正在启动 Smart Start：会在选中节点停止重复推流，并启动一个干净 FFmpeg。";
      try {
        if (!lastTuneRecommendation?.ok) {
          const tune = await postNodeAction("/api/nodes/stream/recommend", { ...payload, stream_key: "" });
          if (tune.ok) {
            applyTuneRecommendation(tune);
            renderTuneRecommendation(tune);
          }
        }
        const startPayload = streamPayload({ includeKey: true });
        if (lastTuneRecommendation?.recommendation) {
          Object.assign(startPayload, lastTuneRecommendation.recommendation);
        }
        const data = await postNodeAction("/api/nodes/stream/start", startPayload);
        refs.tuneBox.textContent = JSON.stringify({
          ok: data.ok,
          node_id: data.node_id,
          message: data.message,
          started_pid: data.result?.started_pid,
          duplicate_processes: data.result?.duplicate_processes,
        }, null, 2);
        await refreshAll();
      } finally {
        refs.smartStartBtn.disabled = false;
      }
    }

    async function postNodeAction(path, payload) {
      const resp = await fetch(path, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const data = await resp.json();
      refs.updateBox.textContent = JSON.stringify(data, null, 2);
      return data;
    }

    async function handleNodeAction(action, nodeId) {
      const node = nodes.find((item) => String(item.id) === String(nodeId));
      const nodeName = node?.name || nodeId;
      if (action === "restart-stream") {
        if (!confirm(`确认请求重启 ${nodeName} 的推流？\n\n保护规则：不会清空直播码；如果节点没有安全重启接口，总控台会拒绝执行。`)) {
          return;
        }
        log(`请求重启推流：${nodeName}`);
        const data = await postNodeAction("/api/nodes/restart-stream", { node_id: nodeId });
        log(data.ok ? `推流重启已执行：${nodeName}` : `推流重启被保护规则拦截：${data.message || nodeName}`);
        await refreshAll();
        return;
      }
      if (action === "reboot-vps") {
        const confirmText = `REBOOT ${nodeId}`;
        const typed = prompt(`重启 VPS 是危险操作。\n请输入 ${confirmText} 才会继续：`);
        if (typed !== confirmText) {
          log(`已取消重启 VPS：${nodeName}`);
          return;
        }
        log(`请求重启 VPS：${nodeName}`);
        const data = await postNodeAction("/api/nodes/reboot", { node_id: nodeId, confirm_text: typed });
        log(data.ok ? `VPS 重启已提交：${nodeName}` : `VPS 重启被保护规则拦截：${data.message || nodeName}`);
        await refreshAll();
      }
    }

    async function upgradeSelectedNodes() {
      const node_ids = selectedNodeIds();
      if (!node_ids.length) {
        refs.updateBox.textContent = "请选择至少一个节点。";
        return;
      }
      refs.upgradeSelectedBtn.disabled = true;
      try {
        const resp = await fetch("/api/nodes/upgrade", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ node_ids }),
        });
        refs.updateBox.textContent = JSON.stringify(await resp.json(), null, 2);
      } finally {
        refs.upgradeSelectedBtn.disabled = false;
      }
    }

    refs.nodeList.addEventListener("click", (event) => {
      const actionButton = event.target.closest("[data-node-action]");
      if (actionButton) {
        event.preventDefault();
        event.stopPropagation();
        handleNodeAction(actionButton.dataset.nodeAction, actionButton.dataset.nodeId);
        return;
      }
      if (event.target.closest("[data-node-check]")) {
        return;
      }
      const row = event.target.closest("[data-node-row]");
      if (!row) return;
      selectedNodeId = row.dataset.nodeId;
      renderNodes();
      renderStreamControls();
    });
    refs.refreshBtn.addEventListener("click", refreshAll);
    refs.uploadBtn.addEventListener("click", uploadMedia);
    refs.checkUpdatesBtn.addEventListener("click", checkUpdates);
    refs.policyBtn.addEventListener("click", showPolicy);
    refs.auditBtn.addEventListener("click", showAudit);
    refs.pushSelectedBtn.addEventListener("click", pushSelectedMedia);
    refs.previewTuneBtn.addEventListener("click", previewTune);
    refs.applyTuneBtn.addEventListener("click", applyLastTune);
    refs.smartStartBtn.addEventListener("click", smartStart);
    refs.streamUrlInput.addEventListener("input", () => { refs.streamUrlInput.dataset.userEdited = "1"; });
    [refs.presetInput, refs.videoBitrateInput, refs.audioBitrateInput, refs.fpsInput, refs.resolutionInput, refs.keyframeInput].forEach((el) => {
      el.addEventListener("input", () => { refs.tuneBox.dataset.copyMode = "0"; });
    });
    refs.upgradeSelectedBtn.addEventListener("click", upgradeSelectedNodes);
    refreshAll();
  </script>
</body>
</html>
"""


@APP.get("/")
def index():
    return HTML


def ensure_dirs() -> None:
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if not NODES_FILE.exists():
        example = CONFIG_DIR / "nodes.example.json"
        if example.exists():
            shutil.copyfile(example, NODES_FILE)


def load_nodes() -> list[dict[str, Any]]:
    ensure_dirs()
    try:
        data = json.loads(NODES_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []
    return [node for node in data if isinstance(node, dict)]


def node_by_id(node_id: str) -> dict[str, Any] | None:
    for node in load_nodes():
        if str(node.get("id")) == node_id:
            return node
    return None


def request_node_json(node: dict[str, Any], path: str, *, timeout: int = 6) -> dict[str, Any]:
    base_url = node_base_url(node)
    if not base_url:
        return {"ok": False, "message": "missing node base_url"}
    try:
        resp = requests.get(f"{base_url}{path}", timeout=timeout)
        try:
            data = resp.json()
        except ValueError:
            data = {"message": resp.text[:500]}
        data["ok"] = resp.ok and bool(data.get("ok", True))
        data.setdefault("status_code", resp.status_code)
        return data
    except Exception as exc:
        return {"ok": False, "message": str(exc)}


def node_base_url(node: dict[str, Any]) -> str:
    return str(node.get("base_url") or "").rstrip("/")


def post_node_json(node: dict[str, Any], path: str, payload: dict[str, Any], *, timeout: int = 15) -> dict[str, Any]:
    base_url = node_base_url(node)
    if not base_url:
        return {"ok": False, "message": "missing node base_url"}
    try:
        resp = requests.post(f"{base_url}{path}", json=payload, timeout=timeout)
        try:
            data = resp.json()
        except ValueError:
            data = {"message": resp.text[:500]}
        data["ok"] = resp.ok and bool(data.get("ok", False))
        data.setdefault("status_code", resp.status_code)
        return data
    except Exception as exc:
        return {"ok": False, "message": str(exc)}


def public_upload_summary(data: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in data.items() if key != "token"}


def upload_policy() -> dict[str, Any]:
    return {
        "name": UPLOAD_POLICY_NAME,
        "safety": {
            "token_storage": "memory-only",
            "public_window_ttl_seconds": NODE_PUBLIC_UPLOAD_TTL_SECONDS,
            "close_public_window_on_success": True,
            "close_public_window_on_failure": True,
            "cancel_partial_upload_on_failure": True,
            "min_free_after_upload_bytes": MIN_FREE_AFTER_UPLOAD_BYTES,
            "max_hub_upload_bytes": APP.config["MAX_CONTENT_LENGTH"],
        },
        "stability": {
            "chunk_retries": NODE_UPLOAD_RETRIES,
            "chunk_timeout_seconds": NODE_UPLOAD_TIMEOUT_SECONDS,
            "probe_before_public_upload": True,
            "probe_timeout_seconds": NODE_UPLOAD_PROBE_TIMEOUT_SECONDS,
            "public_to_internal_fallback": True,
            "preserve_chunk_size_on_fallback": True,
        },
        "speed": {
            "route_preference": "public-window, public-direct, internal",
            "internal_chunk_bytes": NODE_UPLOAD_CHUNK_BYTES,
            "public_chunk_bytes": NODE_PUBLIC_UPLOAD_CHUNK_BYTES,
            "probe_bytes": NODE_UPLOAD_PROBE_BYTES,
            "min_public_upload_bytes_per_second": MIN_PUBLIC_UPLOAD_BYTES_PER_SECOND,
        },
    }


def policy_brief() -> dict[str, Any]:
    policy = upload_policy()
    return {
        "name": policy["name"],
        "safety": "memory-only-secret/auto-close/cancel-partial/disk-guard",
        "stability": "probe/retry/public-to-internal-fallback",
        "speed": "public-first/probe-measured/chunked",
    }


def rotate_push_audit_log() -> None:
    if PUSH_AUDIT_LOG.exists() and PUSH_AUDIT_LOG.stat().st_size > PUSH_AUDIT_LOG_MAX_BYTES:
        PUSH_AUDIT_LOG.replace(PUSH_AUDIT_LOG.with_suffix(".jsonl.1"))


def append_push_audit(event: dict[str, Any]) -> None:
    ensure_dirs()
    rotate_push_audit_log()
    safe_event = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "policy": policy_brief(),
        **event,
    }
    with PUSH_AUDIT_LOG.open("a", encoding="utf-8") as audit_file:
        audit_file.write(json.dumps(safe_event, ensure_ascii=False, separators=(",", ":")) + "\n")


def recent_push_audit(limit: int = 50) -> list[dict[str, Any]]:
    ensure_dirs()
    if not PUSH_AUDIT_LOG.exists():
        return []
    lines = PUSH_AUDIT_LOG.read_text(encoding="utf-8").splitlines()[-max(1, min(limit, 200)):]
    events = []
    for line in lines:
        with suppress(Exception):
            events.append(json.loads(line))
    return events


def probe_upload_route(route: dict[str, Any]) -> dict[str, Any]:
    payload = b"0" * max(1, NODE_UPLOAD_PROBE_BYTES)
    started_at = time.time()
    try:
        resp = requests.post(
            f"{str(route['upload_base_url']).rstrip('/')}/api/upload-probe",
            data=payload,
            headers={**(route.get("headers") or {}), "Content-Type": "application/octet-stream"},
            timeout=NODE_UPLOAD_PROBE_TIMEOUT_SECONDS,
        )
        try:
            data = resp.json()
        except ValueError:
            data = {"message": resp.text[:500]}
        elapsed = max(0.001, time.time() - started_at)
        ok = resp.ok and bool(data.get("ok", False))
        bytes_per_second = int(len(payload) / elapsed)
        if ok and bytes_per_second < MIN_PUBLIC_UPLOAD_BYTES_PER_SECOND:
            ok = False
            data["message"] = (
                f"public probe too slow: {file_size_label(bytes_per_second)}/s "
                f"< {file_size_label(MIN_PUBLIC_UPLOAD_BYTES_PER_SECOND)}/s"
            )
        return {
            "ok": ok,
            "status_code": resp.status_code,
            "elapsed_seconds": round(elapsed, 3),
            "bytes_per_second": bytes_per_second,
            "rate_label": f"{file_size_label(bytes_per_second)}/s",
            "message": data.get("message") or ("probe ok" if ok else "probe failed"),
        }
    except Exception as exc:
        return {"ok": False, "message": str(exc)}


def make_internal_upload_route(node: dict[str, Any], warnings: list[str] | None = None) -> dict[str, Any]:
    base_url = node_base_url(node)
    return {
        "base_url": base_url,
        "upload_base_url": base_url,
        "route": "internal",
        "route_label": "internal",
        "token": "",
        "headers": {},
        "opened_public_window": False,
        "chunk_bytes": NODE_UPLOAD_CHUNK_BYTES,
        "public_status": {},
        "warnings": list(warnings or []),
        "decision_log": [],
        "last_heartbeat_at": 0.0,
        "probe": {"ok": True, "skipped": True, "message": "internal fallback"},
        "fallback_from": "",
    }


def select_node_upload_route(node: dict[str, Any]) -> dict[str, Any]:
    base_url = node_base_url(node)
    if not base_url:
        raise ValueError("missing node base_url")

    status = request_node_json(node, "/api/public-upload", timeout=10)
    route = make_internal_upload_route(node)
    route["public_status"] = public_upload_summary(status)
    if not status.get("ok"):
        route["warnings"].append(status.get("message") or "public upload status unavailable")
        route["decision_log"].append("public upload status unavailable; using internal route")
        return route

    public_origin = str(status.get("public_origin") or "").rstrip("/")
    restrict_public = bool(status.get("restrict_public_to_upload"))
    supports_window = bool(status.get("supported"))
    route["decision_log"].append(
        f"public status ok; supported={supports_window}; restricted={restrict_public}; origin={public_origin or '-'}"
    )

    if supports_window:
        opened = post_node_json(
            node,
            "/api/public-upload/open",
            {
                "ttl_seconds": NODE_PUBLIC_UPLOAD_TTL_SECONDS,
                "mode": "auto",
                "reason": "stream-control-hub-media-push",
            },
            timeout=20,
        )
        if opened.get("ok"):
            token = str(opened.get("token") or "")
            opened_origin = str(opened.get("public_origin") or public_origin).rstrip("/")
            if opened_origin:
                route["decision_log"].append("public window opened; probing public route")
                route.update({
                    "upload_base_url": opened_origin,
                    "route": "public-window",
                    "route_label": "public window",
                    "token": token,
                    "headers": {"X-Public-Upload-Token": token} if token else {},
                    "opened_public_window": True,
                    "chunk_bytes": NODE_PUBLIC_UPLOAD_CHUNK_BYTES,
                    "public_status": public_upload_summary(opened),
                    "last_heartbeat_at": time.time(),
                })
                probe = probe_upload_route(route)
                route["probe"] = probe
                if probe.get("ok"):
                    route["decision_log"].append(
                        f"public probe ok at {probe.get('rate_label')}; using public window route"
                    )
                    return route
                route["warnings"].append(probe.get("message") or "public upload probe failed")
                route["decision_log"].append("public probe failed; closing public window and using internal route")
                close_node_public_upload(node, route, reason="stream-control-hub-public-probe-failed")
                fallback_route = make_internal_upload_route(node, route["warnings"])
                fallback_route["decision_log"] = [*route.get("decision_log", []), "internal fallback selected after failed public probe"]
                return fallback_route
        route["warnings"].append(opened.get("message") or "failed to open public upload window")
        route["decision_log"].append("failed to open public window; considering direct public or internal route")

    if public_origin and not restrict_public:
        route["decision_log"].append("public origin is unrestricted; probing direct public route")
        route.update({
            "upload_base_url": public_origin,
            "route": "public-direct",
            "route_label": "public direct",
            "chunk_bytes": NODE_PUBLIC_UPLOAD_CHUNK_BYTES,
        })
        probe = probe_upload_route(route)
        route["probe"] = probe
        if not probe.get("ok"):
            route["warnings"].append(probe.get("message") or "public direct probe failed")
            route["decision_log"].append("direct public probe failed; using internal route")
            fallback_route = make_internal_upload_route(node, route["warnings"])
            fallback_route["decision_log"] = [*route.get("decision_log", []), "internal fallback selected after failed direct probe"]
            return fallback_route
        route["decision_log"].append(f"direct public probe ok at {probe.get('rate_label')}; using direct public route")
    else:
        route["decision_log"].append("no usable public route selected; using internal route")
    return route


def route_summary(route: dict[str, Any]) -> dict[str, Any]:
    return {
        "route": route.get("route"),
        "route_label": route.get("route_label"),
        "upload_base_url": route.get("upload_base_url"),
        "opened_public_window": bool(route.get("opened_public_window")),
        "chunk_bytes": route.get("chunk_bytes"),
        "warnings": route.get("warnings") or [],
        "decision_log": route.get("decision_log") or [],
        "probe": route.get("probe") or {},
        "fallback_from": route.get("fallback_from") or "",
    }


def touch_node_public_upload(node: dict[str, Any], route: dict[str, Any]) -> None:
    if not route.get("opened_public_window") or not route.get("token"):
        return
    now = time.time()
    if now - float(route.get("last_heartbeat_at") or 0) < 30:
        return
    result = post_node_json(
        node,
        "/api/public-upload/heartbeat",
        {
            "ttl_seconds": NODE_PUBLIC_UPLOAD_TTL_SECONDS,
            "reason": "stream-control-hub-media-push-heartbeat",
            "token": route.get("token"),
        },
        timeout=15,
    )
    if not result.get("ok"):
        raise RuntimeError(result.get("message") or "failed to refresh public upload window")
    route["last_heartbeat_at"] = now


def close_node_public_upload(node: dict[str, Any], route: dict[str, Any], *, reason: str) -> dict[str, Any]:
    if not route.get("opened_public_window"):
        return {"ok": True, "skipped": True}
    result = post_node_json(
        node,
        "/api/public-upload/close",
        {"release_auto": True, "reason": reason},
        timeout=20,
    )
    return public_upload_summary(result)


def cancel_node_upload(node: dict[str, Any], upload_id: str) -> dict[str, Any]:
    return post_node_json(node, "/api/upload-chunk/cancel", {"upload_id": upload_id}, timeout=30)


def should_fallback_to_internal(route: dict[str, Any]) -> bool:
    return route.get("route") in {"public-window", "public-direct"} and route.get("upload_base_url") != route.get("base_url")


def upload_chunk_with_retries(
    media_path: Path,
    route: dict[str, Any],
    *,
    upload_id: str,
    chunk_index: int,
    total_chunks: int,
    offset: int,
    total_size: int,
    chunk_size: int,
) -> dict[str, Any]:
    payload: dict[str, Any] | None = None
    for attempt in range(NODE_UPLOAD_RETRIES + 1):
        payload = upload_chunk_to_node(
            media_path,
            route,
            upload_id=upload_id,
            chunk_index=chunk_index,
            total_chunks=total_chunks,
            offset=offset,
            total_size=total_size,
            chunk_size=chunk_size,
        )
        if payload.get("ok"):
            break
        if attempt < NODE_UPLOAD_RETRIES:
            time.sleep(min(5, 0.8 * (attempt + 1)))
    return payload or {}


def upload_chunk_to_node(
    media_path: Path,
    route: dict[str, Any],
    *,
    upload_id: str,
    chunk_index: int,
    total_chunks: int,
    offset: int,
    total_size: int,
    chunk_size: int,
) -> dict[str, Any]:
    with media_path.open("rb") as stream:
        stream.seek(offset)
        chunk_bytes = stream.read(min(chunk_size, total_size - offset))

    data = {
        "upload_id": upload_id,
        "filename": media_path.name,
        "chunk_index": str(chunk_index),
        "total_chunks": str(total_chunks),
        "offset": str(offset),
        "total_size": str(total_size),
        "chunk_size": str(chunk_size),
    }
    files = {"chunk": (media_path.name, chunk_bytes, "application/octet-stream")}
    try:
        resp = requests.post(
            f"{str(route['upload_base_url']).rstrip('/')}/api/upload-chunk",
            data=data,
            files=files,
            headers=route.get("headers") or {},
            timeout=NODE_UPLOAD_TIMEOUT_SECONDS,
        )
        try:
            payload = resp.json()
        except ValueError:
            payload = {"message": resp.text[:500]}
        payload.setdefault("status_code", resp.status_code)
        payload["ok"] = resp.ok and bool(payload.get("ok", False))
        return payload
    except Exception as exc:
        return {"ok": False, "message": str(exc)}


def push_media_to_node(node: dict[str, Any], media_path: Path) -> dict[str, Any]:
    node_id = str(node.get("id") or "")
    upload_id = f"hub_{uuid.uuid4().hex}"
    total_size = media_path.stat().st_size
    if total_size <= 0:
        return {"node_id": node_id, "ok": False, "message": "media file is empty"}

    started_at = time.time()
    route: dict[str, Any] | None = None
    last_payload: dict[str, Any] = {}
    received_size = 0
    try:
        route = select_node_upload_route(node)
        chunk_size = int(route.get("chunk_bytes") or NODE_UPLOAD_CHUNK_BYTES)
        total_chunks = (total_size + chunk_size - 1) // chunk_size

        for chunk_index in range(total_chunks):
            offset = chunk_index * chunk_size
            touch_node_public_upload(node, route)
            last_payload = upload_chunk_with_retries(
                media_path,
                route,
                upload_id=upload_id,
                chunk_index=chunk_index,
                total_chunks=total_chunks,
                offset=offset,
                total_size=total_size,
                chunk_size=chunk_size,
            )
            if not last_payload.get("ok"):
                if should_fallback_to_internal(route):
                    fallback_message = last_payload.get("message") or f"chunk {chunk_index + 1} upload failed"
                    close_node_public_upload(node, route, reason="stream-control-hub-public-transfer-failed")
                    route = make_internal_upload_route(
                        node,
                        [
                            *(route.get("warnings") or []),
                            f"public route failed at chunk {chunk_index + 1}: {fallback_message}",
                        ],
                    )
                    route["fallback_from"] = "public"
                    route["decision_log"].append(
                        f"public transfer failed at chunk {chunk_index + 1}; switched to internal route"
                    )
                    # Keep the original chunk size and total_chunks so the node-side offset check stays consistent.
                    route["chunk_bytes"] = chunk_size
                    last_payload = upload_chunk_with_retries(
                        media_path,
                        route,
                        upload_id=upload_id,
                        chunk_index=chunk_index,
                        total_chunks=total_chunks,
                        offset=offset,
                        total_size=total_size,
                        chunk_size=chunk_size,
                    )
                if not last_payload.get("ok"):
                    raise RuntimeError(last_payload.get("message") or f"chunk {chunk_index + 1} upload failed")
            received_size = max(received_size, int(last_payload.get("received_size") or 0))

        if not last_payload.get("complete"):
            raise RuntimeError("node did not report upload completion")

        close_result = close_node_public_upload(node, route, reason="stream-control-hub-media-push-complete")
        elapsed = max(0.001, time.time() - started_at)
        audit_event = {
            "node_id": node_id,
            "ok": True,
            "media": media_path.name,
            "size": total_size,
            "received_size": received_size,
            "elapsed_seconds": round(elapsed, 2),
            "average_bytes_per_second": int(total_size / elapsed),
            "route": route_summary(route),
            "video_path": last_payload.get("video_path"),
            "close_public_window": close_result,
        }
        append_push_audit(audit_event)
        return {
            "node_id": node_id,
            "ok": True,
            "message": "media pushed to node",
            "policy": policy_brief(),
            "media": media_path.name,
            "size": total_size,
            "size_label": file_size_label(total_size),
            "received_size": received_size,
            "elapsed_seconds": round(elapsed, 2),
            "average_bytes_per_second": int(total_size / elapsed),
            "average_rate_label": f"{file_size_label(int(total_size / elapsed))}/s",
            "video_path": last_payload.get("video_path"),
            "route": route_summary(route),
            "close_public_window": close_result,
            "audit_recorded": True,
        }
    except Exception as exc:
        cleanup = cancel_node_upload(node, upload_id)
        close_result: dict[str, Any] = {"ok": True, "skipped": True}
        if route:
            with suppress(Exception):
                close_result = close_node_public_upload(node, route, reason="stream-control-hub-media-push-failed")
        failure_event = {
            "node_id": node_id,
            "ok": False,
            "message": str(exc),
            "media": media_path.name,
            "received_size": received_size,
            "last_response": public_upload_summary(last_payload),
            "route": route_summary(route) if route else None,
            "cleanup": public_upload_summary(cleanup),
            "close_public_window": close_result,
        }
        with suppress(Exception):
            append_push_audit(failure_event)
        return {
            "node_id": node_id,
            "ok": False,
            "message": str(exc),
            "policy": policy_brief(),
            "media": media_path.name,
            "received_size": received_size,
            "last_response": public_upload_summary(last_payload),
            "route": route_summary(route) if route else None,
            "cleanup": public_upload_summary(cleanup),
            "close_public_window": close_result,
            "audit_recorded": True,
        }


def media_allowed(filename: str) -> bool:
    return Path(filename).suffix.lower() in ALLOWED_MEDIA_EXTENSIONS


def file_size_label(size: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(size)
    index = 0
    while value >= 1024 and index < len(units) - 1:
        value /= 1024
        index += 1
    return f"{value:.1f} {units[index]}" if index else f"{int(value)} B"


def ensure_media_disk_space(incoming_size: int) -> None:
    if incoming_size <= 0:
        return
    usage = shutil.disk_usage(MEDIA_DIR)
    required_free = incoming_size + max(MIN_FREE_AFTER_UPLOAD_BYTES, int(incoming_size * 0.1))
    if usage.free < required_free:
        raise RuntimeError(
            f"not enough disk space: need {file_size_label(required_free)}, free {file_size_label(usage.free)}"
        )


def list_media() -> list[dict[str, Any]]:
    ensure_dirs()
    items = []
    for path in sorted(MEDIA_DIR.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if not path.is_file() or path.suffix.lower() not in ALLOWED_MEDIA_EXTENSIONS:
            continue
        stat = path.stat()
        items.append({
            "name": path.name,
            "size": stat.st_size,
            "size_label": file_size_label(stat.st_size),
            "modified": stat.st_mtime,
            "modified_label": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stat.st_mtime)),
        })
    return items


@APP.get("/api/nodes")
def api_nodes():
    result = []
    for node in load_nodes():
        node_view = dict(node)
        node_view["health"] = request_node_json(node, "/api/status") if node.get("enabled", True) else {"ok": False}
        result.append(node_view)
    return jsonify(result)


@APP.get("/api/media")
def api_media():
    return jsonify(list_media())


@APP.get("/api/policy")
def api_policy():
    return jsonify({
        "ok": True,
        "policy": upload_policy(),
    })


@APP.get("/api/push-audit")
def api_push_audit():
    try:
        limit = int(request.args.get("limit") or 50)
    except ValueError:
        limit = 50
    return jsonify({
        "ok": True,
        "events": recent_push_audit(limit),
    })


@APP.post("/api/media/upload")
def api_media_upload():
    ensure_dirs()
    upload = request.files.get("file")
    if not upload or not upload.filename:
        return jsonify({"ok": False, "message": "missing file"}), 400
    if not media_allowed(upload.filename):
        return jsonify({"ok": False, "message": "unsupported media extension"}), 400
    incoming_size = int(request.content_length or 0)
    if incoming_size > 0:
        try:
            ensure_media_disk_space(incoming_size)
        except RuntimeError as exc:
            return jsonify({"ok": False, "message": str(exc)}), 507
    name = secure_filename(upload.filename)
    target = MEDIA_DIR / name
    counter = 1
    while target.exists():
        target = MEDIA_DIR / f"{Path(name).stem}-{counter}{Path(name).suffix}"
        counter += 1
    upload.save(target)
    return jsonify({"ok": True, "media": target.name, "size": target.stat().st_size})


@APP.post("/api/media/push")
def api_media_push():
    payload = request.get_json(silent=True) or {}
    node_ids = [str(item) for item in payload.get("node_ids") or []]
    media_name = secure_filename(str(payload.get("media_name") or ""))
    media_path = MEDIA_DIR / media_name
    if not node_ids:
        return jsonify({"ok": False, "message": "no nodes selected"}), 400
    if not media_path.exists():
        return jsonify({"ok": False, "message": "media not found"}), 404
    if not media_path.is_file() or not media_allowed(media_path.name):
        return jsonify({"ok": False, "message": "unsupported media file"}), 400

    results = []
    for node_id in node_ids:
        node = node_by_id(node_id)
        if not node:
            results.append({"node_id": node_id, "ok": False, "message": "node not found"})
            continue
        if not node.get("enabled", True):
            results.append({"node_id": node_id, "ok": False, "message": "node disabled"})
            continue
        results.append(push_media_to_node(node, media_path))
    return jsonify({
        "ok": all(item.get("ok") for item in results) if results else False,
        "media": media_name,
        "results": results,
    })


def stream_payload_for_node(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "stream_url": str(payload.get("stream_url") or "rtmp://a.rtmp.youtube.com/live2").strip().rstrip("/"),
        "stream_key": str(payload.get("stream_key") or "").strip(),
        "video_path": str(payload.get("video_path") or "").strip(),
        "copy_mode": bool(payload.get("copy_mode")),
        "adaptive_mode": str(payload.get("adaptive_mode") or "auto").strip().lower() or "auto",
        "stream_output_mode": str(payload.get("stream_output_mode") or "direct").strip().lower() or "direct",
        "preset": str(payload.get("preset") or "veryfast").strip() or "veryfast",
        "video_bitrate": int(payload.get("video_bitrate") or 4500),
        "audio_bitrate": int(payload.get("audio_bitrate") or 192),
        "fps": int(payload.get("fps") or 30),
        "resolution": str(payload.get("resolution") or "1280x720").strip() or "1280x720",
        "keyframe_seconds": int(payload.get("keyframe_seconds") or 2),
    }


def redacted_stream_result(data: dict[str, Any]) -> dict[str, Any]:
    result = dict(data or {})
    result.pop("command", None)
    if isinstance(result.get("result"), dict):
        nested = dict(result["result"])
        nested.pop("command", None)
        result["result"] = nested
    return result


@APP.post("/api/nodes/stream/recommend")
def api_nodes_stream_recommend():
    payload = request.get_json(silent=True) or {}
    node_id = str(payload.get("node_id") or "")
    node = node_by_id(node_id)
    if not node:
        return jsonify({"ok": False, "message": "node not found"}), 404
    if not node.get("enabled", True):
        return jsonify({"ok": False, "message": "node disabled"}), 409
    node_payload = stream_payload_for_node(payload)
    node_payload["stream_key"] = ""
    result = post_node_json(node, "/api/stream/recommend", node_payload, timeout=45)
    status_code = 200 if result.get("ok") else 502
    return jsonify({"node_id": node_id, **redacted_stream_result(result)}), status_code


@APP.post("/api/nodes/stream/start")
def api_nodes_stream_start():
    payload = request.get_json(silent=True) or {}
    node_id = str(payload.get("node_id") or "")
    node = node_by_id(node_id)
    if not node:
        return jsonify({"ok": False, "message": "node not found"}), 404
    if not node.get("enabled", True):
        return jsonify({"ok": False, "message": "node disabled"}), 409
    node_payload = stream_payload_for_node(payload)
    if not node_payload["video_path"]:
        return jsonify({"ok": False, "message": "missing node video_path"}), 400
    if node_payload["stream_output_mode"] != "local_relay" and not node_payload["stream_key"]:
        return jsonify({"ok": False, "message": "missing stream key"}), 400
    result = post_node_json(node, "/api/start-stream", node_payload, timeout=60)
    status_code = 200 if result.get("ok") else 502
    return jsonify({
        "node_id": node_id,
        **redacted_stream_result(result),
    }), status_code


@APP.post("/api/nodes/restart-stream")
def api_nodes_restart_stream():
    payload = request.get_json(silent=True) or {}
    node_id = str(payload.get("node_id") or "")
    node = node_by_id(node_id)
    if not node:
        return jsonify({"ok": False, "message": "node not found"}), 404
    if not node.get("enabled", True):
        return jsonify({"ok": False, "message": "node disabled"}), 409

    status = request_node_json(node, "/api/status", timeout=10)
    if not status.get("ok"):
        return jsonify({
            "ok": False,
            "node_id": node_id,
            "message": status.get("message") or "node health check failed",
            "status": status,
        }), 502

    stream_config = status.get("stream_config") or {}
    if not stream_config.get("has_stream_key"):
        return jsonify({
            "ok": False,
            "node_id": node_id,
            "message": "node has no saved stream key; blocked to avoid starting a broken stream",
        }), 409

    restart_api = str(node.get("restart_stream_api") or "").strip()
    if restart_api:
        result = post_node_json(node, restart_api, {}, timeout=30)
        return jsonify({
            "ok": bool(result.get("ok")),
            "node_id": node_id,
            "message": result.get("message") or ("restart request accepted" if result.get("ok") else "restart request failed"),
            "result": result,
        }), 200 if result.get("ok") else 502

    return jsonify({
        "ok": False,
        "node_id": node_id,
        "message": (
            "node does not expose a safe restart-stream API yet; blocked to protect current stream config. "
            "Configure restart_stream_api on this node when the node agent supports cached restart."
        ),
    }), 501


@APP.post("/api/nodes/reboot")
def api_nodes_reboot():
    payload = request.get_json(silent=True) or {}
    node_id = str(payload.get("node_id") or "")
    confirm_text = str(payload.get("confirm_text") or "")
    node = node_by_id(node_id)
    if not node:
        return jsonify({"ok": False, "message": "node not found"}), 404
    expected = f"REBOOT {node_id}"
    if confirm_text != expected:
        return jsonify({"ok": False, "message": f"confirmation required: {expected}"}), 400
    if not bool(node.get("allow_vps_reboot") or node.get("reboot_enabled")):
        return jsonify({
            "ok": False,
            "node_id": node_id,
            "message": "VPS reboot is disabled for this node; set allow_vps_reboot only after secure transport is configured",
        }), 403

    reboot_api = str(node.get("reboot_api") or "").strip()
    if reboot_api:
        result = post_node_json(node, reboot_api, {"confirm_text": confirm_text}, timeout=15)
        return jsonify({
            "ok": bool(result.get("ok")),
            "node_id": node_id,
            "message": result.get("message") or ("reboot request accepted" if result.get("ok") else "reboot request failed"),
            "result": result,
        }), 200 if result.get("ok") else 502

    return jsonify({
        "ok": False,
        "node_id": node_id,
        "message": "secure reboot transport is not configured; blocked by protection policy",
    }), 501


def run_git(args: list[str], cwd: Path | None = None, timeout: int = 60) -> dict[str, Any]:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd else None,
        text=True,
        capture_output=True,
        timeout=timeout,
    )
    return {
        "ok": proc.returncode == 0,
        "exit_code": proc.returncode,
        "stdout": proc.stdout.strip(),
        "stderr": proc.stderr.strip(),
    }


@APP.post("/api/github/check")
def api_github_check():
    ensure_dirs()
    repo_dir = WORK_DIR / "istanbul-stream-dashboard"
    if not repo_dir.exists():
        clone = run_git(["clone", "--branch", SOURCE_BRANCH, "--depth", "1", SOURCE_REPO, str(repo_dir)], timeout=180)
        if not clone["ok"]:
            return jsonify({"ok": False, "step": "clone", **clone}), 500
    fetch = run_git(["fetch", "origin", SOURCE_BRANCH], cwd=repo_dir, timeout=120)
    local = run_git(["rev-parse", "HEAD"], cwd=repo_dir)
    remote = run_git(["rev-parse", f"origin/{SOURCE_BRANCH}"], cwd=repo_dir)
    diff = run_git(["diff", "--stat", "HEAD", f"origin/{SOURCE_BRANCH}"], cwd=repo_dir)
    return jsonify({
        "ok": fetch["ok"] and local["ok"] and remote["ok"],
        "repo": SOURCE_REPO,
        "branch": SOURCE_BRANCH,
        "local": local.get("stdout"),
        "remote": remote.get("stdout"),
        "has_updates": local.get("stdout") != remote.get("stdout"),
        "diff_stat": diff.get("stdout"),
        "fetch": fetch,
    })


@APP.post("/api/nodes/upgrade")
def api_nodes_upgrade():
    payload = request.get_json(silent=True) or {}
    node_ids = [str(item) for item in payload.get("node_ids") or []]
    plans = []
    for node_id in node_ids:
        node = node_by_id(node_id)
        if not node:
            plans.append({"node_id": node_id, "ok": False, "message": "node not found"})
            continue
        plans.append({
            "node_id": node_id,
            "ok": False,
            "message": "upgrade transport not configured yet; design keeps FFmpeg untouched and restarts panel only",
            "target": str(node.get("base_url") or ""),
        })
    return jsonify({"ok": True, "plans": plans})


def main() -> None:
    ensure_dirs()
    APP.run(host=os.environ.get("STREAM_HUB_HOST", "127.0.0.1"), port=PORT, threaded=True)
