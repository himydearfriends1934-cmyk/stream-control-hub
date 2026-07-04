from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import time
import uuid
import hmac
import ipaddress
import threading
from contextlib import suppress
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
from flask import Flask, jsonify, request
from werkzeug.utils import secure_filename


ROOT = Path(__file__).resolve().parents[1]


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


load_env_file(ROOT / ".env")
CONFIG_DIR = ROOT / "config"
DATA_DIR = Path(os.environ.get("STREAM_HUB_DATA_DIR", str(ROOT / "data")))
MEDIA_DIR = DATA_DIR / "media"
WORK_DIR = DATA_DIR / "work"
NODES_FILE = Path(os.environ.get("STREAM_HUB_NODES_FILE", str(CONFIG_DIR / "nodes.json")))
PORT = int(os.environ.get("STREAM_HUB_PORT", "8788"))
SOURCE_REPO = os.environ.get(
    "STREAM_HUB_SOURCE_REPO",
    "https://github.com/himydearfriends1934-cmyk/stream-control-hub.git",
)
SOURCE_BRANCH = os.environ.get("STREAM_HUB_SOURCE_BRANCH", "main")
ALLOWED_MEDIA_EXTENSIONS = {".mp4", ".mov", ".mkv", ".m4v", ".webm"}
NODE_UPLOAD_CHUNK_BYTES = int(os.environ.get("STREAM_HUB_NODE_UPLOAD_CHUNK_BYTES", str(8 * 1024 ** 2)))
NODE_PUBLIC_UPLOAD_CHUNK_BYTES = int(os.environ.get("STREAM_HUB_NODE_PUBLIC_UPLOAD_CHUNK_BYTES", str(16 * 1024 ** 2)))
DIRECT_AGENT_UPLOAD_CHUNK_BYTES = int(os.environ.get("STREAM_HUB_DIRECT_AGENT_UPLOAD_CHUNK_BYTES", str(8 * 1024 ** 2)))
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
CONTROL_TOKEN = os.environ.get("STREAM_HUB_CONTROL_TOKEN", "").strip()
TRUSTED_REMOTE_WRITES = os.environ.get("STREAM_HUB_TRUSTED_REMOTE_WRITES", "").strip().lower() in {"1", "true", "yes"}
TAILSCALE_CGNAT = ipaddress.ip_network("100.64.0.0/10")
TAILSCALE_HELPER = ROOT / "scripts" / "tailscale-install.sh"
SHARE_TASKS: dict[str, dict[str, Any]] = {}
SHARE_TASKS_LOCK = threading.Lock()

APP = Flask(__name__)
APP.config["MAX_CONTENT_LENGTH"] = int(os.environ.get("STREAM_HUB_MAX_UPLOAD_BYTES", str(200 * 1024 ** 3)))


def local_git_version() -> str:
    result = run_command(["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"], timeout=5)
    return str(result.get("stdout") or "unmanaged").strip() or "unmanaged"


