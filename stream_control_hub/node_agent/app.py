"""Application entrypoint for the VPS stream node agent."""

from __future__ import annotations

import threading

from . import agent_api as _agent_api  # noqa: F401 - imports register API routes.
from . import chat_api as _chat_api  # noqa: F401 - imports register chat routes.
from . import dashboard_ui as _dashboard_ui  # noqa: F401 - imports register UI routes.
from . import stream_api as _stream_api  # noqa: F401 - imports register streaming routes.
from . import upload_api as _upload_api  # noqa: F401 - imports register upload routes.
from . import youtube_api as _youtube_api  # noqa: F401 - imports register YouTube routes.
from .chat import chat_scheduler_loop
from . import runtime


APP = runtime.APP
PORT = runtime.PORT
_BACKGROUND_STARTED = False


def start_background_services() -> None:
    """Start long-running node services once per process."""
    global _BACKGROUND_STARTED
    if _BACKGROUND_STARTED:
        return
    runtime.reset_public_upload_window_on_startup()
    threading.Thread(target=chat_scheduler_loop, daemon=True).start()
    threading.Thread(target=runtime.stream_watchdog_loop, daemon=True).start()
    _BACKGROUND_STARTED = True


def main() -> None:
    start_background_services()
    APP.run(host="0.0.0.0", port=PORT, debug=False)
