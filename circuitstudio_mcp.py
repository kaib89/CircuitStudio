"""Entry point for MCP clients (Claude Desktop, VS Code, ...).

Standalone launcher so the client only needs an absolute path to THIS file —
no working directory or PYTHONPATH setup required.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from circuitstudio.mcp_server import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