def service_active(name: str) -> bool:
    result = run_command(["systemctl", "is-active", "--quiet", name], timeout=5)
    return bool(result.get("ok"))


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
    .wrap { max-width: 1680px; margin: 0 auto; padding: 12px; }
    .hero {
      border: 1px solid var(--line);
      border-radius: 14px;
      padding: 12px 14px;
      background: rgba(19, 32, 28, 0.88);
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 12px;
      align-items: center;
    }
    h1 { margin: 0; font-size: 26px; letter-spacing: 0; }
    p { color: var(--muted); margin: 5px 0 0; line-height: 1.45; }
    .grid { display: grid; grid-template-columns: minmax(520px, 0.9fr) minmax(620px, 1.1fr); gap: 12px; margin-top: 10px; align-items: start; }
    .side-stack { display: grid; gap: 10px; align-content: start; }
    .bottom-section { grid-column: 1 / -1; display: grid; grid-template-columns: 0.9fr 0.9fr 1.15fr 1.35fr; gap: 10px; }
    .card {
      border: 1px solid var(--line);
      border-radius: 12px;
      padding: 10px;
      background: rgba(19, 32, 28, 0.9);
      box-shadow: 0 18px 60px rgba(0,0,0,0.18);
    }
    .card h2 { margin: 0 0 8px; font-size: 16px; }
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
    .media-list, .log { display: grid; gap: 8px; }
    .node, .media {
      display: grid;
      grid-template-columns: auto 1fr auto;
      gap: 10px;
      align-items: center;
      padding: 8px;
      border-radius: 10px;
      border: 1px solid rgba(49, 89, 76, 0.8);
      background: rgba(25, 43, 37, 0.78);
    }
    .media-name { min-width: 0; }
    .media-name strong { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .media-actions { display: flex; gap: 6px; flex-wrap: wrap; justify-content: flex-end; }
    .media-toolbar {
      display: flex;
      justify-content: space-between;
      gap: 8px;
      align-items: center;
      color: var(--muted);
      font-size: 12px;
      font-weight: 800;
    }
    .media.current-agent { border-color: rgba(54, 211, 153, 0.85); }
    .media-window {
      border: 1px solid rgba(49, 89, 76, 0.85);
      border-radius: 8px;
      max-height: 400px;
      overflow: auto;
      background: rgba(7, 18, 14, 0.66);
    }
    .media-window-head,
    .media-file-row {
      display: grid;
      grid-template-columns: minmax(150px, 1.35fr) 82px 132px minmax(92px, 0.75fr);
      gap: 8px;
      align-items: center;
    }
    .media-window-head {
      position: sticky;
      top: 0;
      z-index: 1;
      padding: 7px 9px;
      color: var(--muted);
      font-size: 12px;
      font-weight: 900;
      background: rgba(19, 32, 28, 0.98);
      border-bottom: 1px solid rgba(49, 89, 76, 0.7);
    }
    .media-file-row {
      width: 100%;
      padding: 7px 9px;
      border: 0;
      border-bottom: 1px solid rgba(49, 89, 76, 0.42);
      border-radius: 0;
      background: transparent;
      color: var(--text);
      cursor: pointer;
      text-align: left;
      font-weight: 700;
    }
    .media-file-row:hover,
    .media-file-row.selected {
      background: rgba(54, 211, 153, 0.1);
    }
    .media-file-row.current-agent {
      box-shadow: inset 3px 0 0 rgba(54, 211, 153, 0.9);
    }
    .media-file-row span {
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      min-width: 0;
    }
    .media-file-row .muted { color: var(--muted); font-size: 12px; }
    .media-context-menu {
      position: fixed;
      z-index: 100;
      min-width: 150px;
      display: none;
      padding: 6px;
      border: 1px solid rgba(49, 89, 76, 0.9);
      border-radius: 8px;
      background: #10201b;
      box-shadow: 0 18px 40px rgba(0,0,0,0.35);
    }
    .media-context-menu.open { display: grid; gap: 4px; }
    .media-context-menu button {
      width: 100%;
      padding: 8px 9px;
      border-radius: 7px;
      text-align: left;
      background: transparent;
      border: 0;
    }
    .media-context-menu button:hover { background: rgba(54, 211, 153, 0.12); }
    .media-context-menu button.danger:hover { background: rgba(251, 113, 133, 0.16); }
    .modal-backdrop {
      position: fixed;
      inset: 0;
      z-index: 90;
      display: none;
      align-items: center;
      justify-content: center;
      padding: 18px;
      background: rgba(1, 7, 5, 0.78);
    }
    .modal-backdrop.open { display: flex; }
    .wizard-modal {
      width: min(920px, 100%);
      max-height: min(760px, calc(100vh - 36px));
      display: grid;
      gap: 12px;
      overflow: auto;
      border: 1px solid rgba(54, 211, 153, 0.45);
      border-radius: 12px;
      padding: 14px;
      background: #0d1a16;
      box-shadow: 0 24px 80px rgba(0,0,0,0.45);
    }
    .wizard-head {
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: flex-start;
    }
    .wizard-head h2 { margin: 0 0 4px; }
    .wizard-head p { margin: 0; font-size: 13px; }
    .wizard-close { min-width: 42px; padding: 8px 10px; }
    .wizard-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
    .wizard-existing-grid { display: grid; grid-template-columns: minmax(150px, 1fr) minmax(170px, 1fr) auto; gap: 10px; align-items: end; }
    .wizard-field { display: grid; gap: 5px; min-width: 0; }
    .wizard-field label { color: var(--muted); font-size: 12px; font-weight: 900; }
    .wizard-step-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 8px; }
    .wizard-step {
      display: grid;
      gap: 5px;
      align-content: start;
      min-height: 92px;
      padding: 10px;
      border: 1px solid rgba(49, 89, 76, 0.78);
      border-radius: 10px;
      background: rgba(7, 18, 14, 0.58);
    }
    .wizard-step strong { font-size: 13px; }
    .wizard-step small { color: var(--muted); line-height: 1.35; }
    .wizard-step.done { border-color: rgba(54, 211, 153, 0.9); }
    .wizard-step.fail { border-color: rgba(251, 113, 133, 0.85); }
    .wizard-actions { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 8px; }
    .wizard-status {
      min-height: 128px;
      max-height: 240px;
      overflow: auto;
      display: grid;
      gap: 7px;
      padding: 12px;
      border-radius: 12px;
      border: 1px solid rgba(49, 89, 76, 0.75);
      background: rgba(7, 18, 14, 0.78);
      color: #d6fff0;
      line-height: 1.45;
    }
    .wizard-status-line { color: var(--muted); }
    .wizard-status-line strong { color: #effdf6; }
    .wizard-status-line.fail { color: #fecdd3; }
    .wizard-status-line.done { color: #b7f7dc; }
    .agent-compact,
    .network-compact {
      display: flex;
      flex-wrap: wrap;
      gap: 7px;
      align-items: center;
      margin: 0;
      padding: 6px 8px;
      border: 1px solid rgba(49, 89, 76, 0.7);
      border-radius: 10px;
      background: rgba(8, 17, 14, 0.38);
      color: var(--muted);
      font-size: 12px;
      font-weight: 800;
    }
    .agent-compact span,
    .network-compact span {
      display: inline-flex;
      min-height: 22px;
      align-items: center;
      padding: 2px 7px;
      border-radius: 999px;
      background: rgba(54, 211, 153, 0.08);
    }
    .agent-compact strong,
    .network-compact strong { color: #d6fff0; }
    .monitor-compact-row { display: grid; grid-template-columns: 0.95fr 1.2fr; gap: 8px; margin-bottom: 8px; }
    .network-compact .compact-title {
      background: transparent;
      color: #d6fff0;
      padding-left: 0;
      font-size: 13px;
    }
    .command-strip {
      margin-top: 10px;
      padding: 10px;
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
      margin-bottom: 8px;
    }
    .command-head h2 { margin: 0 0 3px; font-size: 17px; }
    .command-head p { margin: 0; font-size: 12px; }
    .command-grid {
      display: grid;
      grid-template-columns: minmax(150px, 1fr) minmax(190px, 1.05fr) minmax(250px, 1.35fr) minmax(210px, 1.1fr) 136px;
      gap: 8px;
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
      min-width: 0;
    }
    .command-actions button { padding: 8px 9px; }
    .command-advanced {
      grid-column: 1 / -1;
      margin-top: 2px;
      border: 1px solid rgba(49, 89, 76, 0.7);
      border-radius: 10px;
      background: rgba(7, 18, 14, 0.48);
    }
    .command-advanced summary {
      cursor: pointer;
      padding: 8px 10px;
      color: #d6fff0;
      font-size: 12px;
      font-weight: 900;
      list-style-position: inside;
    }
    .command-advanced[open] summary {
      border-bottom: 1px solid rgba(49, 89, 76, 0.55);
    }
    .command-advanced-grid {
      display: grid;
      grid-template-columns: 1.2fr 1fr 1.15fr 1fr 126px;
      gap: 8px;
      padding: 8px;
      align-items: end;
    }
    .tune-output {
      grid-column: 1 / -1;
      min-height: 54px;
      max-height: 96px;
      margin-top: 2px;
    }
    .monitor-card { min-height: 0; }
    .monitor-heading {
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: flex-start;
    }
    .monitor-heading p { margin: 0; font-size: 12px; }
    .node-monitor {
      min-height: 0;
      border-radius: 12px;
      padding: 10px;
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
      gap: 8px;
      align-items: start;
      margin-bottom: 8px;
      padding-bottom: 8px;
      border-bottom: 1px solid rgba(49, 89, 76, 0.55);
    }
    .monitor-hero h3 { margin: 0; font-size: 21px; letter-spacing: 0; }
    .monitor-hero small { color: var(--muted); display: block; margin-top: 3px; }
    .machine-compact {
      display: flex;
      flex-wrap: wrap;
      gap: 5px;
      margin-top: 6px;
    }
    .machine-compact span {
      display: inline-flex;
      min-height: 20px;
      align-items: center;
      padding: 2px 6px;
      border-radius: 999px;
      background: rgba(54, 211, 153, 0.08);
      color: var(--muted);
      font-size: 12px;
      font-weight: 800;
    }
    .machine-compact strong { color: #d6fff0; }
    .health-strip { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 7px; margin-bottom: 8px; }
    .health-donut {
      display: grid;
      grid-template-columns: 46px minmax(0, 1fr);
      gap: 7px;
      align-items: center;
      padding: 6px;
      border: 1px solid rgba(49, 89, 76, 0.75);
      border-radius: 10px;
      background: rgba(8, 17, 14, 0.38);
      min-width: 0;
    }
    .donut {
      width: 44px;
      height: 44px;
      border-radius: 50%;
      display: grid;
      place-items: center;
      background:
        radial-gradient(circle at center, #07110e 0 54%, transparent 55%),
        conic-gradient(var(--donut-color, var(--accent)) var(--value, 0%), rgba(255,255,255,0.08) 0);
      box-shadow: inset 0 0 14px rgba(0,0,0,0.24);
      font-size: 11px;
      font-weight: 900;
    }
    .donut-info small { color: var(--muted); display: block; font-size: 11px; }
    .donut-info strong { display: block; font-size: 14px; line-height: 1.15; margin-top: 2px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .monitor-panel-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
    .monitor-panel {
      border: 1px solid rgba(49, 89, 76, 0.75);
      border-radius: 10px;
      padding: 8px;
      background: rgba(9, 17, 14, 0.58);
    }
    .monitor-panel h4 { margin: 0 0 5px; font-size: 13px; color: #d6fff0; }
    .node-table-card { min-height: 0; }
    .node-table-toolbar {
      display: flex;
      justify-content: space-between;
      gap: 10px;
      align-items: center;
      margin-bottom: 8px;
    }
    .node-table-toolbar p { margin: 0; font-size: 13px; }
    .node-table {
      display: grid;
      gap: 6px;
      max-height: 320px;
      overflow: auto;
      padding-right: 3px;
    }
    .node-table-head,
    .node-row {
      display: grid;
      grid-template-columns: 22px minmax(130px, 1fr) 70px 76px minmax(250px, 1.15fr);
      gap: 6px;
      align-items: center;
    }
    .node-table-head {
      position: sticky;
      top: 0;
      z-index: 1;
      padding: 6px 8px;
      color: var(--muted);
      font-size: 12px;
      background: rgba(19, 32, 28, 0.96);
      border-bottom: 1px solid rgba(49, 89, 76, 0.55);
    }
    .node-row {
      min-height: 44px;
      padding: 6px 8px;
      border: 1px solid rgba(49, 89, 76, 0.75);
      border-radius: 10px;
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
    .dot { width: 14px; height: 14px; flex: 0 0 14px; border: 2px solid rgba(255,255,255,0.2); border-radius: 999px; background: #fbbf24; box-shadow: inset 0 0 3px rgba(255,255,255,0.5), 0 0 10px rgba(251, 191, 36, 0.65); }
    .dot.ok { background: #28e39f; box-shadow: inset 0 0 3px rgba(255,255,255,0.65), 0 0 12px rgba(40, 227, 159, 0.85); }
    .dot.off { background: #52615c; border-color: rgba(255,255,255,0.1); box-shadow: inset 0 0 3px rgba(0,0,0,0.55); }
    .dot.stream-live { background: #ff334f; box-shadow: inset 0 0 3px rgba(255,255,255,0.7), 0 0 13px rgba(255, 51, 79, 0.95); }
    .dot.stream-idle { background: #4a5551; border-color: rgba(255,255,255,0.08); box-shadow: inset 0 0 3px rgba(0,0,0,0.6); }
    .row-actions { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 3px; align-items: center; }
    .role-row .row-actions { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    .role-group + .role-group { margin-top: 12px; }
    .role-group-title { display: flex; justify-content: space-between; align-items: center; margin: 0 0 6px; color: #d6fff0; }
    .role-row.disabled-role { opacity: 0.58; border-style: dashed; }
    .role-row.disabled-role:hover { opacity: 0.82; }
    .row-actions button.tiny { min-width: 0; padding: 6px 4px; font-size: 10px; overflow: hidden; text-overflow: ellipsis; }
    .settings-button { min-width: 28px !important; font-size: 14px !important; line-height: 1; }
    .role-settings-modal { width: min(520px, 100%); }
    .role-settings-status { display: grid; gap: 8px; }
    .role-settings-item { display: grid; grid-template-columns: 1fr auto; gap: 10px; align-items: center; padding: 10px; border: 1px solid var(--line); border-radius: 10px; background: rgba(7, 18, 14, 0.58); }
    .role-settings-item small { display: block; margin-top: 3px; color: var(--muted); }
    .empty-state {
      min-height: 180px;
      display: grid;
      place-items: center;
      color: var(--muted);
      text-align: center;
      border: 1px dashed rgba(49, 89, 76, 0.8);
      border-radius: 10px;
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
    .metric-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 5px; }
    .metric {
      border: 1px solid rgba(49, 89, 76, 0.75);
      border-radius: 8px;
      padding: 6px;
      background: rgba(8, 17, 14, 0.35);
    }
    .metric small, .mini-table small { color: var(--muted); display: block; font-size: 12px; }
    .metric strong { display: block; font-size: 17px; margin-top: 2px; }
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
      gap: 7px;
      padding: 4px 0;
      border-bottom: 1px solid rgba(49, 89, 76, 0.4);
    }
    .mini-row:last-child { border-bottom: none; }
    .mono { font-family: "Cascadia Mono", "Consolas", monospace; word-break: break-word; }
    .compact-card { padding: 10px; }
    .log-card { display: grid; gap: 8px; }
    .log-card pre { min-height: 58px; max-height: 120px; }
    .node strong, .media strong { display: block; }
    .node small, .media small { color: var(--muted); }
    .resource-card { display: grid; gap: 8px; }
    .resource-card .split { grid-template-columns: 1fr; gap: 8px; }
    .resource-card .actions { display: grid; grid-template-columns: 1fr; }
    .resource-card .media-list { overflow: visible; padding-right: 0; }
    .transfer-box {
      border: 1px solid rgba(49, 89, 76, 0.85);
      border-radius: 8px;
      padding: 8px;
      background: rgba(7, 18, 14, 0.78);
      min-height: 106px;
      display: grid;
      gap: 7px;
    }
    .transfer-title { display: flex; align-items: center; justify-content: space-between; gap: 8px; font-weight: 900; }
    .progress-track { height: 10px; border-radius: 999px; background: rgba(255,255,255,0.1); overflow: hidden; }
    .progress-fill { width: var(--value, 0%); height: 100%; background: linear-gradient(90deg, var(--accent), var(--accent-2)); transition: width 0.25s ease; }
    .transfer-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
    .transfer-grid small { display: block; color: var(--muted); font-size: 11px; }
    .transfer-grid strong { display: block; margin-top: 2px; font-size: 15px; }
    .transfer-message { color: var(--muted); line-height: 1.45; word-break: break-word; }
    .transfer-box.fail { border-color: rgba(251, 113, 133, 0.75); }
    .transfer-box.done { border-color: rgba(54, 211, 153, 0.9); }
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
      min-height: 90px;
      max-height: 240px;
      overflow: auto;
      padding: 12px;
      border-radius: 12px;
      background: #09110e;
      border: 1px solid var(--line);
      color: #c9f7e7;
    }
    .split { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
    @media (max-width: 1080px) {
      .grid, .split, .hero, .node-detail, .bottom-section, .health-strip, .monitor-panel-grid, .command-grid, .command-advanced-grid, .monitor-compact-row { grid-template-columns: 1fr; }
      .bottom-section { grid-column: auto; }
      .monitor-card, .node-table-card { min-height: auto; }
      .node-monitor { min-height: 420px; }
      .node-table { max-height: none; }
      .node-table-head { display: none; }
      .node-row { grid-template-columns: 24px minmax(0, 1fr); }
      .node-state, .row-actions { grid-column: 2; }
      .media-window-head,
      .media-file-row { grid-template-columns: minmax(130px, 1.4fr) 74px 116px minmax(82px, 0.8fr); }
      .wizard-grid, .wizard-existing-grid, .wizard-step-grid, .wizard-actions { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <div class="wrap">
    <section class="hero">
      <div>
        <h1>Stream Control Hub</h1>
        <p>本地总控台：集中监控 VPS 推流节点，视频从浏览器直传 Agent，也可以在 Agent 之间共享。升级面板时不触碰正在运行的 FFmpeg 推流。</p>
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
          <p>和右侧 VPS 节点表联动：先核对目标节点，再选择手动直播码或 YouTube API、选视频、调优、开播。</p>
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
          <label>YouTube 目标</label>
          <div class="command-pair">
            <input id="streamKeyInput" type="password" autocomplete="off" placeholder="手动直播码">
            <select id="youtubeStreamSelect" disabled>
              <option value="">先连接 YouTube API</option>
            </select>
          </div>
        </div>
        <div class="command-field">
          <label>输出 / 自适应</label>
          <div class="command-pair">
            <select id="streamOutputModeInput">
              <option value="direct">直接推 YouTube</option>
              <option value="youtube_api">YouTube API</option>
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
        <details class="command-advanced" id="commandAdvanced">
          <summary>高级参数 / 调优输出</summary>
          <div class="command-advanced-grid">
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
        </details>
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
          <div class="role-group">
            <h3 class="role-group-title"><span>Agent 组</span><small>推流 / 媒体 / Agent 更新</small></h3>
            <div class="node-table" id="nodeList">加载中...</div>
          </div>
          <div class="role-group">
            <h3 class="role-group-title"><span>Hub 组</span><small>控制台 / Hub 更新 / 切换</small></h3>
            <div class="node-table" id="hubNodeList">加载中...</div>
          </div>
        </div>

        <div class="card resource-card">
          <h2>节点资源共享</h2>
          <div class="split">
            <div>
              <input id="mediaInput" type="file" accept=".mp4,.mov,.mkv,.m4v,.webm">
              <div class="actions" style="margin-top: 8px;">
                <button class="primary" id="uploadBtn">上传到当前 Agent</button>
                <button class="danger" id="cancelUploadBtn" disabled>取消上传</button>
              </div>
            </div>
            <div id="uploadBox" class="transfer-box"></div>
          </div>
          <div class="media-list" id="mediaList">加载中...</div>
          <div class="media-context-menu" id="mediaContextMenu">
            <button data-media-menu-action="inspect">查看详情</button>
            <button data-media-menu-action="use">选用开播</button>
            <button data-media-menu-action="share">共享到勾选 Agent</button>
            <button data-media-menu-action="rename">编辑名称</button>
            <button class="danger" data-media-menu-action="delete">删除文件</button>
          </div>
        </div>
      </div>

      <div class="bottom-section">
        <div class="card compact-card">
          <h2>GitHub 更新</h2>
          <p>低频维护功能放在底部，不占用节点监控主视野。</p>
          <div class="actions">
            <button id="checkUpdatesBtn">检查 GitHub 更新</button>
          </div>
        </div>
        <div class="card compact-card">
          <h2>YouTube API</h2>
          <p>在选中 Agent 上授权频道、创建直播并绑定可复用推流。</p>
          <div class="actions">
            <button class="primary" id="youtubeWizardBtn">打开 YouTube 向导</button>
          </div>
        </div>
        <div class="card compact-card">
          <h2>Tailscale 连接</h2>
          <p>打开向导，按检查、安装/修复、登录、验证四步接入 tailnet。</p>
          <div class="actions">
            <button class="primary" id="tailscaleWizardBtn">打开 Tailscale 向导</button>
            <button id="tailscaleStatusBtn">快速状态</button>
          </div>
        </div>
        <div class="card compact-card log-card">
          <h2>策略 / 审计 / 操作日志</h2>
          <pre id="updateBox">点击 Upload Policy 或 Push Audit 查看系统规则与最近推送记录。</pre>
          <pre id="logBox">就绪。</pre>
        </div>
      </div>
    </section>
  </div>

  <div class="modal-backdrop" id="roleSettingsModal" aria-hidden="true">
    <div class="wizard-modal role-settings-modal" role="dialog" aria-modal="true" aria-labelledby="roleSettingsTitle">
      <div class="wizard-head">
        <div>
          <h2 id="roleSettingsTitle">节点角色设置</h2>
          <p id="roleSettingsSummary">查看当前状态后选择需要执行的低频维护操作。</p>
        </div>
        <button class="wizard-close" id="roleSettingsClose" aria-label="关闭">×</button>
      </div>
      <div class="role-settings-status" id="roleSettingsActions"></div>
      <p>保护规则：点击操作后还会显示当前状态与影响范围，必须再次确认才会执行。</p>
    </div>
  </div>

  <div class="modal-backdrop" id="tailscaleWizardModal" aria-hidden="true">
    <div class="wizard-modal" role="dialog" aria-modal="true" aria-labelledby="tailscaleWizardTitle">
      <div class="wizard-head">
        <div>
          <h2 id="tailscaleWizardTitle">Tailscale 安装向导</h2>
          <p>按步骤检查环境、安装或修复 Tailscale、使用一次性 auth key 登录，然后验证 100.x 地址。</p>
        </div>
        <button class="wizard-close" id="tailscaleWizardClose" title="关闭">X</button>
      </div>
      <div class="wizard-grid">
        <div class="wizard-field">
          <label>一次性 auth key</label>
          <input id="tailscaleAuthInput" type="password" autocomplete="off" placeholder="tskey-auth-...">
        </div>
        <div class="wizard-field">
          <label>设备名称</label>
          <input id="tailscaleHostInput" type="text" value="stream-control-hub" placeholder="stream-control-hub">
        </div>
      </div>
      <div class="wizard-existing-grid">
        <div class="wizard-field">
          <label>目标 Agent</label>
          <select id="tailscaleNodeSelect"></select>
        </div>
        <div class="wizard-field">
          <label>已有 Tailscale IP</label>
          <input id="tailscaleExistingIpInput" type="text" autocomplete="off" placeholder="100.x.x.x">
        </div>
        <button id="tailscaleUseExistingIpBtn">验证并连接</button>
      </div>
      <div class="wizard-step-grid">
        <div class="wizard-step" data-tailscale-step="precheck">
          <strong>1. 环境检查</strong>
          <small>包管理器、权限、TUN、tailscale.com 连通性。</small>
        </div>
        <div class="wizard-step" data-tailscale-step="install">
          <strong>2. 安装 / 修复</strong>
          <small>缺失时安装 Tailscale，并启用 tailscaled。</small>
        </div>
        <div class="wizard-step" data-tailscale-step="connect">
          <strong>3. 登录连接</strong>
          <small>使用 auth key 执行 tailscale up。</small>
        </div>
        <div class="wizard-step" data-tailscale-step="verify">
          <strong>4. 验证状态</strong>
          <small>读取本机 100.x 地址和 tailnet peers。</small>
        </div>
      </div>
      <div class="wizard-actions">
        <button id="tailscalePrecheckBtn">1. 检查</button>
        <button id="tailscaleInstallBtn">2. 安装/修复</button>
        <button class="primary" id="tailscaleConnectBtn">3. 登录连接</button>
        <button id="tailscaleVerifyBtn">4. 验证</button>
      </div>
      <div class="wizard-status" id="tailscaleWizardLog">
        <div class="wizard-status-line">打开向导后，先点“1. 检查”。已有 Tailscale IP 的 Agent 可以直接填写 IP 后点“验证并连接”。</div>
      </div>
    </div>
  </div>

  <div class="modal-backdrop" id="youtubeWizardModal" aria-hidden="true">
    <div class="wizard-modal" role="dialog" aria-modal="true" aria-labelledby="youtubeWizardTitle">
      <div class="wizard-head">
        <div>
          <h2 id="youtubeWizardTitle">YouTube Live API</h2>
          <p>授权和直播码都留在当前 Agent；Hub 只保存 YouTube stream ID。</p>
        </div>
        <button class="wizard-close" id="youtubeWizardClose" title="关闭">X</button>
      </div>
      <div class="wizard-grid">
        <div class="wizard-field">
          <label>当前 Agent</label>
          <input id="youtubeNodeInput" type="text" readonly value="先选择 Agent">
        </div>
        <div class="wizard-field">
          <label>已有可复用直播流</label>
          <select id="youtubePrepareStreamSelect">
            <option value="">创建新的可复用直播流</option>
          </select>
        </div>
        <div class="wizard-field">
          <label>直播标题</label>
          <input id="youtubeTitleInput" type="text" maxlength="100" placeholder="直播标题">
        </div>
        <div class="wizard-field">
          <label>OAuth Client ID</label>
          <input id="youtubeClientIdInput" type="text" autocomplete="off" placeholder="Google TV / Limited Input Client ID">
        </div>
        <div class="wizard-field">
          <label>Client Secret（可选）</label>
          <input id="youtubeClientSecretInput" type="password" autocomplete="off" placeholder="部分 OAuth 客户端没有 secret">
        </div>
        <div class="wizard-field">
          <label>可见范围 / 计划时间</label>
          <div class="command-pair">
            <select id="youtubePrivacyInput">
              <option value="private">私享</option>
              <option value="unlisted">不公开</option>
              <option value="public">公开</option>
            </select>
            <input id="youtubeScheduleInput" type="datetime-local">
          </div>
        </div>
      </div>
      <div class="wizard-actions">
        <button id="youtubeRefreshBtn">检查 / 刷新</button>
        <button id="youtubeSaveConfigBtn">保存 API 配置</button>
        <button class="primary" id="youtubeAuthorizeBtn">连接 YouTube</button>
        <button id="youtubePrepareBtn">创建并绑定直播</button>
        <button class="danger" id="youtubeRevokeBtn">断开授权</button>
      </div>
      <div class="wizard-status" id="youtubeWizardLog">
        <div class="wizard-status-line">选择 Agent 后检查状态。首次使用需要在 Agent 的 .agent.env 配置 YOUTUBE_CLIENT_ID。</div>
      </div>
    </div>
  </div>

  <script>
    const TOKEN_FROM_URL = new URLSearchParams(window.location.search).get("token") || "";
    const CONTROL_TOKEN = TOKEN_FROM_URL || sessionStorage.getItem("streamHubControlToken") || localStorage.getItem("streamHubControlToken") || "";
    if (TOKEN_FROM_URL) {
      sessionStorage.setItem("streamHubControlToken", TOKEN_FROM_URL);
      const cleanUrl = new URL(window.location.href);
      cleanUrl.searchParams.delete("token");
      window.history.replaceState({}, document.title, cleanUrl.pathname + cleanUrl.search + cleanUrl.hash);
    }
    function authHeaders(extra = {}) {
      return CONTROL_TOKEN ? { ...extra, "X-Control-Token": CONTROL_TOKEN } : extra;
    }
    const refs = {
      nodeList: document.getElementById("nodeList"),
      hubNodeList: document.getElementById("hubNodeList"),
      roleSettingsModal: document.getElementById("roleSettingsModal"),
      roleSettingsTitle: document.getElementById("roleSettingsTitle"),
      roleSettingsSummary: document.getElementById("roleSettingsSummary"),
      roleSettingsActions: document.getElementById("roleSettingsActions"),
      roleSettingsClose: document.getElementById("roleSettingsClose"),
      nodeMonitor: document.getElementById("nodeMonitor"),
      mediaList: document.getElementById("mediaList"),
      mediaContextMenu: document.getElementById("mediaContextMenu"),
      refreshBtn: document.getElementById("refreshBtn"),
      checkUpdatesBtn: document.getElementById("checkUpdatesBtn"),
      policyBtn: document.getElementById("policyBtn"),
      auditBtn: document.getElementById("auditBtn"),
      tailscaleWizardBtn: document.getElementById("tailscaleWizardBtn"),
      tailscaleWizardModal: document.getElementById("tailscaleWizardModal"),
      tailscaleWizardClose: document.getElementById("tailscaleWizardClose"),
      tailscaleWizardLog: document.getElementById("tailscaleWizardLog"),
      tailscaleAuthInput: document.getElementById("tailscaleAuthInput"),
      tailscaleHostInput: document.getElementById("tailscaleHostInput"),
      tailscaleNodeSelect: document.getElementById("tailscaleNodeSelect"),
      tailscaleExistingIpInput: document.getElementById("tailscaleExistingIpInput"),
      tailscaleUseExistingIpBtn: document.getElementById("tailscaleUseExistingIpBtn"),
      tailscalePrecheckBtn: document.getElementById("tailscalePrecheckBtn"),
      tailscaleInstallBtn: document.getElementById("tailscaleInstallBtn"),
      tailscaleStatusBtn: document.getElementById("tailscaleStatusBtn"),
      tailscaleConnectBtn: document.getElementById("tailscaleConnectBtn"),
      tailscaleVerifyBtn: document.getElementById("tailscaleVerifyBtn"),
      mediaInput: document.getElementById("mediaInput"),
      uploadBtn: document.getElementById("uploadBtn"),
      pushSelectedBtn: document.getElementById("pushSelectedBtn"),
      streamNodeInput: document.getElementById("streamNodeInput"),
      streamNodeHint: document.getElementById("streamNodeHint"),
      streamVideoSelect: document.getElementById("streamVideoSelect"),
      streamKeyInput: document.getElementById("streamKeyInput"),
      youtubeStreamSelect: document.getElementById("youtubeStreamSelect"),
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
      commandAdvanced: document.getElementById("commandAdvanced"),
      tuneBox: document.getElementById("tuneBox"),
      youtubeWizardBtn: document.getElementById("youtubeWizardBtn"),
      youtubeWizardModal: document.getElementById("youtubeWizardModal"),
      youtubeWizardClose: document.getElementById("youtubeWizardClose"),
      youtubeWizardLog: document.getElementById("youtubeWizardLog"),
      youtubeNodeInput: document.getElementById("youtubeNodeInput"),
      youtubePrepareStreamSelect: document.getElementById("youtubePrepareStreamSelect"),
      youtubeTitleInput: document.getElementById("youtubeTitleInput"),
      youtubeClientIdInput: document.getElementById("youtubeClientIdInput"),
      youtubeClientSecretInput: document.getElementById("youtubeClientSecretInput"),
      youtubePrivacyInput: document.getElementById("youtubePrivacyInput"),
      youtubeScheduleInput: document.getElementById("youtubeScheduleInput"),
      youtubeRefreshBtn: document.getElementById("youtubeRefreshBtn"),
      youtubeSaveConfigBtn: document.getElementById("youtubeSaveConfigBtn"),
      youtubeAuthorizeBtn: document.getElementById("youtubeAuthorizeBtn"),
      youtubePrepareBtn: document.getElementById("youtubePrepareBtn"),
      youtubeRevokeBtn: document.getElementById("youtubeRevokeBtn"),
      updateBox: document.getElementById("updateBox"),
      uploadBox: document.getElementById("uploadBox"),
      logBox: document.getElementById("logBox"),
    };
    refs.cancelUploadBtn = document.getElementById("cancelUploadBtn");
    let nodes = [];
    const LAST_NODE_STORAGE_KEY = "streamHubLastSelectedNodeId";
    let selectedNodeId = localStorage.getItem(LAST_NODE_STORAGE_KEY) || "";
    function rememberSelectedNode(nodeId) {
      selectedNodeId = String(nodeId || "");
      if (selectedNodeId) localStorage.setItem(LAST_NODE_STORAGE_KEY, selectedNodeId);
      else localStorage.removeItem(LAST_NODE_STORAGE_KEY);
    }
    let lastTuneRecommendation = null;
    let activeUpload = null;
    let contextMediaRow = null;
    let youtubeOauthSession = "";
    let youtubeOauthPollTimer = null;

    renderTransfer({
      title: "传输状态",
      badge: "ready",
      message: "选择右侧当前 Agent 后，文件会从浏览器直接传到该 Agent；共享时由源 Agent 直接复制到目标 Agent。",
    });

    function selectedNodeIds() {
      return [...document.querySelectorAll("[data-node-check]:checked")].map((el) => el.value);
    }

    function selectedMediaName() {
      const checked = document.querySelector("[data-media-check]:checked");
      return checked ? checked.value : "";
    }

    function selectedMediaPath() {
      const checked = document.querySelector("[data-media-check]:checked");
      return checked ? (checked.dataset.videoPath || checked.value) : "";
    }

    function selectedMediaNodeId() {
      const checked = document.querySelector("[data-media-check]:checked");
      return checked ? (checked.dataset.nodeId || "") : "";
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
      return `<span class="dot ${ok ? "ok" : warn ? "" : "off"}"></span>`;
    }

    function streamDot(streaming) {
      return `<span class="dot ${streaming ? "stream-live" : "stream-idle"}"></span>`;
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

    function fmtDuration(seconds) {
      const value = Math.max(0, Number(seconds || 0));
      if (!Number.isFinite(value) || value <= 0) return "--";
      if (value < 60) return `${Math.ceil(value)} 秒`;
      const minutes = Math.floor(value / 60);
      const rest = Math.ceil(value % 60);
      if (minutes < 60) return `${minutes} 分 ${rest} 秒`;
      const hours = Math.floor(minutes / 60);
      return `${hours} 小时 ${minutes % 60} 分`;
    }

    function friendlyError(error, fallback = "操作失败") {
      if (!error) return fallback;
      const message = String(error.message || error.messageText || error || fallback);
      if (message.includes("Failed to fetch")) return "网络连接失败：浏览器无法连接到目标 Agent，请检查 Tailscale、Agent 服务和端口。";
      if (message.includes("cross-origin")) return "Hub 写入被跨域保护拦截，请刷新页面或确认通过 Tailscale/内网地址访问。";
      if (message.includes("unsupported media")) return "文件格式不支持，请使用 mp4、mov、mkv、m4v 或 webm。";
      if (message.includes("already exists")) return "目标文件名已经存在，请换一个名称。";
      return message;
    }

    function renderTransfer(state = {}) {
      const status = state.status || "idle";
      const percent = pct(state.percent || 0);
      const boxClass = status === "failed" ? "fail" : status === "done" ? "done" : "";
      refs.uploadBox.className = `transfer-box ${boxClass}`;
      refs.uploadBox.innerHTML = `
        <div class="transfer-title">
          <span>${escapeHtml(state.title || "传输状态")}</span>
          <span class="pill ${status === "failed" ? "bad" : status === "done" ? "" : "warn"}">${escapeHtml(state.badge || status)}</span>
        </div>
        <div class="progress-track"><div class="progress-fill" style="--value:${percent}%"></div></div>
        <div class="transfer-grid">
          <div><small>进度</small><strong>${Math.round(percent)}%</strong></div>
          <div><small>已传 / 总量</small><strong>${escapeHtml(fmtBytes(state.doneBytes || 0))} / ${escapeHtml(fmtBytes(state.totalBytes || 0))}</strong></div>
          <div><small>当前速度</small><strong>${escapeHtml(fmtRate(state.currentBps || 0))}</strong></div>
          <div><small>平均速度</small><strong>${escapeHtml(fmtRate(state.averageBps || 0))}</strong></div>
          <div><small>预计剩余</small><strong>${escapeHtml(fmtDuration(state.etaSeconds))}</strong></div>
          <div><small>目标</small><strong>${escapeHtml(state.target || "--")}</strong></div>
        </div>
        <div class="transfer-message">${escapeHtml(state.message || "等待操作。")}</div>
      `;
    }

    function uploadFormWithProgress(url, headers, form, onProgress, uploadState = null) {
      return new Promise((resolve, reject) => {
        const xhr = new XMLHttpRequest();
        if (uploadState) uploadState.xhr = xhr;
        xhr.open("POST", url, true);
        Object.entries(headers || {}).forEach(([key, value]) => xhr.setRequestHeader(key, value));
        xhr.upload.onprogress = (event) => {
          if (event.lengthComputable) onProgress(event.loaded, event.total);
        };
        xhr.onload = () => {
          let payload = {};
          try {
            payload = JSON.parse(xhr.responseText || "{}");
          } catch {
            payload = { ok: false, message: xhr.responseText || xhr.statusText || "目标 Agent 返回了无法识别的响应" };
          }
          if (xhr.status >= 200 && xhr.status < 300 && payload.ok) {
            resolve(payload);
          } else {
            reject(new Error(payload.message || xhr.statusText || `上传失败，HTTP ${xhr.status}`));
          }
        };
        xhr.onerror = () => reject(new Error("网络连接失败：浏览器无法连接到目标 Agent"));
        xhr.onabort = () => reject(new Error("上传已取消"));
        xhr.ontimeout = () => reject(new Error("上传超时：目标 Agent 响应太慢"));
        xhr.onloadend = () => {
          if (uploadState?.xhr === xhr) uploadState.xhr = null;
        };
        xhr.timeout = 0;
        xhr.send(form);
      });
    }

    async function sendUploadChunkWithRetry({ route, target, form, onProgress, uploadState, chunkIndex, totalChunks }) {
      const maxAttempts = 3;
      let lastError = null;
      for (let attempt = 1; attempt <= maxAttempts; attempt += 1) {
        if (uploadState?.canceled) throw new Error("上传已取消");
        try {
          return await uploadFormWithProgress(
            route.upload_url,
            route.headers || target.headers || {},
            form,
            onProgress,
            uploadState,
          );
        } catch (error) {
          lastError = error;
          if (uploadState?.canceled || attempt >= maxAttempts) break;
          renderTransfer({
            status: "running",
            badge: "重试中",
            title: "公网分片重试",
            target: uploadState?.targetLabel || route.label,
            percent: uploadState?.percent || 0,
            doneBytes: uploadState?.doneBytes || 0,
            totalBytes: uploadState?.totalBytes || 0,
            message: `第 ${chunkIndex + 1}/${totalChunks} 块上传失败，正在第 ${attempt + 1} 次重试：${friendlyError(error)}`,
          });
          await new Promise((resolve) => setTimeout(resolve, 800 * attempt));
        }
      }
      throw lastError || new Error("分片上传失败");
    }

    async function cancelUploadState(uploadState) {
      if (!uploadState || uploadState.cancelSent) return;
      uploadState.cancelSent = true;
      const cancelUrl = uploadState.route?.cancel_url || uploadState.target?.cancel_url;
      const cancelHeaders = uploadState.route?.headers || uploadState.target?.headers || {};
      if (!cancelUrl) return;
      await fetch(cancelUrl, {
        method: "POST",
        headers: { ...cancelHeaders, "Content-Type": "application/json" },
        body: JSON.stringify({ upload_id: uploadState.uploadId }),
      }).catch(() => null);
    }

    async function cancelActiveUpload() {
      const uploadState = activeUpload;
      if (!uploadState) return;
      uploadState.canceled = true;
      refs.cancelUploadBtn.disabled = true;
      if (uploadState.xhr) uploadState.xhr.abort();
      await cancelUploadState(uploadState);
      renderTransfer({
        status: "failed",
        badge: "已取消",
        title: "上传已取消",
        target: uploadState.targetLabel || "--",
        percent: uploadState.percent || 0,
        doneBytes: uploadState.doneBytes || 0,
        totalBytes: uploadState.totalBytes || 0,
        message: "已取消上传，Agent 上的临时分片已经清理。",
      });
    }

    async function probeUploadRoute(candidate) {
      const startedAt = performance.now();
      const payload = new Uint8Array(256 * 1024);
      try {
        const resp = await fetch(candidate.probe_url, {
          method: "POST",
          headers: candidate.headers || {},
          body: payload,
          cache: "no-store",
        });
        const elapsed = Math.max(0.001, (performance.now() - startedAt) / 1000);
        const data = await resp.json().catch(() => ({}));
        return {
          ...candidate,
          ok: resp.ok && data.ok !== false,
          elapsed,
          bps: payload.byteLength / elapsed,
          message: data.message || "",
        };
      } catch (error) {
        return {
          ...candidate,
          ok: false,
          elapsed: 9999,
          bps: 0,
          message: friendlyError(error, "线路测速失败"),
        };
      }
    }

    async function chooseUploadRoute(target) {
      const candidates = target.candidates?.length ? target.candidates : [{
        label: "默认线路",
        upload_url: target.upload_url,
        cancel_url: target.cancel_url,
        probe_url: target.probe_url || target.upload_url.replace("/api/upload-chunk", "/api/upload-probe"),
        headers: target.headers || {},
      }];
      const results = await Promise.all(candidates.map(probeUploadRoute));
      const usable = results.filter((item) => item.ok).sort((a, b) => b.bps - a.bps);
      if (!usable.length) {
        const reason = results.find((item) => item.message)?.message || "所有上传线路测速失败";
        throw new Error(reason);
      }
      return { selected: usable[0], results };
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
      const loadText = Array.isArray(h.load_avg) && h.load_avg.length ? h.load_avg.join(" / ") : (h.load_avg || "--");
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
            <div class="machine-compact">
              <span>核心 <strong>${escapeHtml(h.cpu_count || "--")}</strong></span>
              <span>负载 <strong>${escapeHtml(loadText)}</strong></span>
              <span>系统在线 <strong>${escapeHtml(h.uptime || "--")}</strong></span>
              <span>面板在线 <strong>${escapeHtml(h.app_uptime || "--")}</strong></span>
              <span>内存 <strong>${escapeHtml(`${fmtBytes(h.memory?.used || 0)} / ${fmtBytes(h.memory?.total || 0)}`)}</strong></span>
              <span>硬盘 <strong>${escapeHtml(`${fmtBytes(h.disk?.used || 0)} / ${fmtBytes(h.disk?.total || 0)}`)}</strong></span>
            </div>
          </div>
          ${nodeStatusPill(node)}
        </div>

        <div class="health-strip">
          ${donut("CPU", `${Number(h.cpu_percent || 0).toFixed(1)}%`, h.cpu_percent)}
          ${donut("内存", `${Number(h.memory?.percent || 0).toFixed(1)}%`, h.memory?.percent)}
          ${donut("硬盘", `${Number(h.disk?.percent || 0).toFixed(1)}%`, h.disk?.percent)}
          ${donut("推流", stream.running ? "运行中" : "未推流", stream.running ? 100 : 0, stream.running ? "var(--accent)" : "var(--danger)")}
        </div>

        <div class="monitor-compact-row">
          <div class="agent-compact">
            <span>Agent <strong>${escapeHtml(agent.mode || "compatible")}</strong></span>
            <span>版本 <strong>${escapeHtml(agent.version || "--")}</strong></span>
            <span>${agent.headless ? "Headless" : "兼容模式"}</span>
            <span>上传 <strong>${publicUpload.supported === false ? "直传" : "票据直传"}</strong></span>
            <span>路由 <strong>${escapeHtml(transfer.last_route || "--")}</strong></span>
            <span>错误 <strong>${escapeHtml(transfer.last_error || "无")}</strong></span>
          </div>

          <div class="network-compact">
            <span class="compact-title">网络</span>
            <span>上行 <strong>${escapeHtml(fmtRate(net.current_upload_bps || 0))}</strong></span>
            <span>下行 <strong>${escapeHtml(fmtRate(net.current_download_bps || 0))}</strong></span>
            <span>累计发 <strong>${escapeHtml(fmtBytes(net.bytes_sent || 0))}</strong></span>
            <span>累计收 <strong>${escapeHtml(fmtBytes(net.bytes_recv || 0))}</strong></span>
            <span>流量 <strong>${escapeHtml(`${Number(quota.total_percent || 0).toFixed(2)}%`)}</strong></span>
            <span>剩余 <strong>${escapeHtml(fmtBytes(quota.remaining || 0))}</strong></span>
            <span>线路 <strong>${escapeHtml(net.rate_label || "--")}</strong></span>
          </div>
        </div>

        <div class="monitor-panel-grid">
          <div class="monitor-panel">
            <h4>推流引擎</h4>
            <div class="metric-grid">
              ${metric("FFmpeg", stream.running ? "运行中" : "未运行")}
              ${metric("进程", processText)}
              ${metric("视频数", `${videos.length}`)}
              ${metric("推流目标", config.stream_output_mode === "youtube_api" ? "YouTube API" : (config.has_stream_key ? "直播码" : "未配置"))}
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
      const online = Boolean(node.roles?.agent?.enabled ?? nodeOnline(node));
      const streaming = nodeStreaming(node);
      const selected = String(node.id) === String(selectedNodeId);
      const checked = checkedIds.has(String(node.id));
      return `
        <div class="node-row ${selected ? "selected" : ""}" data-node-row data-node-id="${escapeHtml(node.id)}">
          <input data-node-check type="checkbox" value="${escapeHtml(node.id)}" ${checked ? "checked" : ""} ${node.enabled === false ? "disabled" : ""} title="选中后可推送资源或升级">
          <span class="node-name">
            <strong>${escapeHtml(node.name || node.id)}</strong>
            <small>${escapeHtml(h.hostname || node.id)} · 版本 ${escapeHtml(h.agent?.version || "未识别")}</small>
          </span>
          <span class="node-state">${stateDot(online, node.enabled === false)}${online ? "在线" : node.enabled === false ? "禁用" : "离线"}</span>
          <span class="node-state">${streamDot(streaming)}${streaming ? "推流中" : "未推流"}</span>
          <span class="row-actions">
            <button class="tiny" data-node-action="stop-stream" data-node-id="${escapeHtml(node.id)}" ${online ? "" : "disabled"}>停止推流</button>
            <button class="tiny" data-node-action="restart-stream" data-node-id="${escapeHtml(node.id)}" ${online ? "" : "disabled"}>重启推流</button>
            <button class="tiny danger" data-node-action="reboot-vps" data-node-id="${escapeHtml(node.id)}" ${online ? "" : "disabled"}>重启 VPS</button>
            <button class="tiny settings-button" data-role-settings data-node-id="${escapeHtml(node.id)}" title="节点角色设置" aria-label="节点角色设置">⚙</button>
          </span>
        </div>
      `;
    }

    function renderHubRow(node) {
      const role = node.roles?.hub || {};
      const enabled = Boolean(role.enabled);
      const version = role.version || "未安装";
      return `
        <div class="node-row role-row" data-hub-row data-node-id="${escapeHtml(node.id)}" data-hub-url="${escapeHtml(role.url || "")}">
          <span>${stateDot(enabled, false)}</span>
          <span class="node-name"><strong>${escapeHtml(node.name || node.id)}</strong><small>Hub 版本 ${escapeHtml(version)}</small></span>
          <span class="node-state">${enabled ? "已启用" : "未启用"}</span>
          <span class="node-state">8788</span>
          <span class="row-actions">
            <button class="tiny" data-role-action="switch-hub" data-node-id="${escapeHtml(node.id)}">切换 Hub</button>
            <button class="tiny settings-button" data-role-settings data-node-id="${escapeHtml(node.id)}" title="节点角色设置" aria-label="节点角色设置">⚙</button>
          </span>
        </div>
      `;
    }

    function renderNodes() {
      const checkedIds = new Set(selectedNodeIds().map(String));
      if (!nodes.length) {
        refs.nodeMonitor.innerHTML = renderMonitor(null);
        refs.nodeList.innerHTML = `<div class="empty-state">还没有配置节点。</div>`;
        refs.hubNodeList.innerHTML = `<div class="empty-state">还没有配置节点。</div>`;
        return;
      }
      if (!nodes.some((node) => String(node.id) === String(selectedNodeId))) {
        rememberSelectedNode(nodes[0].id || "");
      }
      refs.nodeMonitor.innerHTML = renderMonitor(selectedNode());
      const activeAgents = nodes.filter((node) => Boolean(node.roles?.agent?.enabled));
      const activeHubs = nodes.filter((node) => Boolean(node.roles?.hub?.enabled));
      refs.nodeList.innerHTML = activeAgents.length ? `
        <div class="node-table-head">
          <span></span>
          <span>节点</span>
          <span>在线</span>
          <span>推流</span>
          <span>操作</span>
        </div>
        ${activeAgents.map((node) => renderNodeRow(node, checkedIds)).join("")}
      ` : `<div class="empty-state">还没有已激活的 Agent。</div>`;
      refs.hubNodeList.innerHTML = activeHubs.length ? `
        <div class="node-table-head"><span></span><span>Hub 节点</span><span>状态</span><span>端口</span><span>操作</span></div>
        ${activeHubs.map((node) => renderHubRow(node)).join("")}
      ` : `<div class="empty-state">还没有已激活的 Hub。</div>`;
    }

    function renderMedia() {
      const checkedPath = selectedMediaPath();
      const checkedNodeId = selectedMediaNodeId();
      const entries = nodes
        .flatMap((mediaNode) => [...(mediaNode.health?.videos || [])].map((item) => ({ node: mediaNode, item })))
        .sort((a, b) => Number(b.item.modified || 0) - Number(a.item.modified || 0));
      if (!entries.length) {
        refs.mediaList.innerHTML = `<div class="empty-state">还没有任何 Agent 视频。先上传到当前 Agent，或从其他 Agent 共享过来。</div>`;
        return;
      }
      refs.mediaList.innerHTML = `
        <div class="media-toolbar">
          <strong>全部 Agent 文件</strong>
          <small>按上传时间倒序，共 ${entries.length} 个。右键文件操作。</small>
        </div>
        <div class="media-window">
          <div class="media-window-head">
            <span>文件名</span>
            <span>大小</span>
            <span>上传时间</span>
            <span>所属 Agent</span>
          </div>
          ${entries.map(({ node, item }) => {
            const videoPath = item.video_path || item.path || item.name;
            const nodeId = String(node.id || "");
            const selected = checkedPath && checkedNodeId === nodeId && checkedPath === videoPath;
            const current = nodeId === String(selectedNodeId);
            const name = item.name || videoPath;
            return `
              <div role="button" tabindex="0" class="media-file-row ${current ? "current-agent" : ""} ${selected ? "selected" : ""}" data-media-row data-node-id="${escapeHtml(nodeId)}" data-media-name="${escapeHtml(name)}" data-video-path="${escapeHtml(videoPath)}" data-size="${escapeHtml(item.size || 0)}" data-modified-label="${escapeHtml(item.modified_label || "--")}">
                <span title="${escapeHtml(name)}">${escapeHtml(name)}</span>
                <span class="muted">${escapeHtml(fmtBytes(item.size || 0))}</span>
                <span class="muted">${escapeHtml(item.modified_label || "--")}</span>
                <span title="${escapeHtml(node.name || node.id || "Agent")}">${escapeHtml(node.name || node.id || "Agent")}</span>
                <input data-media-check type="radio" name="media" value="${escapeHtml(name)}" data-node-id="${escapeHtml(nodeId)}" data-video-path="${escapeHtml(videoPath)}" ${selected ? "checked" : ""} hidden>
              </div>
            `;
          }).join("")}
        </div>
      `;
    }

    function syncStreamOutputMode() {
      const mode = refs.streamOutputModeInput.value || "direct";
      refs.streamKeyInput.disabled = mode !== "direct";
      refs.youtubeStreamSelect.disabled = mode !== "youtube_api";
    }

    function setYouTubeModalOpen(open) {
      refs.youtubeWizardModal.classList.toggle("open", open);
      refs.youtubeWizardModal.setAttribute("aria-hidden", open ? "false" : "true");
      if (!open && youtubeOauthPollTimer) {
        clearTimeout(youtubeOauthPollTimer);
        youtubeOauthPollTimer = null;
      }
      if (open) {
        const node = selectedNode();
        refs.youtubeNodeInput.value = node ? `${node.name || node.id} (${node.id})` : "先选择 Agent";
        if (!refs.youtubeScheduleInput.value) {
          const planned = new Date(Date.now() + 5 * 60 * 1000);
          const local = new Date(planned.getTime() - planned.getTimezoneOffset() * 60 * 1000);
          refs.youtubeScheduleInput.value = local.toISOString().slice(0, 16);
        }
        refreshYouTubeResources();
      }
    }

    function renderYouTubeStreams(streams = [], selectedStreamId = "") {
      const previousMain = selectedStreamId || refs.youtubeStreamSelect.value;
      const previousPrepare = refs.youtubePrepareStreamSelect.value;
      const streamOptions = streams.map((item) => {
        const status = item.stream_status || "ready";
        return `<option value="${escapeHtml(item.id)}">${escapeHtml(item.title || item.id)} (${escapeHtml(status)})</option>`;
      }).join("");
      refs.youtubeStreamSelect.innerHTML = streams.length
        ? streamOptions
        : `<option value="">没有可用 YouTube 直播流</option>`;
      refs.youtubePrepareStreamSelect.innerHTML = `<option value="">创建新的可复用直播流</option>${streamOptions}`;
      if (streams.some((item) => item.id === previousMain)) refs.youtubeStreamSelect.value = previousMain;
      if (streams.some((item) => item.id === previousPrepare)) refs.youtubePrepareStreamSelect.value = previousPrepare;
      syncStreamOutputMode();
    }

    async function refreshYouTubeResources() {
      const node = selectedNode();
      if (!node) {
        refs.youtubeWizardLog.textContent = "请先选择一个 Agent。";
        return null;
      }
      refs.youtubeRefreshBtn.disabled = true;
      refs.youtubeNodeInput.value = `${node.name || node.id} (${node.id})`;
      refs.youtubeWizardLog.textContent = "正在从 Agent 读取 YouTube 授权和直播资源...";
      try {
        const data = await postNodeAction("/api/nodes/youtube/resources", { node_id: selectedNodeId });
        if (!data.ok && data.configured === undefined) {
          renderYouTubeStreams([]);
          refs.youtubeWizardLog.textContent = data.message || "YouTube API 读取失败";
          return data;
        }
        if (!data.configured) {
          renderYouTubeStreams([]);
          refs.youtubeWizardLog.textContent = "当前 Agent 尚未配置 YOUTUBE_CLIENT_ID。请先在 Agent 的 .agent.env 配置 Google TV / Limited Input OAuth client ID，再更新或重启 Agent。";
          return data;
        }
        if (!data.authorized) {
          renderYouTubeStreams([]);
          refs.youtubeWizardLog.textContent = "Client ID 已配置，频道尚未授权。点击“连接 YouTube”获取设备验证码。";
          return data;
        }
        if (!data.ok) {
          refs.youtubeWizardLog.textContent = data.message || "YouTube API 读取失败";
          return data;
        }
        const selectedId = selectedNode()?.health?.stream_config?.youtube_stream_id || "";
        renderYouTubeStreams(data.streams || [], selectedId);
        const lines = [
          `频道：${data.channel?.title || "--"}`,
          `直播流：${(data.streams || []).length} 个`,
          `直播活动：${(data.broadcasts || []).length} 个`,
        ];
        (data.broadcasts || []).slice(0, 5).forEach((item) => {
          lines.push(`${item.title || item.id} / ${item.life_cycle_status || "--"} / ${item.privacy_status || "--"}`);
        });
        refs.youtubeWizardLog.textContent = lines.join("\n");
        return data;
      } catch (error) {
        refs.youtubeWizardLog.textContent = friendlyError(error, "YouTube API 读取失败");
        return null;
      } finally {
        refs.youtubeRefreshBtn.disabled = false;
      }
    }

    async function pollYouTubeAuthorization(delaySeconds = 5) {
      if (!youtubeOauthSession) return;
      if (youtubeOauthPollTimer) clearTimeout(youtubeOauthPollTimer);
      youtubeOauthPollTimer = setTimeout(async () => {
        try {
          const data = await postNodeAction("/api/nodes/youtube/oauth/poll", {
            node_id: selectedNodeId,
            session_id: youtubeOauthSession,
          });
          if (data.ok && data.authorized) {
            youtubeOauthSession = "";
            youtubeOauthPollTimer = null;
            refs.youtubeWizardLog.textContent = "YouTube 授权成功，正在读取频道资源...";
            await refreshYouTubeResources();
            return;
          }
          if (data.ok && data.pending) {
            pollYouTubeAuthorization(Number(data.retry_after || 5));
            return;
          }
          youtubeOauthSession = "";
          youtubeOauthPollTimer = null;
          refs.youtubeWizardLog.textContent = data.message || "YouTube 授权失败";
        } catch (error) {
          youtubeOauthSession = "";
          youtubeOauthPollTimer = null;
          refs.youtubeWizardLog.textContent = friendlyError(error, "YouTube 授权状态读取失败");
        }
      }, Math.max(1, Number(delaySeconds || 5)) * 1000);
    }

    async function startYouTubeAuthorization() {
      if (!selectedNodeId) {
        refs.youtubeWizardLog.textContent = "请先选择一个 Agent。";
        return;
      }
      refs.youtubeAuthorizeBtn.disabled = true;
      try {
        const data = await postNodeAction("/api/nodes/youtube/oauth/start", { node_id: selectedNodeId });
        if (!data.ok) {
          refs.youtubeWizardLog.textContent = data.message || "无法启动 YouTube 授权";
          return;
        }
        youtubeOauthSession = data.session_id;
        refs.youtubeWizardLog.innerHTML = `
          <div class="wizard-status-line done"><strong>设备验证码：${escapeHtml(data.user_code)}</strong></div>
          <div class="wizard-status-line"><a href="${escapeHtml(data.verification_url)}" target="_blank" rel="noopener">打开 Google 设备授权页面</a></div>
          <div class="wizard-status-line">完成授权后本页会自动刷新。</div>
        `;
        window.open(data.verification_url, "_blank", "noopener");
        pollYouTubeAuthorization(Number(data.interval || 5));
      } catch (error) {
        refs.youtubeWizardLog.textContent = friendlyError(error, "无法启动 YouTube 授权");
      } finally {
        refs.youtubeAuthorizeBtn.disabled = false;
      }
    }

    async function saveYouTubeConfig() {
      if (!selectedNodeId) {
        refs.youtubeWizardLog.textContent = "请先选择一个 Agent。";
        return;
      }
      const clientId = refs.youtubeClientIdInput.value.trim();
      const clientSecret = refs.youtubeClientSecretInput.value.trim();
      if (!clientId) {
        refs.youtubeWizardLog.textContent = "请先填写 Google OAuth Client ID。";
        return;
      }
      refs.youtubeSaveConfigBtn.disabled = true;
      refs.youtubeWizardLog.textContent = "正在把 YouTube API 配置保存到当前 Agent...";
      try {
        const data = await postNodeAction("/api/nodes/youtube/config", {
          node_id: selectedNodeId,
          client_id: clientId,
          client_secret: clientSecret,
        });
        if (!data.ok) {
          refs.youtubeWizardLog.textContent = data.message || "YouTube API 配置保存失败";
          return;
        }
        refs.youtubeClientSecretInput.value = "";
        refs.youtubeWizardLog.textContent = "配置已保存。下一步点击“连接 YouTube”完成频道授权。";
        await refreshYouTubeResources();
      } catch (error) {
        refs.youtubeWizardLog.textContent = friendlyError(error, "YouTube API 配置保存失败");
      } finally {
        refs.youtubeSaveConfigBtn.disabled = false;
      }
    }

    async function prepareYouTubeBroadcast() {
      const title = refs.youtubeTitleInput.value.trim();
      if (!selectedNodeId || !title) {
        refs.youtubeWizardLog.textContent = "请选择 Agent 并填写直播标题。";
        return;
      }
      refs.youtubePrepareBtn.disabled = true;
      try {
        const resolutionMatch = refs.resolutionInput.value.match(/x(\d+)$/i);
        const scheduled = refs.youtubeScheduleInput.value
          ? new Date(refs.youtubeScheduleInput.value).toISOString()
          : "";
        const data = await postNodeAction("/api/nodes/youtube/prepare", {
          node_id: selectedNodeId,
          title,
          privacy_status: refs.youtubePrivacyInput.value,
          scheduled_start_time: scheduled,
          stream_id: refs.youtubePrepareStreamSelect.value,
          resolution: resolutionMatch ? `${resolutionMatch[1]}p` : "720p",
          frame_rate: Number(refs.fpsInput.value || 30) >= 50 ? "60fps" : "30fps",
          enable_auto_start: true,
          enable_auto_stop: true,
        });
        if (!data.ok) {
          refs.youtubeWizardLog.textContent = data.message || "创建 YouTube 直播失败";
          return;
        }
        refs.youtubeWizardLog.textContent = `直播已创建并绑定。\n${data.result?.title || title}\n${data.result?.watch_url || ""}`;
        await refreshYouTubeResources();
        refs.youtubeStreamSelect.value = data.result?.stream_id || "";
        refs.streamOutputModeInput.value = "youtube_api";
        syncStreamOutputMode();
      } catch (error) {
        refs.youtubeWizardLog.textContent = friendlyError(error, "创建 YouTube 直播失败");
      } finally {
        refs.youtubePrepareBtn.disabled = false;
      }
    }

    async function revokeYouTubeAuthorization() {
      if (!selectedNodeId || !window.confirm("确认断开当前 Agent 的 YouTube 授权？")) return;
      try {
        const data = await postNodeAction("/api/nodes/youtube/oauth/revoke", { node_id: selectedNodeId });
        refs.youtubeWizardLog.textContent = data.ok ? "YouTube 授权已断开。" : (data.message || "断开授权失败");
        if (data.ok) renderYouTubeStreams([]);
      } catch (error) {
        refs.youtubeWizardLog.textContent = friendlyError(error, "断开授权失败");
      }
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
      if (config.youtube_stream_id) refs.youtubeStreamSelect.value = config.youtube_stream_id;
      syncStreamOutputMode();
    }

    function streamPayload({ includeKey = true } = {}) {
      const payload = {
        node_id: selectedNodeId,
        stream_url: refs.streamUrlInput.value.trim(),
        stream_key: includeKey ? refs.streamKeyInput.value.trim() : "",
        youtube_stream_id: refs.youtubeStreamSelect.value,
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
        const nodeResp = await fetch("/api/nodes");
        nodes = await nodeResp.json();
        renderNodes();
        renderMedia();
        renderStreamControls();
        renderTailscaleNodeOptions();
        log("状态已刷新");
      } finally {
        refs.refreshBtn.disabled = false;
      }
    }

    async function uploadMedia() {
      const node = selectedNode();
      const file = refs.mediaInput.files[0];
      if (!node?.id) {
        renderTransfer({ status: "failed", badge: "失败", title: "上传未开始", message: "请先在右侧选择一个目标 Agent。" });
        return;
      }
      if (!file) {
        renderTransfer({ status: "failed", badge: "失败", title: "上传未开始", message: "请先选择一个视频文件。" });
        return;
      }
      refs.uploadBtn.disabled = true;
      refs.cancelUploadBtn.disabled = false;
      const uploadId = `browser_${Date.now()}_${Math.random().toString(16).slice(2)}`;
      let target = null;
      let uploadRoute = null;
      const uploadState = {
        uploadId,
        target: null,
        route: null,
        xhr: null,
        canceled: false,
        cancelSent: false,
        targetLabel: node.name || node.id,
        doneBytes: 0,
        totalBytes: file.size,
        percent: 0,
      };
      activeUpload = uploadState;
      try {
        const targetResp = await fetch("/api/nodes/upload-target", {
          method: "POST",
          headers: authHeaders({ "Content-Type": "application/json" }),
          body: JSON.stringify({ node_id: node.id, upload_id: uploadId, filename: file.name, total_size: file.size }),
        });
        target = await targetResp.json();
        uploadState.target = target;
        if (!target.ok) {
          renderTransfer({
            status: "failed",
            badge: "失败",
            title: "无法获取上传目标",
            message: friendlyError(target.message || "Hub 未返回可用 Agent 上传地址"),
          });
          return;
        }
        if (uploadState.canceled) throw new Error("上传已取消");
        renderTransfer({
          status: "running",
          badge: "测速中",
          title: `选择上传线路`,
          target: node.name || node.id,
          totalBytes: file.size,
          message: "正在自动测速公网和 Tailscale 线路，优先选择最快公网直连。",
        });
        const routeChoice = await chooseUploadRoute(target);
        if (uploadState.canceled) throw new Error("上传已取消");
        uploadRoute = routeChoice.selected;
        uploadState.route = uploadRoute;
        uploadState.targetLabel = `${node.name || node.id} / ${uploadRoute.label}`;
        const uploadFilename = target.filename || file.name;
        const savedNameNote = uploadFilename !== file.name ? `，保存名：${uploadFilename}` : "";
        const chunkSize = Number(target.chunk_bytes || 16 * 1024 * 1024);
        const totalChunks = Math.ceil(file.size / chunkSize);
        const startedAt = performance.now();
        let lastPayload = {};
        let lastPaintAt = 0;
        renderTransfer({
          status: "running",
          badge: "上传中",
          title: `上传到 ${node.name || node.id}`,
          target: `${node.name || node.id} / ${uploadRoute.label}`,
          totalBytes: file.size,
          currentBps: uploadRoute.bps || 0,
          message: `已选择 ${uploadRoute.label}，测速 ${fmtRate(uploadRoute.bps || 0)}，准备上传：${file.name}${savedNameNote}`,
        });
        for (let chunkIndex = 0; chunkIndex < totalChunks; chunkIndex += 1) {
          if (uploadState.canceled) throw new Error("上传已取消");
          const offset = chunkIndex * chunkSize;
          const blob = file.slice(offset, Math.min(file.size, offset + chunkSize));
          const form = new FormData();
          form.append("upload_id", uploadId);
          form.append("filename", uploadFilename);
          form.append("chunk_index", String(chunkIndex));
          form.append("total_chunks", String(totalChunks));
          form.append("offset", String(offset));
          form.append("total_size", String(file.size));
          form.append("chunk_size", String(chunkSize));
          form.append("chunk", blob, uploadFilename);
          const chunkStartedAt = performance.now();
          lastPayload = await sendUploadChunkWithRetry({
            route: uploadRoute,
            target,
            form,
            uploadState,
            chunkIndex,
            totalChunks,
            onProgress: (loaded) => {
            const now = performance.now();
            if (now - lastPaintAt < 250 && loaded < blob.size) return;
            lastPaintAt = now;
            const chunkSeconds = Math.max(0.001, (now - chunkStartedAt) / 1000);
            const totalSeconds = Math.max(0.001, (now - startedAt) / 1000);
            const uploaded = Math.min(file.size, offset + loaded);
            const averageBps = uploaded / totalSeconds;
            uploadState.doneBytes = uploaded;
            uploadState.percent = file.size ? (uploaded / file.size) * 100 : 0;
            renderTransfer({
              status: "running",
              badge: "上传中",
              title: `上传到 ${node.name || node.id}`,
              target: `${node.name || node.id} / ${uploadRoute.label}`,
              percent: file.size ? (uploaded / file.size) * 100 : 0,
              doneBytes: uploaded,
              totalBytes: file.size,
              currentBps: loaded / chunkSeconds,
              averageBps,
              etaSeconds: averageBps > 0 ? (file.size - uploaded) / averageBps : 0,
              message: `正在通过 ${uploadRoute.label} 上传 ${file.name}${savedNameNote}，第 ${chunkIndex + 1}/${totalChunks} 块。`,
            });
            },
          });
          const chunkSeconds = Math.max(0.001, (performance.now() - chunkStartedAt) / 1000);
          const totalSeconds = Math.max(0.001, (performance.now() - startedAt) / 1000);
          const uploaded = Math.min(file.size, offset + blob.size);
          const averageBps = uploaded / totalSeconds;
          uploadState.doneBytes = uploaded;
          uploadState.percent = file.size ? (uploaded / file.size) * 100 : 0;
          renderTransfer({
            status: "running",
            badge: "上传中",
            title: `上传到 ${node.name || node.id}`,
            target: `${node.name || node.id} / ${uploadRoute.label}`,
            percent: file.size ? (uploaded / file.size) * 100 : 0,
            doneBytes: uploaded,
            totalBytes: file.size,
            currentBps: blob.size / chunkSeconds,
            averageBps,
            etaSeconds: averageBps > 0 ? (file.size - uploaded) / averageBps : 0,
            message: `正在通过 ${uploadRoute.label} 上传 ${file.name}${savedNameNote}，第 ${chunkIndex + 1}/${totalChunks} 块。`,
          });
        }
        const elapsed = Math.max(0.001, (performance.now() - startedAt) / 1000);
        renderTransfer({
          status: "done",
          badge: "完成",
          title: `上传完成`,
          target: `${node.name || node.id} / ${uploadRoute?.label || "默认线路"}`,
          percent: 100,
          doneBytes: file.size,
          totalBytes: file.size,
          currentBps: 0,
          averageBps: file.size / elapsed,
          etaSeconds: 0,
          message: `${file.name} 已通过 ${uploadRoute?.label || "默认线路"} 上传到 ${node.name || node.id}${savedNameNote}。`,
        });
        await refreshAll();
      } catch (error) {
        await cancelUploadState(uploadState);
        if (uploadState.canceled) {
          renderTransfer({
            status: "failed",
            badge: "已取消",
            title: "上传已取消",
            target: `${node.name || node.id}${uploadRoute?.label ? " / " + uploadRoute.label : ""}`,
            percent: uploadState.percent || 0,
            doneBytes: uploadState.doneBytes || 0,
            totalBytes: file.size,
            message: "已取消上传，Agent 上的临时分片已经清理。",
          });
          return;
        }
        renderTransfer({
          status: "failed",
          badge: "失败",
          title: "上传失败",
          target: `${node.name || node.id}${uploadRoute?.label ? " / " + uploadRoute.label : ""}`,
          totalBytes: file.size,
          message: friendlyError(error, "上传失败"),
        });
      } finally {
        refs.uploadBtn.disabled = false;
        refs.cancelUploadBtn.disabled = true;
        if (activeUpload === uploadState) activeUpload = null;
      }
    }

    async function checkUpdates() {
      refs.updateBox.textContent = "正在检查 GitHub...";
      const resp = await fetch("/api/github/check", { method: "POST", headers: authHeaders() });
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

    async function showTailscaleStatus() {
      setTailscaleWizardOpen(true);
      setTailscaleStep("verify", "running");
      setTailscaleLog("正在读取 Tailscale 状态...");
      const resp = await fetch("/api/tailscale/status");
      const data = await resp.json();
      setTailscaleStep("verify", data.ok ? "done" : "fail");
      setTailscaleLog(data);
      refs.updateBox.textContent = JSON.stringify(data, null, 2);
    }

    function setTailscaleWizardOpen(open) {
      refs.tailscaleWizardModal.classList.toggle("open", open);
      refs.tailscaleWizardModal.setAttribute("aria-hidden", open ? "false" : "true");
    }

    function tailscaleStatusLines(data, fallback = "Tailscale 状态已更新") {
      if (typeof data === "string") return [{ text: data }];
      const ok = Boolean(data?.ok);
      const lines = [{ text: ok ? (data.message || fallback) : (data?.message || "操作失败"), tone: ok ? "done" : "fail" }];
      if (data?.installed === false) lines.push({ text: "当前机器还没有安装 Tailscale。", tone: "fail" });
      if (data?.backend_state) lines.push({ label: "运行状态", text: data.backend_state });
      const self = data?.self || data?.status?.self || {};
      const tailscaleIps = self.tailscale_ips || data?.tailscale_ips || data?.status?.tailscale_ips || [];
      if (tailscaleIps.length) lines.push({ label: "本机 Tailscale IP", text: tailscaleIps.join(" / "), tone: "done" });
      if (self.dns_name) lines.push({ label: "Tailnet 名称", text: self.dns_name });
      const peers = data?.peers || data?.status?.peers || [];
      if (Array.isArray(peers)) lines.push({ label: "可见设备", text: `${peers.length} 台` });
      if (data?.node_id && data?.base_url) lines.push({ label: "Agent 连接", text: `${data.node_id} -> ${data.base_url}`, tone: "done" });
      if (data?.previous_base_url) lines.push({ label: "原地址已保留", text: data.previous_base_url });
      const detail = data?.error || data?.result?.stderr || data?.result?.message || data?.precheck?.message || "";
      if (!ok && detail) lines.push({ label: "失败原因", text: String(detail).slice(0, 260), tone: "fail" });
      lines.push({ label: "下一步", text: ok ? "可以刷新节点列表，或继续上传/共享/推流。" : "请按提示修复后重试。" });
      return lines;
    }

    function setTailscaleLog(value, fallback = "Tailscale 状态已更新") {
      const lines = tailscaleStatusLines(value, fallback);
      refs.tailscaleWizardLog.innerHTML = lines.map((line) => {
        const cls = line.tone === "fail" ? " fail" : line.tone === "done" ? " done" : "";
        const label = line.label ? `<strong>${escapeHtml(line.label)}：</strong>` : "";
        return `<div class="wizard-status-line${cls}">${label}${escapeHtml(line.text || "")}</div>`;
      }).join("");
    }

    function setTailscaleStep(step, state) {
      const el = refs.tailscaleWizardModal.querySelector(`[data-tailscale-step="${step}"]`);
      if (!el) return;
      el.classList.remove("done", "fail");
      if (state === "done" || state === "fail") el.classList.add(state);
    }

    function setTailscaleBusy(busy) {
      [refs.tailscalePrecheckBtn, refs.tailscaleInstallBtn, refs.tailscaleConnectBtn, refs.tailscaleVerifyBtn, refs.tailscaleUseExistingIpBtn]
        .forEach((button) => { button.disabled = busy; });
    }

    function renderTailscaleNodeOptions() {
      const current = refs.tailscaleNodeSelect.dataset.initialized === "1"
        ? refs.tailscaleNodeSelect.value
        : "";
      const existingOptions = nodes.map((node) => {
            const id = String(node.id || "");
            const label = `${node.name || id} (${node.base_url || "未配置地址"})`;
            return `<option value="${escapeHtml(id)}" ${id === current ? "selected" : ""}>${escapeHtml(label)}</option>`;
          }).join("");
      refs.tailscaleNodeSelect.innerHTML = `
        <option value="" ${current ? "" : "selected"}>新增 Agent（仅输入 IP）</option>
        ${existingOptions}
      `;
      refs.tailscaleNodeSelect.dataset.initialized = "1";
    }

    async function runTailscaleStep(step, label, action) {
      setTailscaleWizardOpen(true);
      setTailscaleBusy(true);
      setTailscaleStep(step, "running");
      setTailscaleLog(`${label}...`);
      try {
        const data = await action();
        setTailscaleStep(step, data.ok ? "done" : "fail");
        setTailscaleLog(data);
        refs.updateBox.textContent = JSON.stringify(data, null, 2);
        return data;
      } catch (error) {
        const data = { ok: false, message: friendlyError(error, `${label}失败`) };
        setTailscaleStep(step, "fail");
        setTailscaleLog(data);
        refs.updateBox.textContent = JSON.stringify(data, null, 2);
        return data;
      } finally {
        setTailscaleBusy(false);
      }
    }

    async function precheckTailscale() {
      return runTailscaleStep("precheck", "正在检查 Tailscale 安装环境", async () => {
        const resp = await fetch("/api/tailscale/precheck");
        return resp.json();
      });
    }

    async function installTailscale() {
      return runTailscaleStep("install", "正在安装或修复 Tailscale", async () => {
        const resp = await fetch("/api/tailscale/install", {
          method: "POST",
          headers: authHeaders({ "Content-Type": "application/json" }),
          body: JSON.stringify({}),
        });
        return resp.json();
      });
    }

    async function connectTailscale() {
      const auth_key = refs.tailscaleAuthInput.value.trim();
      const hostname = refs.tailscaleHostInput.value.trim() || "stream-control-hub";
      if (!auth_key) {
        setTailscaleWizardOpen(true);
        setTailscaleLog("请输入一次性 Tailscale auth key。");
        return;
      }
      const data = await runTailscaleStep("connect", "正在使用 auth key 登录 Tailscale", async () => {
        const resp = await fetch("/api/tailscale/connect", {
          method: "POST",
          headers: authHeaders({ "Content-Type": "application/json" }),
          body: JSON.stringify({ auth_key, hostname, ssh: false, accept_routes: true }),
        });
        return resp.json();
      });
      if (data.ok) {
        refs.tailscaleAuthInput.value = "";
        setTailscaleStep("verify", data.status?.ok ? "done" : "fail");
      }
    }

    async function verifyTailscale() {
      return showTailscaleStatus();
    }

    async function connectExistingTailscaleIp() {
      const node_id = refs.tailscaleNodeSelect.value || "";
      const tailscale_ip = refs.tailscaleExistingIpInput.value.trim();
      if (!tailscale_ip) {
        setTailscaleWizardOpen(true);
        setTailscaleLog("请输入已有的 Tailscale IP，例如 100.x.x.x。");
        return;
      }
      const data = await runTailscaleStep("verify", "正在验证已有 Tailscale IP 并连接 Agent", async () => {
        const resp = await fetch("/api/tailscale/connect-existing-ip", {
          method: "POST",
          headers: authHeaders({ "Content-Type": "application/json" }),
          body: JSON.stringify({ node_id, tailscale_ip }),
        });
        return resp.json();
      });
      if (data.ok) {
        rememberSelectedNode(data.node_id || selectedNodeId || "");
        log(`已连接 ${data.node_id} 到 ${data.base_url}`);
        await refreshAll();
      }
    }

    async function pushSelectedMedia() {
      const sourceNode = nodes.find((item) => String(item.id) === String(selectedMediaNodeId())) || selectedNode();
      const target_node_ids = selectedNodeIds().filter((id) => String(id) !== String(sourceNode?.id || ""));
      const media = selectedMediaPath() || selectedMediaName();
      const targetLabel = target_node_ids
        .map((id) => nodes.find((item) => String(item.id) === String(id))?.name || id)
        .join(", ");
      if (!sourceNode?.id || !target_node_ids.length || !media) {
        renderTransfer({
          status: "failed",
          badge: "失败",
          title: "共享未开始",
          message: "请选择源 Agent 的一个服务器视频，并勾选至少一个其他 Agent。",
        });
        return;
      }
      if (refs.pushSelectedBtn) refs.pushSelectedBtn.disabled = true;
      renderTransfer({
        status: "running",
        badge: "共享中",
        title: `共享到 ${targetLabel}`,
        target: targetLabel,
        message: `正在创建共享任务：${media}`,
      });
      try {
        const resp = await fetch("/api/media/share", {
          method: "POST",
          headers: authHeaders({ "Content-Type": "application/json" }),
          body: JSON.stringify({ source_node_id: sourceNode.id, target_node_ids, media }),
        });
        const first = await resp.json().catch(() => ({ ok: false, message: resp.statusText }));
        if (!resp.ok || !first.ok || !first.task_id) {
          renderTransfer({
            status: "failed",
            badge: "失败",
            title: "共享启动失败",
            target: targetLabel,
            message: friendlyError(first.message || first.error || "Hub 未能创建共享任务"),
          });
          return;
        }
        let last = first;
        while (true) {
          renderTransfer({
            status: last.status === "done" ? "done" : last.status === "failed" ? "failed" : "running",
            badge: last.status === "done" ? "完成" : last.status === "failed" ? "失败" : "共享中",
            title: last.status === "done" ? "共享完成" : last.status === "failed" ? "共享失败" : `共享到 ${targetLabel}`,
            target: targetLabel,
            percent: last.percent || 0,
            doneBytes: last.done_bytes || 0,
            totalBytes: last.total_bytes || 0,
            currentBps: last.current_bps || 0,
            averageBps: last.average_bps || 0,
            etaSeconds: last.eta_seconds || 0,
            message: last.status === "failed"
              ? friendlyError(last.error || last.message || "共享失败")
              : last.status === "done"
                ? `${media} 已共享到 ${targetLabel}。`
                : (last.message || "正在共享，请稍候。"),
          });
          if (last.status === "done") {
            await refreshAll();
            break;
          }
          if (last.status === "failed") {
            break;
          }
          await new Promise((resolve) => setTimeout(resolve, 1000));
          const statusResp = await fetch(`/api/media/share/status/${encodeURIComponent(first.task_id)}`);
          last = await statusResp.json().catch(() => ({ ok: false, status: "failed", message: statusResp.statusText }));
          if (!statusResp.ok && last.status !== "failed") {
            last = { ...last, status: "failed", message: last.message || "无法读取共享进度" };
          }
        }
      } catch (error) {
        renderTransfer({
          status: "failed",
          badge: "失败",
          title: "共享失败",
          target: targetLabel,
          message: friendlyError(error, "共享失败"),
        });
      } finally {
        if (refs.pushSelectedBtn) refs.pushSelectedBtn.disabled = false;
      }
    }

    async function previewTune() {
      if (refs.commandAdvanced) refs.commandAdvanced.open = true;
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
      if (refs.commandAdvanced) refs.commandAdvanced.open = true;
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
      const youtubeApiMode = payload.stream_output_mode === "youtube_api";
      const targetMissing = (!relayMode && !youtubeApiMode && !payload.stream_key)
        || (youtubeApiMode && !payload.youtube_stream_id);
      if (!payload.node_id || !payload.video_path || targetMissing) {
        refs.tuneBox.textContent = relayMode
          ? "请先选择节点和服务器视频，并确认本地中继可用。"
          : youtubeApiMode
            ? "请先通过 YouTube 向导授权频道并选择直播流。"
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
        if (data.ok) refs.streamKeyInput.value = "";
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
        headers: authHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify(payload),
      });
      const data = await resp.json();
      refs.updateBox.textContent = JSON.stringify(data, null, 2);
      return data;
    }

    async function postJson(path, payload) {
      const resp = await fetch(path, {
        method: "POST",
        headers: authHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify(payload),
      });
      const data = await resp.json().catch(() => ({ ok: false, message: resp.statusText }));
      if (!resp.ok && data.ok !== false) data.ok = false;
      return data;
    }

    async function handleMediaAction(action, row) {
      const nodeId = row.dataset.nodeId || selectedNodeId;
      const mediaName = row.dataset.mediaName || "";
      const videoPath = row.dataset.videoPath || mediaName;
      const node = nodes.find((item) => String(item.id) === String(nodeId));
      const nodeLabel = node?.name || nodeId;
      if (action === "inspect") {
        const input = row.querySelector("[data-media-check]");
        if (input) input.checked = true;
        renderTransfer({
          status: "done",
          badge: "详情",
          title: "Agent 文件详情",
          target: nodeLabel,
          percent: 100,
          doneBytes: Number(row.dataset.size || 0),
          totalBytes: Number(row.dataset.size || 0),
          message: `${mediaName} | ${row.dataset.modifiedLabel || "--"} | ${videoPath}`,
        });
        return;
      }
      if (action === "use") {
        rememberSelectedNode(nodeId);
        const input = row.querySelector("[data-media-check]");
        if (input) input.checked = true;
        renderTransfer({
          status: "done",
          badge: "已选用",
          title: "已选用服务器视频",
          target: nodeLabel,
          percent: 100,
          message: `${mediaName} 已放入当前 Agent 的开播选择。`,
        });
        renderNodes();
        renderMedia();
        renderStreamControls();
        refs.streamVideoSelect.value = videoPath;
        return;
      }
      if (action === "rename") {
        const nextName = prompt("输入新的文件名，保留 .mp4/.mov/.mkv/.m4v/.webm 后缀：", mediaName);
        if (!nextName || nextName === mediaName) return;
        const data = await postJson("/api/nodes/media/rename", {
          node_id: nodeId,
          media: videoPath,
          new_name: nextName,
        });
        renderTransfer({
          status: data.ok ? "done" : "failed",
          badge: data.ok ? "完成" : "失败",
          title: data.ok ? "重命名完成" : "重命名失败",
          target: nodeLabel,
          percent: data.ok ? 100 : 0,
          message: data.ok ? `${mediaName} 已改名为 ${data.name || nextName}。` : friendlyError(data.message || data.error || "重命名失败"),
        });
        await refreshAll();
        return;
      }
      if (action === "delete") {
        if (!confirm(`确认删除 ${mediaName}？\n\n只删除当前 Agent 上的这个视频，不会影响其他 Agent。`)) return;
        const data = await postJson("/api/nodes/media/delete", {
          node_id: nodeId,
          media: videoPath,
        });
        renderTransfer({
          status: data.ok ? "done" : "failed",
          badge: data.ok ? "完成" : "失败",
          title: data.ok ? "删除完成" : "删除失败",
          target: nodeLabel,
          percent: data.ok ? 100 : 0,
          message: data.ok ? `${data.name || mediaName} 已从 ${nodeLabel} 删除。` : friendlyError(data.message || data.error || "删除失败"),
        });
        await refreshAll();
      }
    }

    function selectMediaRow(row) {
      document.querySelectorAll("[data-media-row].selected").forEach((item) => item.classList.remove("selected"));
      row.classList.add("selected");
      const input = row.querySelector("[data-media-check]");
      if (input) input.checked = true;
    }

    function hideMediaMenu() {
      refs.mediaContextMenu.classList.remove("open");
      refs.mediaContextMenu.style.left = "";
      refs.mediaContextMenu.style.top = "";
      contextMediaRow = null;
    }

    function showMediaMenu(event, row) {
      event.preventDefault();
      selectMediaRow(row);
      contextMediaRow = row;
      refs.mediaContextMenu.classList.add("open");
      const menuWidth = refs.mediaContextMenu.offsetWidth || 160;
      const menuHeight = refs.mediaContextMenu.offsetHeight || 170;
      const left = Math.min(event.clientX, window.innerWidth - menuWidth - 8);
      const top = Math.min(event.clientY, window.innerHeight - menuHeight - 8);
      refs.mediaContextMenu.style.left = `${Math.max(8, left)}px`;
      refs.mediaContextMenu.style.top = `${Math.max(8, top)}px`;
    }

    async function handleNodeAction(action, nodeId) {
      const node = nodes.find((item) => String(item.id) === String(nodeId));
      const nodeName = node?.name || nodeId;
      if (action === "stop-stream") {
        if (!confirm(`确认停止 ${nodeName} 的推流？`)) {
          return;
        }
        log(`请求停止推流：${nodeName}`);
        const data = await postNodeAction("/api/nodes/stop-stream", { node_id: nodeId });
        log(data.ok ? `推流已停止：${nodeName}` : `停止推流失败：${data.message || nodeName}`);
        await refreshAll();
        return;
      }
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
        return;
      }
    }

    let roleSettingsNodeId = "";

    function setRoleSettingsOpen(open, nodeId = "") {
      roleSettingsNodeId = open ? String(nodeId) : "";
      refs.roleSettingsModal.classList.toggle("open", open);
      refs.roleSettingsModal.setAttribute("aria-hidden", open ? "false" : "true");
      if (!open) return;
      const node = nodes.find((item) => String(item.id) === roleSettingsNodeId);
      if (!node) return setRoleSettingsOpen(false);
      refs.roleSettingsTitle.textContent = `${node.name || node.id} · 角色设置`;
      refs.roleSettingsSummary.textContent = "角色维护功能不会直接执行；选择后还需通过保护确认。";
      refs.roleSettingsActions.innerHTML = ["agent", "hub"].map((role) => {
        const info = node.roles?.[role] || {};
        const enabled = Boolean(info.enabled);
        const label = role === "hub" ? "Hub" : "Agent";
        return `<div class="role-settings-item">
          <span><strong>${label}</strong><small>当前状态：${enabled ? `已激活 · 版本 ${escapeHtml(info.version || "未识别")}` : "未激活"}</small></span>
          <button class="${enabled ? "" : "primary"}" data-settings-role="${role}" data-role-action="${enabled ? "upgrade-role" : "activate-role"}">${enabled ? `升级 ${label}` : `激活 ${label}`}</button>
        </div>`;
      }).join("");
    }

    async function handleRoleAction(action, role, nodeId, sourceButton) {
      const node = nodes.find((item) => String(item.id) === String(nodeId));
      const nodeName = node?.name || nodeId;
      const roleLabel = role === "hub" ? "Hub" : "Agent";
      if (action === "switch-hub") {
        const url = node?.roles?.hub?.url;
        if (url) window.location.href = url;
        return;
      }
      const activating = action === "activate-role";
      const roleInfo = node?.roles?.[role] || {};
      const currentStatus = roleInfo.enabled ? `已激活，当前版本 ${roleInfo.version || "未识别"}` : "未激活";
      const warning = activating
        ? `${nodeName} 的 ${roleLabel} 当前状态：${currentStatus}。\n\n是否确认激活 ${roleLabel}？\n\n安全提示：将新增并启用独立 systemd 服务，开放 Tailscale ${role === "hub" ? "8788" : "8787"} 端口。现有 ${role === "hub" ? "Agent" : "Hub"} 会继续运行，配置与视频不会删除。`
        : `${nodeName} 的 ${roleLabel} 当前状态：${currentStatus}。\n\n是否确认升级 ${roleLabel}？\n\n系统会从 GitHub main 拉取最新版，只重启该角色，不停止另一个角色。`;
      if (!confirm(warning)) return;
      setRoleSettingsOpen(false);
      if (sourceButton) sourceButton.disabled = true;
      const path = activating ? `/api/nodes/roles/${role}/activate` : `/api/nodes/roles/${role}/upgrade`;
      const data = await postNodeAction(path, { node_id: nodeId });
      refs.updateBox.textContent = JSON.stringify(data, null, 2);
      log(data.ok ? `${roleLabel} 任务已提交：${nodeName}` : `${roleLabel} 操作失败：${data.message || nodeName}`);
      if (data.ok) {
        await new Promise((resolve) => setTimeout(resolve, 8000));
        await refreshAll();
      } else if (sourceButton) {
        sourceButton.disabled = false;
      }
    }

    refs.nodeList.addEventListener("click", (event) => {
      const settingsButton = event.target.closest("[data-role-settings]");
      if (settingsButton) {
        event.preventDefault();
        event.stopPropagation();
        setRoleSettingsOpen(true, settingsButton.dataset.nodeId);
        return;
      }
      const roleButton = event.target.closest("[data-role-action]");
      if (roleButton) {
        event.preventDefault();
        event.stopPropagation();
        handleRoleAction(roleButton.dataset.roleAction, roleButton.dataset.role, roleButton.dataset.nodeId, roleButton);
        return;
      }
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
      rememberSelectedNode(row.dataset.nodeId);
      renderNodes();
      renderMedia();
      renderStreamControls();
    });
    refs.hubNodeList.addEventListener("click", (event) => {
      const settingsButton = event.target.closest("[data-role-settings]");
      if (settingsButton) {
        event.preventDefault();
        event.stopPropagation();
        setRoleSettingsOpen(true, settingsButton.dataset.nodeId);
        return;
      }
      const roleButton = event.target.closest("[data-role-action]");
      if (roleButton) {
        event.preventDefault();
        event.stopPropagation();
        handleRoleAction(roleButton.dataset.roleAction, roleButton.dataset.role || "hub", roleButton.dataset.nodeId, roleButton);
        return;
      }
      const row = event.target.closest("[data-hub-row]");
      if (!row) return;
      const node = nodes.find((item) => String(item.id) === String(row.dataset.nodeId));
      if (node?.roles?.hub?.enabled && node.roles.hub.url) window.location.href = node.roles.hub.url;
    });
    refs.roleSettingsClose.addEventListener("click", () => setRoleSettingsOpen(false));
    refs.roleSettingsModal.addEventListener("click", (event) => {
      if (event.target === refs.roleSettingsModal) {
        setRoleSettingsOpen(false);
        return;
      }
      const button = event.target.closest("[data-settings-role]");
      if (!button) return;
      handleRoleAction(button.dataset.roleAction, button.dataset.settingsRole, roleSettingsNodeId, button);
    });
    refs.mediaList.addEventListener("click", (event) => {
      hideMediaMenu();
      const row = event.target.closest("[data-media-row]");
      if (!row) return;
      selectMediaRow(row);
    });
    refs.mediaList.addEventListener("dblclick", (event) => {
      const row = event.target.closest("[data-media-row]");
      if (row) handleMediaAction("inspect", row);
    });
    refs.mediaList.addEventListener("keydown", (event) => {
      if (event.key !== "Enter" && event.key !== " ") return;
      const row = event.target.closest("[data-media-row]");
      if (!row) return;
      event.preventDefault();
      selectMediaRow(row);
      handleMediaAction("inspect", row);
    });
    refs.mediaList.addEventListener("contextmenu", (event) => {
      const row = event.target.closest("[data-media-row]");
      if (!row) return;
      showMediaMenu(event, row);
    });
    refs.mediaContextMenu.addEventListener("click", (event) => {
      const button = event.target.closest("[data-media-menu-action]");
      if (!button || !contextMediaRow) return;
      const row = contextMediaRow;
      const action = button.dataset.mediaMenuAction;
      hideMediaMenu();
      if (action === "share") {
        selectMediaRow(row);
        pushSelectedMedia();
      } else {
        handleMediaAction(action, row);
      }
    });
    document.addEventListener("click", (event) => {
      if (!event.target.closest("#mediaContextMenu")) hideMediaMenu();
    });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape") hideMediaMenu();
    });
    refs.refreshBtn.addEventListener("click", refreshAll);
    refs.uploadBtn.addEventListener("click", uploadMedia);
    refs.cancelUploadBtn.addEventListener("click", cancelActiveUpload);
    refs.checkUpdatesBtn.addEventListener("click", checkUpdates);
    refs.policyBtn.addEventListener("click", showPolicy);
    refs.auditBtn.addEventListener("click", showAudit);
    refs.tailscaleWizardBtn.addEventListener("click", () => setTailscaleWizardOpen(true));
    refs.tailscaleWizardClose.addEventListener("click", () => setTailscaleWizardOpen(false));
    refs.tailscaleWizardModal.addEventListener("click", (event) => {
      if (event.target === refs.tailscaleWizardModal) setTailscaleWizardOpen(false);
    });
    refs.youtubeWizardBtn.addEventListener("click", () => setYouTubeModalOpen(true));
    refs.youtubeWizardClose.addEventListener("click", () => setYouTubeModalOpen(false));
    refs.youtubeWizardModal.addEventListener("click", (event) => {
      if (event.target === refs.youtubeWizardModal) setYouTubeModalOpen(false);
    });
    refs.youtubeRefreshBtn.addEventListener("click", refreshYouTubeResources);
    refs.youtubeSaveConfigBtn.addEventListener("click", saveYouTubeConfig);
    refs.youtubeAuthorizeBtn.addEventListener("click", startYouTubeAuthorization);
    refs.youtubePrepareBtn.addEventListener("click", prepareYouTubeBroadcast);
    refs.youtubeRevokeBtn.addEventListener("click", revokeYouTubeAuthorization);
    refs.tailscalePrecheckBtn.addEventListener("click", precheckTailscale);
    refs.tailscaleInstallBtn.addEventListener("click", installTailscale);
    refs.tailscaleStatusBtn.addEventListener("click", showTailscaleStatus);
    refs.tailscaleConnectBtn.addEventListener("click", connectTailscale);
    refs.tailscaleVerifyBtn.addEventListener("click", verifyTailscale);
    refs.tailscaleUseExistingIpBtn.addEventListener("click", connectExistingTailscaleIp);
    if (refs.pushSelectedBtn) refs.pushSelectedBtn.addEventListener("click", pushSelectedMedia);
    refs.previewTuneBtn.addEventListener("click", previewTune);
    refs.applyTuneBtn.addEventListener("click", applyLastTune);
    refs.smartStartBtn.addEventListener("click", smartStart);
    refs.streamOutputModeInput.addEventListener("change", syncStreamOutputMode);
    refs.streamUrlInput.addEventListener("input", () => { refs.streamUrlInput.dataset.userEdited = "1"; });
    [refs.presetInput, refs.videoBitrateInput, refs.audioBitrateInput, refs.fpsInput, refs.resolutionInput, refs.keyframeInput].forEach((el) => {
      el.addEventListener("input", () => { refs.tuneBox.dataset.copyMode = "0"; });
    });
    refreshAll();
  </script>
</body>
</html>
"""


@APP.get("/")
def index():
    return HTML


def is_private_or_loopback_host(hostname: str) -> bool:
    if not hostname:
        return False
    if hostname in {"localhost", "127.0.0.1", "::1"}:
        return True
    try:
        ip = ipaddress.ip_address(hostname.split("%", 1)[0])
    except ValueError:
        return hostname.endswith(".local") or hostname.endswith(".ts.net") or hostname.endswith(".beta.tailscale.net")
    return ip.is_loopback or ip.is_private or ip in TAILSCALE_CGNAT


def request_is_local() -> bool:
    remote_addr = request.remote_addr or ""
    try:
        remote_ip = ipaddress.ip_address(remote_addr.split("%", 1)[0])
    except ValueError:
        return False
    return remote_ip.is_loopback


def request_control_token() -> str:
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth.split(" ", 1)[1].strip()
    return request.headers.get("X-Control-Token", "").strip()


def has_valid_control_token() -> bool:
    return bool(CONTROL_TOKEN and hmac.compare_digest(request_control_token(), CONTROL_TOKEN))


def write_request_allowed() -> bool:
    if request_is_local() or has_valid_control_token():
        return True
    return TRUSTED_REMOTE_WRITES


def dangerous_local_action_allowed() -> bool:
    return request_is_local() or has_valid_control_token() or TRUSTED_REMOTE_WRITES


def reject_forbidden(message: str = "control token or localhost access required"):
    return jsonify({"ok": False, "message": message}), 403


@APP.before_request
def protect_write_requests():
    if request.method in {"GET", "HEAD", "OPTIONS"}:
        return None
    if not write_request_allowed():
        return reject_forbidden()
    origin = request.headers.get("Origin")
    if origin:
        parsed = urlparse(origin)
        host = parsed.hostname or ""
        if not is_private_or_loopback_host(host) and not has_valid_control_token():
            return reject_forbidden("cross-origin write requests require STREAM_HUB_CONTROL_TOKEN")
    return None


def redact_secret(value: str, *secrets: str) -> str:
    redacted = value
    for secret in secrets:
        if secret:
            redacted = redacted.replace(secret, "[redacted]")
    return redacted


def run_command(args: list[str], timeout: int = 60, secrets: list[str] | None = None) -> dict[str, Any]:
    if not args:
        return {"ok": False, "message": "missing command"}
    if not shutil.which(args[0]):
        return {"ok": False, "message": f"{args[0]} is not installed"}
    try:
        proc = subprocess.run(args, text=True, capture_output=True, timeout=timeout)
    except Exception as exc:
        return {"ok": False, "message": str(exc)}
    secret_values = secrets or []
    return {
        "ok": proc.returncode == 0,
        "exit_code": proc.returncode,
        "stdout": redact_secret(proc.stdout.strip(), *secret_values),
        "stderr": redact_secret(proc.stderr.strip(), *secret_values),
    }


def run_helper_script(
    script: Path,
    args: list[str],
    timeout: int = 60,
    env: dict[str, str] | None = None,
    secrets: list[str] | None = None,
) -> dict[str, Any]:
    if not script.exists():
        return {"ok": False, "message": f"helper script missing: {script}"}
    if not shutil.which("sh"):
        return {"ok": False, "message": "sh is not available"}
    proc_env = os.environ.copy()
    if env:
        proc_env.update(env)
    try:
        proc = subprocess.run(
            ["sh", str(script), *args],
            text=True,
            capture_output=True,
            timeout=timeout,
            env=proc_env,
        )
    except Exception as exc:
        return {"ok": False, "message": str(exc)}
    secret_values = secrets or []
    return {
        "ok": proc.returncode == 0,
        "exit_code": proc.returncode,
        "stdout": redact_secret(proc.stdout.strip(), *secret_values),
        "stderr": redact_secret(proc.stderr.strip(), *secret_values),
    }


def tailscale_status() -> dict[str, Any]:
    if TAILSCALE_HELPER.exists():
        helper = run_helper_script(TAILSCALE_HELPER, ["status"], timeout=15)
        if helper.get("ok"):
            try:
                data = json.loads(helper.get("stdout") or "{}")
            except json.JSONDecodeError:
                data = {}
            if data:
                return tailscale_status_from_json(data)
        if "not installed" in str(helper.get("stdout") or helper.get("stderr") or helper.get("message") or "").lower():
            return {"ok": False, "installed": False, "message": "tailscale is not installed"}
    result = run_command(["tailscale", "status", "--json"], timeout=15)
    if not result.get("ok"):
        return result
    try:
        data = json.loads(result.get("stdout") or "{}")
    except json.JSONDecodeError:
        return {"ok": False, "message": "tailscale returned invalid json"}
    return tailscale_status_from_json(data)


def tailscale_status_from_json(data: dict[str, Any]) -> dict[str, Any]:
    self_info = data.get("Self") or {}
    return {
        "ok": True,
        "installed": True,
        "backend_state": data.get("BackendState"),
        "self": {
            "host_name": self_info.get("HostName"),
            "dns_name": self_info.get("DNSName"),
            "tailscale_ips": self_info.get("TailscaleIPs") or [],
            "online": self_info.get("Online"),
        },
        "peers": [
            {
                "host_name": peer.get("HostName"),
                "dns_name": peer.get("DNSName"),
                "tailscale_ips": peer.get("TailscaleIPs") or [],
                "online": peer.get("Online"),
                "last_seen": peer.get("LastSeen"),
            }
            for peer in (data.get("Peer") or {}).values()
        ],
    }


def tailscale_precheck() -> dict[str, Any]:
    result = run_helper_script(TAILSCALE_HELPER, ["precheck"], timeout=60)
    payload: dict[str, Any] = {"ok": False, "message": result.get("message") or "Tailscale precheck failed"}
    with suppress(json.JSONDecodeError):
        payload = json.loads(result.get("stdout") or "{}")
    payload["result"] = result
    return payload


def tailscale_install() -> dict[str, Any]:
    precheck = tailscale_precheck()
    result = run_helper_script(TAILSCALE_HELPER, ["install"], timeout=600)
    return {
        "ok": bool(result.get("ok")),
        "message": "Tailscale install/fix complete" if result.get("ok") else "Tailscale install/fix failed",
        "precheck": precheck,
        "result": result,
        "status": tailscale_status() if result.get("ok") else None,
    }


def tailscale_connect(auth_key: str, hostname: str, *, accept_routes: bool = True, ssh: bool = False) -> dict[str, Any]:
    env = {
        "TAILSCALE_AUTH_KEY": auth_key,
        "TAILSCALE_HOSTNAME": hostname,
        "TAILSCALE_ACCEPT_ROUTES": "1" if accept_routes else "0",
        "TAILSCALE_SSH": "1" if ssh else "0",
    }
    precheck = tailscale_precheck()
    result = run_helper_script(TAILSCALE_HELPER, ["connect"], timeout=600, env=env, secrets=[auth_key])
    if not result.get("ok") and "sh is not available" in str(result.get("message") or ""):
        args = [
            "tailscale",
            "up",
            "--auth-key",
            auth_key,
            "--hostname",
            hostname,
            "--accept-dns=false",
        ]
        if accept_routes:
            args.append("--accept-routes")
        if ssh:
            args.append("--ssh")
        result = run_command(args, timeout=90, secrets=[auth_key])
    status = tailscale_status() if result.get("ok") else None
    return {
        "ok": bool(result.get("ok")),
        "message": "Tailscale connected" if result.get("ok") else "Tailscale connect failed",
        "precheck": precheck,
        "result": result,
        "status": status,
    }


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


def save_nodes(nodes: list[dict[str, Any]]) -> None:
    ensure_dirs()
    NODES_FILE.write_text(json.dumps(nodes, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


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
        resp = requests.get(f"{base_url}{path}", headers=node_headers(node), timeout=timeout)
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


def node_role_urls(node: dict[str, Any]) -> dict[str, str]:
    base_url = node_base_url(node)
    parsed = urlparse(base_url)
    host = parsed.hostname or ""
    if not host:
        return {"agent": base_url, "hub": ""}
    host_label = f"[{host}]" if ":" in host else host
    return {
        "agent": base_url or f"http://{host_label}:8787",
        "hub": str(node.get("hub_url") or f"http://{host_label}:8788").rstrip("/"),
    }


def request_hub_role_status(node: dict[str, Any]) -> dict[str, Any]:
    hub_url = node_role_urls(node)["hub"]
    if not hub_url:
        return {"ok": False, "enabled": False, "message": "missing Hub URL"}
    try:
        response = requests.get(f"{hub_url}/api/role-status", timeout=3)
        data = response.json()
        hub = (data.get("roles") or {}).get("hub") or {}
        return {"ok": response.ok, "enabled": response.ok and bool(hub.get("enabled", True)), "url": hub_url, **hub}
    except Exception as exc:
        return {"ok": False, "enabled": False, "url": hub_url, "message": str(exc)}


def schedule_agent_role_activation(control_hub_url: str) -> dict[str, Any]:
    if not shutil.which("systemd-run"):
        raise RuntimeError("systemd-run is required to activate the Agent role")
    unit = f"stream-control-agent-activate-{int(time.time())}"
    root = shlex.quote(str(ROOT))
    control_hub = shlex.quote(control_hub_url)
    script = f"set -eu; sleep 2; env STREAM_AGENT_CONTROL_HUB={control_hub} CHOICE=1 sh {root}/scripts/install-agent.sh"
    result = subprocess.run(
        ["systemd-run", "--unit", unit, "--collect", "--no-block", "/bin/sh", "-c", script],
        text=True,
        capture_output=True,
        timeout=15,
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "failed to schedule Agent activation").strip())
    return {"unit": unit, "role": "agent", "control_hub": control_hub_url}


def schedule_hub_upgrade() -> dict[str, Any]:
    if not shutil.which("systemd-run") or not (ROOT / ".git").exists():
        raise RuntimeError("Hub must be a Git-managed systemd installation")
    unit = f"stream-control-hub-upgrade-{int(time.time())}"
    root = shlex.quote(str(ROOT))
    script = (
        "set -eu; sleep 2; "
        f"git -C {root} fetch origin main; git -C {root} checkout main; "
        f"git -C {root} pull --ff-only origin main; env BRANCH=main CHOICE=1 "
        f"STREAM_HUB_SUPPRESS_TOKEN_OUTPUT=1 sh {root}/scripts/install-hub.sh"
    )
    result = subprocess.run(
        ["systemd-run", "--unit", unit, "--collect", "--no-block", "/bin/sh", "-c", script],
        text=True,
        capture_output=True,
        timeout=15,
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "failed to schedule Hub upgrade").strip())
    return {"unit": unit, "role": "hub", "from_version": local_git_version(), "target_branch": "main"}


def node_upload_base_urls(node: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for key in ("upload_base_url", "public_base_url"):
        value = str(node.get(key) or "").strip().rstrip("/")
        if value:
            values.append(value)
    for value in node.get("upload_base_urls") or []:
        value = str(value or "").strip().rstrip("/")
        if value:
            values.append(value)
    base_url = node_base_url(node)
    if base_url:
        values.append(base_url)
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value not in seen:
            result.append(value)
            seen.add(value)
    return result


def safe_media_filename(value: str) -> str:
    raw = Path(str(value or "").strip()).name
    name = secure_filename(raw)
    suffix = Path(raw).suffix.lower()
    if suffix not in ALLOWED_MEDIA_EXTENSIONS and Path(name).suffix.lower() not in ALLOWED_MEDIA_EXTENSIONS:
        raise ValueError("unsupported media extension")
    if not name or Path(name).suffix.lower() not in ALLOWED_MEDIA_EXTENSIONS:
        name = f"upload-{uuid.uuid4().hex}{suffix}"
    if not media_allowed(name):
        raise ValueError("unsupported media extension")
    return name


def upload_route_label(url: str, base_url: str) -> str:
    try:
        host = urlparse(url).hostname or ""
        ip = ipaddress.ip_address(host.split("%", 1)[0])
        if ip in TAILSCALE_CGNAT:
            return "Tailscale 兜底"
        if ip.is_private or ip.is_loopback:
            return "内网直连"
    except ValueError:
        if host.endswith(".ts.net") or host.endswith(".beta.tailscale.net"):
            return "Tailscale 兜底"
    return "公网直连" if url != base_url else "默认线路"


def node_headers(node: dict[str, Any]) -> dict[str, str]:
    token = str(node.get("token") or node.get("control_token") or "").strip()
    return {"X-Control-Token": token} if token else {}


def post_node_json(node: dict[str, Any], path: str, payload: dict[str, Any], *, timeout: int = 15) -> dict[str, Any]:
    base_url = node_base_url(node)
    if not base_url:
        return {"ok": False, "message": "missing node base_url"}
    try:
        resp = requests.post(f"{base_url}{path}", json=payload, headers=node_headers(node), timeout=timeout)
        try:
            data = resp.json()
        except ValueError:
            data = {"message": resp.text[:500]}
        data["ok"] = resp.ok and bool(data.get("ok", False))
        data.setdefault("status_code", resp.status_code)
        return data
    except Exception as exc:
        return {"ok": False, "message": str(exc)}


def post_url_json(url: str, payload: dict[str, Any], *, timeout: int = 30) -> dict[str, Any]:
    try:
        response = requests.post(url, json=payload, timeout=timeout)
        try:
            data = response.json()
        except ValueError:
            data = {"message": response.text[:500]}
        data["ok"] = response.ok and bool(data.get("ok", False))
        data.setdefault("status_code", response.status_code)
        return data
    except Exception as exc:
        return {"ok": False, "message": str(exc)}


def request_node_upload_ticket(node: dict[str, Any], *, upload_id: str, filename: str, total_size: int) -> dict[str, Any]:
    return post_node_json(
        node,
        "/api/upload-ticket",
        {"upload_id": upload_id, "filename": filename, "total_size": total_size},
        timeout=10,
    )


def request_node_media_info(node: dict[str, Any], media: str) -> dict[str, Any]:
    status = request_node_json(node, "/api/status", timeout=10)
    if not status.get("ok"):
        return {"ok": False, "message": status.get("message") or "source node status unavailable"}
    media_name = Path(media).name
    for item in status.get("videos") or []:
        values = {str(item.get("name") or ""), str(item.get("video_path") or ""), str(item.get("path") or "")}
        if media in values or media_name in values:
            return {"ok": True, **item}
    return {"ok": False, "message": "media not found on source node"}


def share_task_snapshot(task_id: str) -> dict[str, Any] | None:
    with SHARE_TASKS_LOCK:
        task = SHARE_TASKS.get(task_id)
        return dict(task) if task else None


def update_share_task(task_id: str, **updates: Any) -> None:
    with SHARE_TASKS_LOCK:
        task = SHARE_TASKS.get(task_id)
        if not task:
            return
        task.update(updates)
        task["updated_at"] = time.time()


def share_task_payload(task: dict[str, Any]) -> dict[str, Any]:
    total = int(task.get("total_bytes") or 0)
    done = int(task.get("done_bytes") or 0)
    average_bps = int(task.get("average_bps") or 0)
    eta = int((total - done) / average_bps) if total and average_bps > 0 and done < total else 0
    return {
        "ok": task.get("status") != "failed",
        "task_id": task.get("task_id"),
        "status": task.get("status"),
        "message": task.get("message") or "",
        "source_node_id": task.get("source_node_id"),
        "target_node_ids": task.get("target_node_ids") or [],
        "media": task.get("media"),
        "done_bytes": done,
        "total_bytes": total,
        "percent": round((done / total) * 100, 2) if total else 0,
        "current_bps": int(task.get("current_bps") or 0),
        "average_bps": average_bps,
        "eta_seconds": eta,
        "results": task.get("results") or [],
        "error": task.get("error") or "",
    }


def run_share_task(
    task_id: str,
    source_node: dict[str, Any],
    target_nodes: list[dict[str, Any]],
    media: str,
    progress_url: str,
) -> None:
    started_at = time.time()
    results: list[dict[str, Any]] = []
    try:
        for target_index, target_node in enumerate(target_nodes):
            target_node_id = str(target_node.get("id") or "")
            previous_task = share_task_snapshot(task_id) or {}
            previous_total = int(previous_task.get("single_target_total_bytes") or previous_task.get("total_bytes") or 0)
            media_info = request_node_media_info(source_node, media)
            if not media_info.get("ok"):
                raise RuntimeError(media_info.get("message") or "source media not found")
            filename = str(media_info.get("name") or Path(media).name)
            total_size = int(media_info.get("size") or 0)
            if total_size <= 0:
                raise RuntimeError("source media size is unavailable")
            upload_id = f"share_{uuid.uuid4().hex}"
            ticket = request_node_upload_ticket(target_node, upload_id=upload_id, filename=filename, total_size=total_size)
            if not ticket.get("ok"):
                raise RuntimeError(ticket.get("message") or f"{target_node_id} did not issue an upload ticket")
            upload_urls = node_upload_base_urls(target_node)
            target_upload_base_urls = upload_urls or [node_base_url(target_node)]
            update_share_task(
                task_id,
                status="running",
                message=f"正在共享到 {target_node.get('name') or target_node_id}",
                done_bytes=previous_total * target_index if previous_total else int(previous_task.get("done_bytes") or 0),
                total_bytes=previous_total * len(target_nodes) if previous_total else int(previous_task.get("total_bytes") or 0),
            )
            share_payload = {
                "media": media,
                "target_base_url": target_upload_base_urls[0],
                "target_base_urls": target_upload_base_urls,
                "upload_id": upload_id,
                "target_upload_ticket": str(ticket.get("ticket") or ""),
                "progress_url": progress_url,
                "progress_task_id": task_id,
                "progress_target_index": target_index,
                "progress_target_count": len(target_nodes),
                "progress_target_node_id": target_node_id,
            }
            result = post_node_json(source_node, "/api/share-media", share_payload, timeout=1800)
            result["node_id"] = target_node_id
            results.append(result)
            if not result.get("ok"):
                raise RuntimeError(result.get("message") or f"{target_node_id} 共享失败")
        elapsed = max(0.001, time.time() - started_at)
        task = share_task_snapshot(task_id) or {}
        total = int(task.get("total_bytes") or 0)
        update_share_task(
            task_id,
            status="done",
            done_bytes=total or int(task.get("done_bytes") or 0),
            current_bps=0,
            average_bps=int((total or int(task.get("done_bytes") or 0)) / elapsed),
            message="共享完成",
            results=results,
        )
    except Exception as exc:
        update_share_task(
            task_id,
            status="failed",
            message="共享失败",
            error=str(exc),
            results=results,
        )


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
        "headers": node_headers(node),
        "opened_public_window": False,
        "chunk_bytes": NODE_UPLOAD_CHUNK_BYTES,
        "public_status": {},
        "warnings": list(warnings or []),
        "decision_log": [],
        "last_heartbeat_at": 0.0,
        "probe": {"ok": True, "skipped": True, "message": "internal fallback"},
        "fallback_from": "",
    }


def select_node_upload_route(
    node: dict[str, Any],
    *,
    upload_id: str,
    filename: str,
    total_size: int,
) -> dict[str, Any]:
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
    supports_window = bool(status.get("window_supported"))
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
                    "headers": {
                        **node_headers(node),
                        **({"X-Public-Upload-Token": token} if token else {}),
                    },
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

    if public_origin:
        headers = node_headers(node)
        if restrict_public or bool(status.get("ticket_required")):
            ticket = request_node_upload_ticket(node, upload_id=upload_id, filename=filename, total_size=total_size)
            if not ticket.get("ok"):
                route["warnings"].append(ticket.get("message") or "failed to issue public upload ticket")
                route["decision_log"].append("public upload ticket unavailable; using internal route")
                fallback_route = make_internal_upload_route(node, route["warnings"])
                fallback_route["decision_log"] = [*route.get("decision_log", []), "internal fallback selected after failed ticket issue"]
                return fallback_route
            ticket_value = str(ticket.get("ticket") or "")
            if not ticket_value:
                route["warnings"].append("public upload ticket response did not include a ticket")
                route["decision_log"].append("public upload ticket missing; using internal route")
                fallback_route = make_internal_upload_route(node, route["warnings"])
                fallback_route["decision_log"] = [*route.get("decision_log", []), "internal fallback selected after missing ticket"]
                return fallback_route
            headers = {"X-Upload-Ticket": ticket_value}
            route["token"] = ticket_value
            route["decision_log"].append("public upload ticket issued; probing public route")
        route["decision_log"].append(
            "probing discovered public route with upload ticket"
            if restrict_public
            else "public origin is unrestricted; probing direct public route"
        )
        route.update({
            "upload_base_url": public_origin,
            "route": "public-direct",
            "route_label": "public direct",
            "headers": headers,
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
        route = select_node_upload_route(node, upload_id=upload_id, filename=media_path.name, total_size=total_size)
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
        node_view.pop("token", None)
        node_view.pop("control_token", None)
        node_view["health"] = request_node_json(node, "/api/status") if node.get("enabled", True) else {"ok": False}
        urls = node_role_urls(node)
        agent_health = node_view["health"]
        agent_info = agent_health.get("agent") or {}
        node_view["roles"] = {
            "agent": {
                "enabled": bool(agent_health.get("ok")),
                "version": str(agent_info.get("version") or "unrecognized"),
                "url": urls["agent"],
            },
            "hub": request_hub_role_status(node),
        }
        result.append(node_view)
    return jsonify(result)


@APP.get("/api/role-status")
def api_hub_role_status():
    host = (request.host.split(":", 1)[0] or "127.0.0.1").strip("[]")
    return jsonify({
        "ok": True,
        "roles": {
            "hub": {"enabled": True, "version": local_git_version(), "url": f"http://{host}:{PORT}"},
            "agent": {"enabled": service_active("stream-control-headless-agent.service"), "url": f"http://{host}:8787"},
        },
    })


@APP.post("/api/roles/agent/activate")
def api_activate_agent_role():
    payload = request.get_json(silent=True) or {}
    control_hub_url = str(payload.get("control_hub_url") or request.host_url.rstrip("/")).strip().rstrip("/")
    parsed = urlparse(control_hub_url)
    if not parsed.hostname or not is_private_or_loopback_host(parsed.hostname):
        return jsonify({"ok": False, "message": "control_hub_url must use a private or Tailscale address"}), 400
    try:
        result = schedule_agent_role_activation(control_hub_url)
    except Exception as exc:
        return jsonify({"ok": False, "message": str(exc)}), 409
    return jsonify({"ok": True, "accepted": True, "message": "Agent activation scheduled; Hub remains active", "result": result}), 202


@APP.post("/api/upgrade")
def api_upgrade_hub():
    try:
        result = schedule_hub_upgrade()
    except Exception as exc:
        return jsonify({"ok": False, "message": str(exc)}), 409
    return jsonify({"ok": True, "accepted": True, "message": "Hub upgrade scheduled; Agent remains active", "result": result}), 202


@APP.get("/api/media")
def api_media():
    return jsonify(list_media())


@APP.post("/api/nodes/upload-target")
def api_node_upload_target():
    payload = request.get_json(silent=True) or {}
    node_id = str(payload.get("node_id") or "").strip()
    upload_id = secure_filename(str(payload.get("upload_id") or "").strip())
    original_filename = str(payload.get("filename") or "").strip()
    try:
        filename = safe_media_filename(original_filename)
        total_size = int(payload.get("total_size") or 0)
    except Exception as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400
    node = node_by_id(node_id)
    if not node:
        return jsonify({"ok": False, "message": "node not found"}), 404
    if not node.get("enabled", True):
        return jsonify({"ok": False, "message": "node disabled"}), 400
    base_url = node_base_url(node)
    if not base_url:
        return jsonify({"ok": False, "message": "missing node base_url"}), 400
    if not upload_id or not filename or total_size <= 0:
        return jsonify({"ok": False, "message": "upload_id, filename and total_size are required"}), 400
    ticket = request_node_upload_ticket(node, upload_id=upload_id, filename=filename, total_size=total_size)
    if not ticket.get("ok"):
        return jsonify({
            "ok": False,
            "message": ticket.get("message") or "Agent did not issue an upload ticket",
            "status_code": ticket.get("status_code"),
        }), int(ticket.get("status_code") or 502)
    headers = {
        "X-Upload-Route": "direct-browser",
        "X-Upload-Ticket": str(ticket.get("ticket") or ""),
    }
    public_status = request_node_json(node, "/api/public-upload", timeout=10)
    discovered_public_url = (
        str(public_status.get("public_origin") or "").rstrip("/")
        if public_status.get("ok") and public_status.get("supported")
        else ""
    )
    upload_urls = []
    for url in [discovered_public_url, *node_upload_base_urls(node), base_url]:
        if url and url not in upload_urls:
            upload_urls.append(url)
    candidates = []
    for url in upload_urls:
        candidates.append({
            "url": url,
            "label": upload_route_label(url, base_url),
            "upload_url": f"{url}/api/upload-chunk",
            "probe_url": f"{url}/api/upload-probe",
            "cancel_url": f"{url}/api/upload-chunk/cancel",
            "headers": headers,
        })
    return jsonify({
        "ok": True,
        "node_id": node_id,
        "filename": filename,
        "original_filename": original_filename,
        "base_url": base_url,
        "upload_url": candidates[0]["upload_url"],
        "cancel_url": candidates[0]["cancel_url"],
        "probe_url": candidates[0]["probe_url"],
        "candidates": candidates,
        "chunk_bytes": DIRECT_AGENT_UPLOAD_CHUNK_BYTES,
        "headers": headers,
        "ticket_expires_in": ticket.get("expires_in"),
        "public_status": public_upload_summary(public_status),
    })


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


@APP.get("/api/tailscale/status")
def api_tailscale_status():
    return jsonify(tailscale_status())


@APP.get("/api/tailscale/precheck")
def api_tailscale_precheck():
    return jsonify(tailscale_precheck())


@APP.post("/api/tailscale/install")
def api_tailscale_install():
    if not dangerous_local_action_allowed():
        return reject_forbidden("Tailscale install requires localhost, trusted network, or STREAM_HUB_CONTROL_TOKEN")
    result = tailscale_install()
    return jsonify(result), 200 if result.get("ok") else 500


@APP.post("/api/tailscale/connect")
def api_tailscale_connect():
    if not dangerous_local_action_allowed():
        return reject_forbidden("Tailscale connect requires localhost, trusted network, or STREAM_HUB_CONTROL_TOKEN")
    payload = request.get_json(silent=True) or {}
    auth_key = str(payload.get("auth_key") or "").strip()
    hostname = secure_filename(str(payload.get("hostname") or "stream-control-hub").strip()) or "stream-control-hub"
    if not auth_key.startswith("tskey-"):
        return jsonify({"ok": False, "message": "valid Tailscale auth key required"}), 400
    result = tailscale_connect(
        auth_key,
        hostname,
        accept_routes=bool(payload.get("accept_routes", True)),
        ssh=bool(payload.get("ssh", False)),
    )
    return jsonify(result), 200 if result.get("ok") else 500


@APP.post("/api/tailscale/connect-existing-ip")
def api_tailscale_connect_existing_ip():
    if not dangerous_local_action_allowed():
        return reject_forbidden("Tailscale Agent 接入需要 localhost、可信网络或 STREAM_HUB_CONTROL_TOKEN")
    payload = request.get_json(silent=True) or {}
    node_id = str(payload.get("node_id") or "").strip()
    raw_ip = str(payload.get("tailscale_ip") or payload.get("ip") or "").strip()
    try:
        ip = ipaddress.ip_address(raw_ip.split("%", 1)[0])
    except ValueError:
        return jsonify({"ok": False, "message": "请输入有效的 Tailscale IP，例如 100.x.x.x"}), 400
    if ip not in TAILSCALE_CGNAT:
        return jsonify({"ok": False, "message": "这个地址不是 Tailscale 100.x 地址，请确认后再连接"}), 400

    base_url = f"http://{ip}:8787"
    nodes = load_nodes()
    target_index = next((index for index, item in enumerate(nodes) if str(item.get("id")) == node_id), -1) if node_id else -1
    creating = not node_id
    if node_id and target_index < 0:
        return jsonify({"ok": False, "message": "Agent 不存在"}), 404
    if creating:
        node_id = f"agent-{str(ip).replace('.', '-')}"
        target_index = next((index for index, item in enumerate(nodes) if str(item.get("id")) == node_id), -1)
        creating = target_index < 0
    node = dict(nodes[target_index]) if target_index >= 0 else {
        "id": node_id,
        "name": node_id,
        "role": "stream-node",
        "enabled": True,
    }
    previous_base_url = node_base_url(node)
    probe_node = dict(node)
    probe_node["base_url"] = base_url
    status = request_node_json(probe_node, "/api/status", timeout=12)
    if not status.get("ok"):
        status_code = int(status.get("status_code") or 502)
        message = status.get("message") or "无法连接到这个 Tailscale IP 上的 Agent"
        if status_code == 403:
            message = "Agent 已响应但未授权；请把 STREAM_AGENT_CONTROL_HUB 设置为当前 Hub 的 Tailscale URL"
        return jsonify({
            "ok": False,
            "message": message,
            "node_id": node_id,
            "base_url": base_url,
            "status_code": status_code,
        }), status_code

    if previous_base_url and previous_base_url != base_url and not node.get("public_base_url"):
        node["public_base_url"] = previous_base_url
    node["base_url"] = base_url
    agent = status.get("agent") if isinstance(status.get("agent"), dict) else {}
    if creating:
        node["name"] = str(agent.get("name") or status.get("hostname") or node_id)
    node["tailscale_ip"] = str(ip)
    node["tailscale_connected_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    if target_index >= 0:
        nodes[target_index] = node
    else:
        nodes.append(node)
    save_nodes(nodes)
    return jsonify({
        "ok": True,
        "message": "已有 Tailscale IP 验证成功，Agent 已接入",
        "node_id": node_id,
        "base_url": base_url,
        "previous_base_url": previous_base_url,
        "hostname": status.get("hostname"),
        "platform": status.get("platform"),
        "created": creating,
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
    if not name:
        return jsonify({"ok": False, "message": "invalid filename"}), 400
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


@APP.post("/api/media/share")
def api_media_share():
    payload = request.get_json(silent=True) or {}
    source_node_id = str(payload.get("source_node_id") or "").strip()
    target_node_ids = [str(item) for item in payload.get("target_node_ids") or []]
    media = str(payload.get("media") or payload.get("video_path") or "").strip()
    source_node = node_by_id(source_node_id)
    if not source_node:
        return jsonify({"ok": False, "message": "source node not found"}), 404
    if not source_node.get("enabled", True):
        return jsonify({"ok": False, "message": "source node disabled"}), 400
    if not target_node_ids:
        return jsonify({"ok": False, "message": "no target agents selected"}), 400
    if not media:
        return jsonify({"ok": False, "message": "no media selected"}), 400

    target_nodes = []
    for target_node_id in target_node_ids:
        if target_node_id == source_node_id:
            continue
        target_node = node_by_id(target_node_id)
        if not target_node:
            return jsonify({"ok": False, "message": f"target node not found: {target_node_id}"}), 404
        if not target_node.get("enabled", True):
            return jsonify({"ok": False, "message": f"target node disabled: {target_node_id}"}), 400
        target_nodes.append(target_node)
    if not target_nodes:
        return jsonify({"ok": False, "message": "no target agents selected"}), 400

    task_id = f"share_{uuid.uuid4().hex}"
    progress_url = request.host_url.rstrip("/") + f"/api/media/share/progress/{task_id}"
    with SHARE_TASKS_LOCK:
        SHARE_TASKS[task_id] = {
            "task_id": task_id,
            "status": "queued",
            "source_node_id": source_node_id,
            "target_node_ids": [str(node.get("id") or "") for node in target_nodes],
            "media": media,
            "message": "共享任务已创建",
            "done_bytes": 0,
            "total_bytes": 0,
            "current_bps": 0,
            "average_bps": 0,
            "results": [],
            "created_at": time.time(),
            "updated_at": time.time(),
        }
    worker = threading.Thread(
        target=run_share_task,
        args=(task_id, source_node, target_nodes, media, progress_url),
        daemon=True,
    )
    worker.start()
    return jsonify({"ok": True, **share_task_payload(share_task_snapshot(task_id) or {})})


@APP.get("/api/media/share/status/<task_id>")
def api_media_share_status(task_id: str):
    task = share_task_snapshot(task_id)
    if not task:
        return jsonify({"ok": False, "message": "share task not found"}), 404
    return jsonify(share_task_payload(task))


@APP.post("/api/media/share/progress/<task_id>")
def api_media_share_progress(task_id: str):
    payload = request.get_json(silent=True) or {}
    task = share_task_snapshot(task_id)
    if not task:
        return jsonify({"ok": False, "message": "share task not found"}), 404
    target_index = max(0, int(payload.get("target_index") or 0))
    target_count = max(1, int(payload.get("target_count") or 1))
    single_total = int(payload.get("total_bytes") or 0)
    single_done = int(payload.get("done_bytes") or 0)
    aggregate_total = single_total * target_count if single_total else int(task.get("total_bytes") or 0)
    aggregate_done = (single_total * target_index + single_done) if single_total else single_done
    update_share_task(
        task_id,
        status="running",
        message=str(payload.get("message") or "正在共享"),
        done_bytes=aggregate_done,
        total_bytes=aggregate_total,
        single_target_total_bytes=single_total or int(task.get("single_target_total_bytes") or 0),
        current_bps=int(payload.get("current_bps") or 0),
        average_bps=int(payload.get("average_bps") or 0),
    )
    return jsonify({"ok": True})


@APP.post("/api/nodes/media/rename")
def api_node_media_rename():
    payload = request.get_json(silent=True) or {}
    node_id = str(payload.get("node_id") or "").strip()
    media = str(payload.get("media") or payload.get("video_path") or "").strip()
    try:
        new_name = safe_media_filename(str(payload.get("new_name") or "").strip())
    except ValueError as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400
    node = node_by_id(node_id)
    if not node:
        return jsonify({"ok": False, "message": "node not found"}), 404
    if not media or not new_name:
        return jsonify({"ok": False, "message": "media and new_name are required"}), 400
    result = post_node_json(node, "/api/media/rename", {"media": media, "new_name": new_name}, timeout=30)
    return jsonify({"node_id": node_id, **result}), 200 if result.get("ok") else int(result.get("status_code") or 502)


@APP.post("/api/nodes/media/delete")
def api_node_media_delete():
    payload = request.get_json(silent=True) or {}
    node_id = str(payload.get("node_id") or "").strip()
    media = str(payload.get("media") or payload.get("video_path") or "").strip()
    node = node_by_id(node_id)
    if not node:
        return jsonify({"ok": False, "message": "node not found"}), 404
    if not media:
        return jsonify({"ok": False, "message": "media is required"}), 400
    result = post_node_json(node, "/api/media/delete", {"media": media}, timeout=30)
    return jsonify({"node_id": node_id, **result}), 200 if result.get("ok") else int(result.get("status_code") or 502)


def stream_payload_for_node(payload: dict[str, Any]) -> dict[str, Any]:
    stream_url = str(payload.get("stream_url") or "rtmp://a.rtmp.youtube.com/live2").strip().rstrip("/")
    stream_key = str(payload.get("stream_key") or "").strip()
    if stream_key.lower().startswith(("rtmp://", "rtmps://")):
        parsed_key = stream_key.rstrip("/")
        head, sep, tail = parsed_key.rpartition("/")
        if sep and head.lower().startswith(("rtmp://", "rtmps://")) and tail:
            stream_url = head.rstrip("/")
            stream_key = tail.strip()
    return {
        "stream_url": stream_url,
        "stream_key": stream_key,
        "youtube_stream_id": str(payload.get("youtube_stream_id") or "").strip(),
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
    if node_payload["stream_output_mode"] == "direct" and not node_payload["stream_key"]:
        return jsonify({"ok": False, "message": "missing stream key"}), 400
    if node_payload["stream_output_mode"] == "youtube_api" and not node_payload["youtube_stream_id"]:
        return jsonify({"ok": False, "message": "missing YouTube API stream ID"}), 400
    result = post_node_json(node, "/api/start-stream", node_payload, timeout=60)
    status_code = 200 if result.get("ok") else 502
    return jsonify({
        "node_id": node_id,
        **redacted_stream_result(result),
    }), status_code


@APP.post("/api/nodes/stop-stream")
def api_nodes_stop_stream():
    payload = request.get_json(silent=True) or {}
    node_id = str(payload.get("node_id") or "")
    node = node_by_id(node_id)
    if not node:
        return jsonify({"ok": False, "message": "node not found"}), 404
    if not node.get("enabled", True):
        return jsonify({"ok": False, "message": "node disabled"}), 409
    stop_api = str(node.get("stop_stream_api") or "/api/stop-stream").strip() or "/api/stop-stream"
    if not stop_api.startswith("/"):
        stop_api = f"/{stop_api}"
    result = post_node_json(node, stop_api, {}, timeout=30)
    status_code = 200 if result.get("ok") else int(result.get("status_code") or 502)
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
    if not stream_config.get("restart_ready"):
        return jsonify({
            "ok": False,
            "node_id": node_id,
            "message": "node has no active stream recovery configuration",
        }), 409

    restart_api = str(node.get("restart_stream_api") or "/api/restart-stream").strip() or "/api/restart-stream"
    if not restart_api.startswith("/"):
        restart_api = f"/{restart_api}"
    result = post_node_json(node, restart_api, {}, timeout=30)
    return jsonify({
        "ok": bool(result.get("ok")),
        "node_id": node_id,
        "message": result.get("message") or ("restart request accepted" if result.get("ok") else "restart request failed"),
        "result": result,
    }), 200 if result.get("ok") else int(result.get("status_code") or 502)


def youtube_node_from_payload(payload: dict[str, Any]) -> tuple[dict[str, Any] | None, tuple[Any, int] | None]:
    node_id = str(payload.get("node_id") or "").strip()
    node = node_by_id(node_id)
    if not node:
        return None, (jsonify({"ok": False, "message": "node not found"}), 404)
    if not node.get("enabled", True):
        return None, (jsonify({"ok": False, "message": "node disabled"}), 409)
    return node, None


@APP.post("/api/nodes/youtube/resources")
def api_nodes_youtube_resources():
    payload = request.get_json(silent=True) or {}
    node, error = youtube_node_from_payload(payload)
    if error:
        return error
    assert node is not None
    status = request_node_json(node, "/api/youtube/status?verify=1", timeout=30)
    if not status.get("ok") or not status.get("authorized"):
        return jsonify({"node_id": str(payload.get("node_id") or ""), **status}), int(status.get("status_code") or 200)
    streams = request_node_json(node, "/api/youtube/streams", timeout=30)
    broadcasts = request_node_json(node, "/api/youtube/broadcasts", timeout=30)
    ok = bool(streams.get("ok") and broadcasts.get("ok"))
    result = {
        "ok": ok,
        "node_id": str(payload.get("node_id") or ""),
        "configured": bool(status.get("configured")),
        "authorized": bool(status.get("authorized")),
        "channel": status.get("channel") or {},
        "streams": streams.get("streams") or [],
        "broadcasts": broadcasts.get("broadcasts") or [],
    }
    if not ok:
        result["message"] = streams.get("message") or broadcasts.get("message") or "YouTube resources unavailable"
    return jsonify(result), 200 if ok else 502


@APP.post("/api/nodes/youtube/oauth/start")
def api_nodes_youtube_oauth_start():
    payload = request.get_json(silent=True) or {}
    node, error = youtube_node_from_payload(payload)
    if error:
        return error
    assert node is not None
    result = post_node_json(node, "/api/youtube/oauth/start", {}, timeout=30)
    return jsonify({"node_id": str(payload.get("node_id") or ""), **result}), int(
        result.get("status_code") or (200 if result.get("ok") else 502)
    )


@APP.post("/api/nodes/youtube/config")
def api_nodes_youtube_config():
    payload = request.get_json(silent=True) or {}
    node, error = youtube_node_from_payload(payload)
    if error:
        return error
    assert node is not None
    client_id = str(payload.get("client_id") or "").strip()
    client_secret = str(payload.get("client_secret") or "").strip()
    if not client_id:
        return jsonify({"ok": False, "message": "YOUTUBE_CLIENT_ID is required"}), 400
    result = post_node_json(
        node,
        "/api/youtube/config",
        {"client_id": client_id, "client_secret": client_secret},
        timeout=30,
    )
    return jsonify({"node_id": str(payload.get("node_id") or ""), **result}), int(
        result.get("status_code") or (200 if result.get("ok") else 502)
    )


@APP.post("/api/nodes/youtube/oauth/poll")
def api_nodes_youtube_oauth_poll():
    payload = request.get_json(silent=True) or {}
    node, error = youtube_node_from_payload(payload)
    if error:
        return error
    assert node is not None
    result = post_node_json(
        node,
        "/api/youtube/oauth/poll",
        {"session_id": str(payload.get("session_id") or "")},
        timeout=30,
    )
    return jsonify({"node_id": str(payload.get("node_id") or ""), **result}), int(
        result.get("status_code") or (200 if result.get("ok") else 502)
    )


@APP.post("/api/nodes/youtube/prepare")
def api_nodes_youtube_prepare():
    payload = request.get_json(silent=True) or {}
    node, error = youtube_node_from_payload(payload)
    if error:
        return error
    assert node is not None
    allowed = {
        "title",
        "description",
        "privacy_status",
        "scheduled_start_time",
        "stream_id",
        "stream_title",
        "resolution",
        "frame_rate",
        "made_for_kids",
        "enable_auto_start",
        "enable_auto_stop",
        "enable_dvr",
    }
    node_payload = {key: payload.get(key) for key in allowed if key in payload}
    result = post_node_json(node, "/api/youtube/prepare", node_payload, timeout=60)
    return jsonify({"node_id": str(payload.get("node_id") or ""), **result}), int(
        result.get("status_code") or (200 if result.get("ok") else 502)
    )


@APP.post("/api/nodes/youtube/oauth/revoke")
def api_nodes_youtube_oauth_revoke():
    payload = request.get_json(silent=True) or {}
    node, error = youtube_node_from_payload(payload)
    if error:
        return error
    assert node is not None
    result = post_node_json(node, "/api/youtube/oauth/revoke", {}, timeout=30)
    return jsonify({"node_id": str(payload.get("node_id") or ""), **result}), int(
        result.get("status_code") or (200 if result.get("ok") else 502)
    )


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
    if not (ROOT / ".git").exists():
        return jsonify({
            "ok": False,
            "step": "checkout",
            "message": "Hub checkout has no git metadata; reinstall from the official repository before checking updates",
            "repo": SOURCE_REPO,
            "branch": SOURCE_BRANCH,
        }), 409

    fetch = run_git(["fetch", "--quiet", "--no-tags", SOURCE_REPO, SOURCE_BRANCH], cwd=ROOT, timeout=120)
    if not fetch["ok"]:
        safe_fetch = dict(fetch)
        safe_fetch["stderr"] = redact_secret(str(fetch.get("stderr") or ""), SOURCE_REPO)
        return jsonify({
            "ok": False,
            "step": "fetch",
            "repo": SOURCE_REPO,
            "branch": SOURCE_BRANCH,
            "fetch": safe_fetch,
        }), 502

    local = run_git(["rev-parse", "HEAD"], cwd=ROOT)
    remote = run_git(["rev-parse", "FETCH_HEAD"], cwd=ROOT)
    behind = run_git(["rev-list", "--count", "HEAD..FETCH_HEAD"], cwd=ROOT)
    ahead = run_git(["rev-list", "--count", "FETCH_HEAD..HEAD"], cwd=ROOT)
    diff = run_git(["diff", "--stat", "HEAD", "FETCH_HEAD"], cwd=ROOT)
    local_label = run_git(["log", "-1", "--format=%h %s", "HEAD"], cwd=ROOT)
    remote_label = run_git(["log", "-1", "--format=%h %s", "FETCH_HEAD"], cwd=ROOT)
    checks = (local, remote, behind, ahead, diff, local_label, remote_label)
    ok = all(item["ok"] for item in checks)
    behind_count = int(behind.get("stdout") or 0) if behind["ok"] else None
    ahead_count = int(ahead.get("stdout") or 0) if ahead["ok"] else None
    return jsonify({
        "ok": ok,
        "repo": SOURCE_REPO,
        "branch": SOURCE_BRANCH,
        "local": local.get("stdout"),
        "remote": remote.get("stdout"),
        "local_label": local_label.get("stdout"),
        "remote_label": remote_label.get("stdout"),
        "behind_count": behind_count,
        "ahead_count": ahead_count,
        "has_updates": bool(behind_count) if behind_count is not None else None,
        "diff_stat": diff.get("stdout"),
    }), 200 if ok else 500


@APP.post("/api/nodes/upgrade")
def api_nodes_upgrade():
    payload = request.get_json(silent=True) or {}
    node_id = str(payload.get("node_id") or "").strip()
    node = node_by_id(node_id)
    if not node:
        return jsonify({"ok": False, "node_id": node_id, "message": "node not found"}), 404
    if not node.get("enabled", True):
        return jsonify({"ok": False, "node_id": node_id, "message": "node disabled"}), 409
    upgrade_api = str(node.get("upgrade_api") or "/api/upgrade").strip() or "/api/upgrade"
    if not upgrade_api.startswith("/"):
        upgrade_api = f"/{upgrade_api}"
    result = post_node_json(node, upgrade_api, {}, timeout=30)
    status_code = 202 if result.get("ok") else int(result.get("status_code") or 502)
    return jsonify({"node_id": node_id, **result}), status_code


@APP.post("/api/nodes/roles/<role>/activate")
def api_activate_node_role(role: str):
    if role not in {"agent", "hub"}:
        return jsonify({"ok": False, "message": "unsupported role"}), 404
    payload = request.get_json(silent=True) or {}
    node_id = str(payload.get("node_id") or "").strip()
    node = node_by_id(node_id)
    if not node:
        return jsonify({"ok": False, "node_id": node_id, "message": "node not found"}), 404
    if role == "hub":
        result = post_node_json(node, "/api/roles/hub/activate", {}, timeout=30)
    else:
        hub_url = node_role_urls(node)["hub"]
        if not hub_url:
            return jsonify({"ok": False, "node_id": node_id, "message": "Hub role is unavailable; SSH bootstrap is required"}), 409
        result = post_url_json(
            f"{hub_url}/api/roles/agent/activate",
            {"control_hub_url": request.host_url.rstrip("/")},
            timeout=30,
        )
    status_code = 202 if result.get("ok") else int(result.get("status_code") or 502)
    return jsonify({"node_id": node_id, "role": role, **result}), status_code


@APP.post("/api/nodes/roles/<role>/upgrade")
def api_upgrade_node_role(role: str):
    payload = request.get_json(silent=True) or {}
    node_id = str(payload.get("node_id") or "").strip()
    node = node_by_id(node_id)
    if not node:
        return jsonify({"ok": False, "node_id": node_id, "message": "node not found"}), 404
    if role == "agent":
        result = post_node_json(node, "/api/upgrade", {}, timeout=30)
    elif role == "hub":
        hub_url = node_role_urls(node)["hub"]
        result = post_url_json(f"{hub_url}/api/upgrade", {}, timeout=30)
    else:
        return jsonify({"ok": False, "message": "unsupported role"}), 404
    status_code = 202 if result.get("ok") else int(result.get("status_code") or 502)
    return jsonify({"node_id": node_id, "role": role, **result}), status_code


def main() -> None:
    ensure_dirs()
    host = os.environ.get("STREAM_HUB_HOST", "127.0.0.1")
    try:
        from waitress import serve

        serve(APP, host=host, port=PORT)
    except ImportError:
        APP.run(host=host, port=PORT, threaded=True)
