from __future__ import annotations

import os
import sys


def main() -> None:
    role = (sys.argv[1] if len(sys.argv) > 1 else os.environ.get("STREAM_CONTROL_ROLE", "hub")).lower()
    if role in {"agent", "node", "node-agent", "stream-node"}:
        from .node_agent.app import main as agent_main

        agent_main()
        return

    from .app import main as hub_main

    hub_main()


if __name__ == "__main__":
    main()
