"""Dashboard UI routes for the stream node agent.

Headless deployments still register these routes; the index route returns the
agent JSON contract when STREAM_NODE_AGENT_MODE=1.
"""

from .dashboard_templates import HTML, LOGIN_HTML
from .runtime import *  # noqa: F403 - route handlers intentionally reuse runtime globals.

@APP.route("/login", methods=["GET", "POST"])
def login():
    if not dashboard_auth_enabled():
        return redirect(url_for("index"))
    if request.method == "POST":
        password = (request.form.get("password") or "").strip()
        next_url = (request.form.get("next") or "").strip() or url_for("index")
        if password == dashboard_password():
            session["dashboard_authenticated"] = True
            return redirect(next_url if next_url.startswith("/") else url_for("index"))
        error_html = '<div class="error">密码不正确，请重新输入。</div>'
    else:
        if is_logged_in():
            return redirect(url_for("index"))
        error_html = ""
        next_url = (request.args.get("next") or "").strip() or url_for("index")
    html = LOGIN_HTML.replace("__ERROR_HTML__", error_html).replace("__NEXT_VALUE__", next_url)
    return render_template_string(html)


@APP.route("/logout")
def logout():
    session.pop("dashboard_authenticated", None)
    return redirect(url_for("login") if dashboard_auth_enabled() else url_for("index"))


@APP.route("/")
@protected
def index():
    if STREAM_NODE_AGENT_MODE:
        response = jsonify({
            "ok": True,
            "mode": "headless-agent",
            "name": STREAM_NODE_AGENT_NAME,
            "version": APP_VERSION,
            "message": "Stream node agent is running without the dashboard UI. Use the Control Hub for monitoring and operations.",
            "control_hub": CONTROL_HUB_URL,
            "api": {
                "status": "/api/status",
                "public_upload": "/api/public-upload",
                "upload_probe": "/api/upload-probe",
                "upload_chunk": "/api/upload-chunk",
            },
        })
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        return response

    response = APP.response_class(
        render_template_string(
            HTML
            .replace("__APP_VERSION__", APP_VERSION)
            .replace("__STREAM_FIFO_ENABLED__", "true" if STREAM_FIFO_ENABLED else "false")
            .replace("__STREAM_FIFO_QUEUE_SIZE__", str(STREAM_FIFO_QUEUE_SIZE))
            .replace("__STREAM_FIFO_RECOVERY_WAIT_SECONDS__", str(STREAM_FIFO_RECOVERY_WAIT_SECONDS))
            .replace("__STREAM_RELAY_LOCAL_URL__", STREAM_RELAY_LOCAL_URL.replace("\\", "\\\\").replace('"', '\\"')),
        ),
        mimetype="text/html",
    )
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return response


