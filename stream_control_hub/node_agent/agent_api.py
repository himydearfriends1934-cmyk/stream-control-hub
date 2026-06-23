"""HTTP API routes exposed by a VPS stream node agent."""

from .runtime import *  # noqa: F403 - extracted route handlers intentionally reuse runtime globals.

@APP.route("/api/status")
@protected
def api_status():
    cpu_percent = psutil.cpu_percent(interval=0.2)
    memory = psutil.virtual_memory()
    disk = psutil.disk_usage("/")
    net = psutil.net_io_counters()
    net_rates = get_current_net_rates(net)
    ffmpeg_processes = list_ffmpeg_processes()
    stream_bitrate_kbps = read_current_stream_bitrate_kbps()
    stream_config = load_stream_config() or {}
    public_stream_config = stream_config_public_view(stream_config)
    adaptive_state = adaptive_state_snapshot()
    stream_tuning = load_stream_tuning()
    runtime_state = load_stream_runtime_state()
    relay = stream_relay_status(stream_config)

    try:
        load_avg_raw = os.getloadavg()
        load_avg = " / ".join(f"{v:.2f}" for v in load_avg_raw)
    except (AttributeError, OSError):
        load_avg = "N/A"

    return jsonify({
        "ok": True,
        "agent": {
            "name": STREAM_NODE_AGENT_NAME,
            "version": APP_VERSION,
            "mode": "headless-agent" if STREAM_NODE_AGENT_MODE else "dashboard-compatible",
            "headless": STREAM_NODE_AGENT_MODE,
            "control_hub": CONTROL_HUB_URL,
            "started_at": START_TIME,
            "started_at_label": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(START_TIME)),
        },
        "hostname": platform.node(),
        "platform": f"{platform.system()} {platform.release()} ({platform.machine()})",
        "cpu_percent": cpu_percent,
        "cpu_count": psutil.cpu_count(),
        "memory": {
            "total": memory.total,
            "used": memory.used,
            "percent": memory.percent,
        },
        "disk": {
            "total": disk.total,
            "used": disk.used,
            "percent": disk.percent,
        },
        "net": {
            "bytes_sent": net.bytes_sent,
            "bytes_recv": net.bytes_recv,
            "current_upload_bps": net_rates["upload_bps"],
            "current_download_bps": net_rates["download_bps"],
            "rate_label": (
                f"↑ {net_rates['upload_bps'] / 1024 / 1024:.2f} MB/s ↓ {net_rates['download_bps'] / 1024 / 1024:.2f} MB/s"
                if max(net_rates["upload_bps"], net_rates["download_bps"]) >= 1024 * 1024
                else f"↑ {net_rates['upload_bps'] / 1024:.0f} KB/s ↓ {net_rates['download_bps'] / 1024:.0f} KB/s"
            ),
            "rate_percent": bytes_to_rate_percent(net_rates["combined_bps"]),
        },
        "quota": {
            "limit": TRAFFIC_QUOTA_BYTES,
            "total_used": net.bytes_sent + net.bytes_recv,
            "remaining": max(0, TRAFFIC_QUOTA_BYTES - net.bytes_sent - net.bytes_recv),
            "sent_percent": net.bytes_sent / TRAFFIC_QUOTA_BYTES * 100,
            "recv_percent": net.bytes_recv / TRAFFIC_QUOTA_BYTES * 100,
            "total_percent": (net.bytes_sent + net.bytes_recv) / TRAFFIC_QUOTA_BYTES * 100,
        },
        "uptime": format_seconds(time.time() - psutil.boot_time()),
        "app_uptime": format_seconds(time.time() - START_TIME),
        "load_avg": load_avg,
        "stream_config": public_stream_config,
        "public_upload": public_upload_status(),
        "transfer": transfer_runtime_snapshot(),
        "stream": {
            "running": bool(ffmpeg_processes),
            "processes": ffmpeg_processes,
            "current_bitrate_kbps": stream_bitrate_kbps,
            "current_bitrate_label": (
                f"{stream_bitrate_kbps / 1000:.2f} Mbps" if stream_bitrate_kbps and stream_bitrate_kbps >= 1000
                else (f"{stream_bitrate_kbps:.0f} kbps" if stream_bitrate_kbps else "未知")
            ),
            "auto_restart": {
                **STREAM_RESTART_STATE,
                "runtime_state": runtime_state,
                "stall_timeout_seconds": STREAM_STALL_TIMEOUT_SECONDS,
                "startup_stall_grace_seconds": STREAM_STARTUP_STALL_GRACE_SECONDS,
                "last_restart_at_label": (
                    time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(STREAM_RESTART_STATE["last_restart_at"]))
                    if STREAM_RESTART_STATE["last_restart_at"] else ""
                ),
                "last_exit_at_label": (
                    time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(STREAM_RESTART_STATE["last_exit_at"]))
                    if STREAM_RESTART_STATE["last_exit_at"] else ""
                ),
                "next_restart_at_label": (
                    time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(STREAM_RESTART_STATE["next_restart_at"]))
                    if STREAM_RESTART_STATE["next_restart_at"] else ""
                ),
            },
            "adaptive": {
                **adaptive_state,
                "last_evaluated_at_label": (
                    time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(adaptive_state["last_evaluated_at"]))
                    if adaptive_state.get("last_evaluated_at") else ""
                ),
                "last_applied_at_label": (
                    time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(adaptive_state["last_applied_at"]))
                    if adaptive_state.get("last_applied_at") else ""
                ),
                "cooldown_until_label": (
                    time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(adaptive_state["cooldown_until"]))
                    if adaptive_state.get("cooldown_until") else ""
                ),
            },
            "tuning": stream_tuning,
            "relay": relay,
        },
        "videos": list_uploaded_videos(),
        "chat_plan": load_chat_plan(),
        "chat_runtime": chat_runtime_snapshot(),
        "youtube_auth": youtube_auth_status(request.host_url.rstrip("/")),
    })
