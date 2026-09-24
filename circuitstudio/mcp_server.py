"""MCP server (stdio) — lets an LLM own the netlist.

Deliberately talks to the JSON files directly instead of to the running editor:
no port coordination, and it works whether or not the GUI is open. The editor
polls circuit.json, so writes show up live in the browser.

Pure stdlib JSON-RPC 2.0 over stdin/stdout — no SDK dependency.
Anything printed to stdout that is not a protocol message breaks the transport,
so all diagnostics go to stderr.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from .document import Project, is_safe_name, list_projects
from .registry import CONFIGURABLE_TYPES, KNOWN_TYPES, build_component
from .scene import Scene

ROOT = Path(__file__).resolve().parent.parent
PROJECTS_DIR = ROOT / "projects"

SERVER_NAME = "circuitstudio"
SERVER_VERSION = "0.1.0"
KNOWN_PROTOCOLS = {"2024-11-05", "2025-03-26", "2025-06-18"}
FALLBACK_PROTOCOL = "2024-11-05"

CIRCUIT_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "components": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "z.B. R1, U1, GND1"},
                    "type": {"type": "string", "enum": KNOWN_TYPES},
                    "value": {"type": "string", "description": "z.B. 10kΩ, 100nF"},
                    "pins": {
                        "type": "object",
                        "description": "Only for 'ic' and board types: "
                                       "{left:[],right:[],top:[],bottom:[]}",
                    },
                    "n": {"type": "integer", "description": "Only for 'connector'"},
                    "side": {"type": "string", "description": "Only for 'connector'"},
                },
                "required": ["id", "type"],
            },
        },
        "nets": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "pins": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Pin references, written as 'PartID.PinName'",
                    },
                },
                "required": ["name", "pins"],
            },
        },
        "notes": {
            "type": "array",
            "description": (
                "Annotations printed on the schematic itself — explanations, "
                "warnings, sourcing hints. Better here than in chat, because "
                "they survive in the exported SVG."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "z.B. N1"},
                    "text": {"type": "string"},
                    "anchor": {
                        "type": "string",
                        "description": (
                            "Optional: 'R1' or 'U1.GPIO25'. The note is then pinned "
                            "to that part or pin with a dashed leader line."
                        ),
                    },
                },
                "required": ["id", "text"],
            },
        },
    },
    "required": ["components", "nets"],
}

TOOLS: list[dict[str, Any]] = [
    {
        "name": "list_projects",
        "description": "List all existing CircuitStudio projects.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_component_types",
        "description": (
            "List every available part type with its pin names. ALWAYS call "
            "this before writing a circuit — pin names must match exactly."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_circuit",
        "description": "Read a project's netlist (circuit.json).",
        "inputSchema": {
            "type": "object",
            "properties": {"project": {"type": "string"}},
            "required": ["project"],
        },
    },
    {
        "name": "write_circuit",
        "description": (
            "Write a project's complete netlist. Replaces circuit.json entirely "
            "but leaves the human-made layout untouched: known part IDs keep "
            "their position, new ones are roughly auto-placed. Validated BEFORE "
            "writing — on unknown types or wrong pin names nothing is written "
            "and the errors are returned instead."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {
                    "type": "string",
                    "description": "Project name without extension; A-Z a-z 0-9 _ - only",
                },
                "circuit": CIRCUIT_SCHEMA,
            },
            "required": ["project", "circuit"],
        },
    },
    {
        "name": "open_editor",
        "description": (
            "Start the CircuitStudio editor and open it in the browser so the "
            "human can arrange the parts."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"project": {"type": "string"}},
            "required": ["project"],
        },
    },
]


# ── tool implementations ────────────────────────────────────────────────────

def _require_name(args: dict[str, Any]) -> str:
    name = args.get("project")
    if not isinstance(name, str) or not is_safe_name(name):
        raise ValueError(
            "Invalid project name. Only A-Z, a-z, 0-9, _ and - are allowed.")
    return name


def _validate(circuit: dict[str, Any]) -> list[str]:
    """Check types and pin references without needing any layout."""
    errors: list[str] = []
    comps: dict[str, Any] = {}
    seen: set[str] = set()

    for spec in circuit.get("components", []):
        if not isinstance(spec, dict):
            errors.append(f"Part is not an object: {spec!r}")
            continue
        cid = str(spec.get("id", ""))
        if cid in seen:
            errors.append(f"Duplicate part ID: {cid}")
            continue
        seen.add(cid)
        if "." in cid:
            # Pin references are split at the first dot, so 'U.1.GND' would
            # silently point at a part called 'U'.
            errors.append(f"Part ID '{cid}' must not contain a '.'")
            continue
        try:
            comp = build_component(spec)
        except (ValueError, TypeError, AttributeError) as exc:
            errors.append(f"Part '{cid}': {exc}")
            continue
        comps[cid] = comp
        names = [p.name for p in comp.pins]
        dupes = sorted({n for n in names if names.count(n) > 1})
        if dupes:
            # Only the first pin of that name would ever be connected.
            errors.append(
                f"Part '{cid}': pin name(s) {', '.join(dupes)} used more than "
                f"once — give each pin a unique name (e.g. GND_1, GND_2)")

    owner: dict[str, str] = {}      # pin ref -> first net that uses it
    net_names: set[str] = set()
    for net in circuit.get("nets", []):
        if not isinstance(net, dict):
            errors.append(f"Net is not an object: {net!r}")
            continue
        nname = str(net.get("name", "?"))
        if nname in net_names:
            errors.append(
                f"Duplicate net name '{nname}' — nets with the same name are "
                f"one node, so merge their pins into a single net")
        net_names.add(nname)
        pins = net.get("pins", [])
        if not isinstance(pins, list):
            errors.append(f"Net '{nname}': 'pins' must be a list")
            continue
        for ref in pins:
            ref = str(ref)
            if "." not in ref:
                errors.append(f"Net '{nname}': '{ref}' is not an 'ID.Pin' reference")
                continue
            cid, pin = ref.split(".", 1)
            comp = comps.get(cid)
            if comp is None:
                if cid not in seen:
                    errors.append(f"Net '{nname}': part '{cid}' does not exist")
                continue
            try:
                comp.pin(pin)
            except KeyError:
                available = ", ".join(p.name for p in comp.pins)
                errors.append(
                    f"Net '{nname}': '{cid}' has no pin '{pin}'. "
                    f"Available: {available}")
                continue
            first = owner.setdefault(ref, nname)
            if first != nname:
                errors.append(
                    f"Pin '{ref}' is in net '{first}' and in net '{nname}' — "
                    f"that shorts both nets; merge them or fix the pin")

    seen_notes: set[str] = set()
    for note in circuit.get("notes", []) or []:
        if not isinstance(note, dict):
            errors.append(f"Note is not an object: {note!r}")
            continue
        nid = str(note.get("id", ""))
        if not nid:
            errors.append("Note without an 'id'")
            continue
        if nid in seen_notes:
            errors.append(f"Duplicate note ID: {nid}")
        seen_notes.add(nid)
        if not str(note.get("text", "")).strip():
            errors.append(f"Note '{nid}' has no text")
        anchor = str(note.get("anchor", "") or "")
        if not anchor:
            continue
        cid, _, pin = anchor.partition(".")
        comp = comps.get(cid)
        if comp is None:
            errors.append(f"Note '{nid}': anchor part '{cid}' does not exist")
        elif pin:
            try:
                comp.pin(pin)
            except KeyError:
                available = ", ".join(p.name for p in comp.pins)
                errors.append(
                    f"Note '{nid}': '{cid}' has no pin '{pin}'. "
                    f"Available: {available}")
    return errors


def tool_list_projects(_args: dict[str, Any]) -> str:
    names = list_projects(PROJECTS_DIR)
    if not names:
        return f"No projects yet in {PROJECTS_DIR}."
    return "Projects:\n" + "\n".join(f"- {n}" for n in names)


def tool_list_component_types(_args: dict[str, Any]) -> str:
    lines = ["Available part types (type -> pins):"]
    for t in KNOWN_TYPES:
        if t in CONFIGURABLE_TYPES:
            lines.append(f"- {t}: pins are user-defined")
            continue
        try:
            comp = build_component({"id": "X1", "type": t})
            lines.append(f"- {t}: " + ", ".join(p.name for p in comp.pins))
        except ValueError as exc:
            lines.append(f"- {t}: (error: {exc})")
    lines.append("")
    lines.append("'ic' and the board types (esp32, rpi, pico, arduino_*) take "
                 "their pins via pins={\"left\":[...],\"right\":[...],"
                 "\"top\":[...],\"bottom\":[...]}. Those names are then the "
                 "valid pin names.")
    lines.append("'connector' takes n=<count>; its pins are named \"1\"..\"n\".")
    lines.append("Nets named VCC/VDD/3V3/5V... are drawn in red, "
                 "GND/VSS in black and thicker.")
    return "\n".join(lines)


def tool_get_circuit(args: dict[str, Any]) -> str:
    name = _require_name(args)
    path = PROJECTS_DIR / f"{name}.circuit.json"
    if not path.exists():
        return f"Project '{name}' does not exist yet."
    return path.read_text(encoding="utf-8")


def tool_write_circuit(args: dict[str, Any]) -> str:
    name = _require_name(args)
    circuit = args.get("circuit")
    if not isinstance(circuit, dict):
        raise ValueError("'circuit' must be an object.")
    if not isinstance(circuit.get("components"), list) or \
       not isinstance(circuit.get("nets"), list):
        raise ValueError("'circuit' needs the lists 'components' and 'nets'.")

    errors = _validate(circuit)
    if errors:
        return ("NOT written — please fix:\n"
                + "\n".join(f"- {e}" for e in errors))

    PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    project = Project(PROJECTS_DIR, name)
    if project.circuit_path.exists() or project.layout_path.exists():
        # Only the layout matters here — the circuit is about to be replaced,
        # and a broken circuit.json must not block the write that fixes it.
        project.load_layout()
    circuit.setdefault("title", name)
    project.circuit = circuit
    project.save_circuit()
    project.autoplace()
    project.autoplace_notes()
    project.save_layout()

    scene = Scene(project)
    project.save_routes()       # the editor then opens without routing again
    new_ids = project.auto_placed_ids()
    msg = [
        f"Written: {project.circuit_path}",
        f"{len(scene.components)} parts, {len(scene.nets)} nets"
        + (f", {len(scene.notes)} notes." if scene.notes else "."),
    ]
    if new_ids:
        msg.append(f"Not arranged yet (marked orange in the editor): "
                   f"{', '.join(sorted(new_ids))}")
    else:
        msg.append("All parts already have a position from the layout.")
    msg.append("The human now arranges the parts in the editor "
               "(open_editor) and exports the SVG.")
    return "\n".join(msg)


def tool_open_editor(args: dict[str, Any]) -> str:
    name = _require_name(args)
    creationflags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
    subprocess.Popen(
        [sys.executable, "-m", "circuitstudio", name],
        cwd=str(ROOT),
        creationflags=creationflags,
        # Our stdout is the JSON-RPC channel. Without a new console (anything
        # but Windows) the editor would inherit it and its startup banner
        # would corrupt the protocol stream.
        stdin=subprocess.DEVNULL,
        stdout=None if creationflags else subprocess.DEVNULL,
        start_new_session=not creationflags,
    )
    return (f"Editor for '{name}' started — the browser opens automatically. "
            f"The console window stays open; closing it stops the editor.")


HANDLERS = {
    "list_projects": tool_list_projects,
    "list_component_types": tool_list_component_types,
    "get_circuit": tool_get_circuit,
    "write_circuit": tool_write_circuit,
    "open_editor": tool_open_editor,
}


# ── JSON-RPC plumbing ───────────────────────────────────────────────────────

def _result(msg_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _error(msg_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def handle(msg: dict[str, Any]) -> dict[str, Any] | None:
    method = msg.get("method")
    msg_id = msg.get("id")
    params = msg.get("params") or {}

    if method == "initialize":
        requested = params.get("protocolVersion")
        version = requested if requested in KNOWN_PROTOCOLS else FALLBACK_PROTOCOL
        return _result(msg_id, {
            "protocolVersion": version,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        })

    if method in ("notifications/initialized", "notifications/cancelled"):
        return None  # notifications get no response

    if method == "ping":
        return _result(msg_id, {})

    if method == "tools/list":
        return _result(msg_id, {"tools": TOOLS})

    if method in ("resources/list", "prompts/list"):
        key = method.split("/")[0]
        return _result(msg_id, {key: []})

    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        handler = HANDLERS.get(name)
        if handler is None:
            return _result(msg_id, {
                "content": [{"type": "text", "text": f"Unknown tool: {name}"}],
                "isError": True,
            })
        try:
            text = handler(args)
            return _result(msg_id, {"content": [{"type": "text", "text": text}]})
        except Exception as exc:  # report back instead of killing the transport
            return _result(msg_id, {
                "content": [{"type": "text", "text": f"Error: {exc}"}],
                "isError": True,
            })

    if msg_id is None:
        return None
    return _error(msg_id, -32601, f"Method not found: {method}")


def main() -> int:
    stdout = sys.stdout.buffer
    for raw in sys.stdin.buffer:
        try:
            line = raw.decode("utf-8").strip()
        except UnicodeDecodeError:
            continue
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(msg, dict):
            stdout.write((json.dumps(_error(None, -32600, "Invalid Request"))
                          + "\n").encode("utf-8"))
            stdout.flush()
            continue
        try:
            response = handle(msg)
        except Exception as exc:
            print(f"[circuitstudio-mcp] {exc}", file=sys.stderr, flush=True)
            response = _error(msg.get("id"), -32603, str(exc))
        if response is not None:
            stdout.write((json.dumps(response, ensure_ascii=False) + "\n").encode("utf-8"))
            stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
