from __future__ import annotations

import argparse
import sys
import traceback
from pathlib import Path

from .document import list_projects
from .server import find_running_editor, serve

# Project root = folder containing this package. Everything stays relative so
# the whole folder can be moved and still works.
ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PROJECTS_DIR = ROOT / "projects"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="circuitstudio")
    parser.add_argument("project", nargs="?", default=None,
                        help="project name (without .circuit.json)")
    parser.add_argument("--projects-dir", default=str(DEFAULT_PROJECTS_DIR))
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args(argv)

    projects_dir = Path(args.projects_dir).resolve()
    projects_dir.mkdir(parents=True, exist_ok=True)

    name = args.project
    if not name:
        existing = list_projects(projects_dir)
        name = existing[0] if existing else "demo"

    running = find_running_editor(projects_dir, name)
    if running:
        print(f"'{name}' is already open at {running} — showing that editor.")
        if not args.no_browser:
            import webbrowser
            webbrowser.open(running)
        return 0

    serve(projects_dir, name,
          open_browser=not args.no_browser,
          port=args.port)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # keep the console window useful when double-clicked
        traceback.print_exc()
        sys.exit(1)
