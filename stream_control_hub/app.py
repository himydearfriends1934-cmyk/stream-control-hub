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
    .grid { display: grid; grid-template-columns: 1.15fr 0.85fr; gap: 14px; margin-top: 14px; }
    .card {
      border: 1px solid var(--line);
      border-radius: 16px;
      padding: 14px;
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
    button:disabled { opacity: 0.55; cursor: not-allowed; }
    input[type=file] { width: 100%; }
    .node-list, .media-list, .log { display: grid; gap: 8px; }
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
    .node strong, .media strong { display: block; }
    .node small, .media small { color: var(--muted); }
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
    @media (max-width: 980px) {
      .grid, .split, .hero { grid-template-columns: 1fr; }
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
        <button id="checkUpdatesBtn">检查 GitHub 更新</button>
      </div>
    </section>

    <section class="grid">
      <div class="card">
        <h2>VPS 节点</h2>
        <div class="node-list" id="nodeList">加载中...</div>
      </div>

      <div class="card">
        <h2>GitHub 更新</h2>
        <div class="actions">
          <button class="primary" id="upgradeSelectedBtn">更新选中节点</button>
        </div>
        <pre id="updateBox">等待检查...</pre>
      </div>

      <div class="card">
        <h2>本地资源库</h2>
        <div class="split">
          <div>
            <input id="mediaInput" type="file" accept=".mp4,.mov,.mkv,.m4v,.webm">
            <div class="actions" style="margin-top: 8px;">
              <button class="primary" id="uploadBtn">上传到本地总控台</button>
              <button id="pushSelectedBtn">推送选中资源到节点</button>
            </div>
          </div>
          <pre id="uploadBox">先上传到本地，再推送给 VPS。</pre>
        </div>
        <div class="media-list" id="mediaList" style="margin-top: 12px;">加载中...</div>
      </div>

      <div class="card">
        <h2>操作日志</h2>
        <pre id="logBox">就绪。</pre>
      </div>
    </section>
  </div>

  <script>
    const refs = {
      nodeList: document.getElementById("nodeList"),
      mediaList: document.getElementById("mediaList"),
      refreshBtn: document.getElementById("refreshBtn"),
      checkUpdatesBtn: document.getElementById("checkUpdatesBtn"),
      upgradeSelectedBtn: document.getElementById("upgradeSelectedBtn"),
      mediaInput: document.getElementById("mediaInput"),
      uploadBtn: document.getElementById("uploadBtn"),
      pushSelectedBtn: document.getElementById("pushSelectedBtn"),
      updateBox: document.getElementById("updateBox"),
      uploadBox: document.getElementById("uploadBox"),
      logBox: document.getElementById("logBox"),
    };
    let nodes = [];
    let media = [];

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
      if (!node.enabled) return `<span class="pill warn">disabled</span>`;
      if (!node.health?.ok) return `<span class="pill bad">offline</span>`;
      return `<span class="pill">online</span>`;
    }

    function renderNodes() {
      refs.nodeList.innerHTML = nodes.length ? nodes.map((node) => `
        <label class="node">
          <input data-node-check type="checkbox" value="${node.id}" ${node.enabled ? "" : "disabled"}>
          <span>
            <strong>${node.name || node.id}</strong>
            <small>${node.base_url || ""}</small><br>
            <small>推流：${node.health?.stream?.current_bitrate_label || "未知"} | FFmpeg：${node.health?.stream?.running ? "运行中" : "未运行"}</small>
          </span>
          ${nodeStatusPill(node)}
        </label>
      `).join("") : "还没有配置节点。";
    }

    function renderMedia() {
      refs.mediaList.innerHTML = media.length ? media.map((item) => `
        <label class="media">
          <input data-media-check type="radio" name="media" value="${item.name}">
          <span>
            <strong>${item.name}</strong>
            <small>${item.size_label} | ${item.modified_label}</small>
          </span>
          <span class="pill">local</span>
        </label>
      `).join("") : "本地资源库还没有视频。";
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

    refs.refreshBtn.addEventListener("click", refreshAll);
    refs.uploadBtn.addEventListener("click", uploadMedia);
    refs.checkUpdatesBtn.addEventListener("click", checkUpdates);
    refs.pushSelectedBtn.addEventListener("click", pushSelectedMedia);
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
        data = resp.json()
        data.setdefault("ok", resp.ok)
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
        data.setdefault("ok", resp.ok)
        data.setdefault("status_code", resp.status_code)
        return data
    except Exception as exc:
        return {"ok": False, "message": str(exc)}


def public_upload_summary(data: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in data.items() if key != "token"}


def select_node_upload_route(node: dict[str, Any]) -> dict[str, Any]:
    base_url = node_base_url(node)
    if not base_url:
        raise ValueError("missing node base_url")

    status = request_node_json(node, "/api/public-upload", timeout=10)
    route = {
        "base_url": base_url,
        "upload_base_url": base_url,
        "route": "internal",
        "route_label": "internal",
        "token": "",
        "headers": {},
        "opened_public_window": False,
        "chunk_bytes": NODE_UPLOAD_CHUNK_BYTES,
        "public_status": public_upload_summary(status),
        "warnings": [],
        "last_heartbeat_at": 0.0,
    }
    if not status.get("ok"):
        route["warnings"].append(status.get("message") or "public upload status unavailable")
        return route

    public_origin = str(status.get("public_origin") or "").rstrip("/")
    restrict_public = bool(status.get("restrict_public_to_upload"))
    supports_window = bool(status.get("supported"))

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
                return route
        route["warnings"].append(opened.get("message") or "failed to open public upload window")

    if public_origin and not restrict_public:
        route.update({
            "upload_base_url": public_origin,
            "route": "public-direct",
            "route_label": "public direct",
            "chunk_bytes": NODE_PUBLIC_UPLOAD_CHUNK_BYTES,
        })
    return route


def route_summary(route: dict[str, Any]) -> dict[str, Any]:
    return {
        "route": route.get("route"),
        "route_label": route.get("route_label"),
        "upload_base_url": route.get("upload_base_url"),
        "opened_public_window": bool(route.get("opened_public_window")),
        "chunk_bytes": route.get("chunk_bytes"),
        "warnings": route.get("warnings") or [],
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
            last_payload = payload or {}
            if not last_payload.get("ok"):
                raise RuntimeError(last_payload.get("message") or f"chunk {chunk_index + 1} upload failed")
            received_size = max(received_size, int(last_payload.get("received_size") or 0))

        if not last_payload.get("complete"):
            raise RuntimeError("node did not report upload completion")

        close_result = close_node_public_upload(node, route, reason="stream-control-hub-media-push-complete")
        elapsed = max(0.001, time.time() - started_at)
        return {
            "node_id": node_id,
            "ok": True,
            "message": "media pushed to node",
            "media": media_path.name,
            "size": total_size,
            "size_label": file_size_label(total_size),
            "received_size": received_size,
            "elapsed_seconds": round(elapsed, 2),
            "average_rate_label": f"{file_size_label(int(total_size / elapsed))}/s",
            "video_path": last_payload.get("video_path"),
            "route": route_summary(route),
            "close_public_window": close_result,
        }
    except Exception as exc:
        cleanup = cancel_node_upload(node, upload_id)
        close_result: dict[str, Any] = {"ok": True, "skipped": True}
        if route:
            with suppress(Exception):
                close_result = close_node_public_upload(node, route, reason="stream-control-hub-media-push-failed")
        return {
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


@APP.post("/api/media/upload")
def api_media_upload():
    ensure_dirs()
    upload = request.files.get("file")
    if not upload or not upload.filename:
        return jsonify({"ok": False, "message": "missing file"}), 400
    if not media_allowed(upload.filename):
        return jsonify({"ok": False, "message": "unsupported media extension"}), 400
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
