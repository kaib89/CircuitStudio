"""MCP server (stdio) — lets an LLM own the netlist.

Deliberately talks to the JSON files directly instead of to the running editor:
no port coordination, and it works whether or not the GUI is open. The editor
polls circuit.json, so writes show up live in the browser.

Pure stdlib JSON-RPC 2.0 over stdin/stdout — no SDK dependency.
Anything printed to stdout that is not a protocol message breaks the transport,
so all diagnostics go to stderr.
"""
from __future__ import annotations

import base64
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from . import footprints, library
from .bb_place import autoplace
from .breadboard import HUMAN_EDIT, Breadboard, parse_hole
from .document import Project, is_safe_name, list_projects
from .erc import check as erc_check, format_report
from .registry import CONFIGURABLE_TYPES, KNOWN_TYPES, build_component
from .scene import Scene
from .server import find_running_editor

ROOT = Path(__file__).resolve().parent.parent
PROJECTS_DIR = ROOT / "projects"

SERVER_NAME = "circuitstudio"
SERVER_VERSION = "0.1.0"
KNOWN_PROTOCOLS = {"2024-11-05", "2025-03-26", "2025-06-18"}
FALLBACK_PROTOCOL = "2024-11-05"

_TIE_DOC = (
    "ground, vcc, vdd and label are net ties, not parts: several of them in "
    "one net are connected by definition and never wired to each other. Each "
    "ordinary pin of the net is wired only to the nearest of these symbols, "
    "so use them generously — one GND symbol next to every part that needs "
    "ground, and a label pair instead of a long signal wire across the sheet "
    "(a 'label' needs value = net name). With only one such symbol the whole "
    "net is wired to it as usual."
)

CIRCUIT_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "components": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "e.g. R1, U1, GND1"},
                    "type": {"type": "string", "enum": KNOWN_TYPES,
                             "description": _TIE_DOC},
                    "part": {
                        "type": "string",
                        "description": (
                            "A real part from the library (list_library), e.g. "
                            "\"74HCU04\", \"LM358\", \"BC547\". Supplies pins, "
                            "package and pinout — prefer it over typing them. "
                            "'type' can then be left off."
                        ),
                    },
                    "value": {"type": "string", "description": "e.g. 10kΩ, 100nF"},
                    "pins": {
                        "type": "object",
                        "description": "Only for 'ic' and board types: "
                                       "{left:[],right:[],top:[],bottom:[]}",
                    },
                    "package": {
                        "type": "string",
                        "description": (
                            "Only needed for the breadboard: physical package of "
                            "an 'ic' (\"DIP-14\", \"SIP-4\") — see "
                            "list_component_types for the built-in defaults."
                        ),
                    },
                    "pinout": {
                        "type": "object",
                        "description": (
                            "Only needed for the breadboard: {pin name: physical "
                            "pin number} from the datasheet, covering every pin. "
                            "Required for 'ic' (with 'package') and transistors."
                        ),
                    },
                    "n": {"type": "integer", "description": "Only for 'connector'"},
                    "side": {"type": "string", "description": "Only for 'connector'"},
                    "group": {
                        "type": "string",
                        "description": (
                            "Optional functional block, e.g. 'Pitch oscillator', "
                            "'Power supply'. Parts of one group are auto-arranged "
                            "as one compact block, and the blocks are then placed "
                            "side by side. Worth it from ~15 parts on. Leave it "
                            "off ground/supply/label symbols — those are hung "
                            "onto their pins anyway."
                        ),
                    },
                },
                "required": ["id"],
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
                    "id": {"type": "string", "description": "e.g. N1"},
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
        "nc": {
            "type": "array",
            "description": (
                "Pins that are meant to stay unconnected, e.g. [\"U1.EN\", "
                "\"J1.2\"]. They get the standard no-connect cross in the "
                "drawing and stop being reported as a warning — use this to say "
                "'yes, I left this open on purpose'. For the reason behind it, "
                "add a note anchored to the same pin."
            ),
            "items": {"type": "string"},
        },
    },
    "required": ["components", "nets"],
}

_COMPONENT_SCHEMA = CIRCUIT_SCHEMA["properties"]["components"]["items"]
_NET_SCHEMA = CIRCUIT_SCHEMA["properties"]["nets"]["items"]
_NOTE_SCHEMA = CIRCUIT_SCHEMA["properties"]["notes"]["items"]
_REFS = {"type": "array", "items": {"type": "string"}}

CHANGES_SCHEMA = {
    "type": "object",
    "description": (
        "Applied in this order: removals and disconnect, then upserts, then "
        "connect. Everything is validated as a whole before anything is written."
    ),
    "properties": {
        "title": {"type": "string"},
        "remove_components": {**_REFS, "description":
                              "Part IDs. Their pins are also dropped from every "
                              "net and from 'nc'."},
        "remove_nets": {**_REFS, "description": "Net names."},
        "remove_notes": {**_REFS, "description": "Note IDs."},
        "disconnect": {**_REFS, "description":
                       "Pin references ('R1.2') to take out of whatever net holds them."},
        "upsert_components": {"type": "array", "items": _COMPONENT_SCHEMA,
                              "description": "Added, or replacing the part with the same ID."},
        "upsert_nets": {"type": "array", "items": _NET_SCHEMA,
                        "description": "Added, or replacing the net with the same name."},
        "upsert_notes": {"type": "array", "items": _NOTE_SCHEMA,
                         "description": "Added, or replacing the note with the same ID."},
        "add_nc": {**_REFS, "description":
                   "Pins to mark as deliberately unconnected (see 'nc')."},
        "remove_nc": {**_REFS, "description": "Pins to take off the 'nc' list."},
        "connect": {
            "type": "array",
            "description": (
                "Add pins to a net (created if missing). A pin already in "
                "another net is moved, not duplicated; a pin on the 'nc' list "
                "comes off it."),
            "items": {
                "type": "object",
                "properties": {"net": {"type": "string"}, "pins": _REFS},
                "required": ["net", "pins"],
            },
        },
    },
}

_HOLE_DOC = ("Terminal holes a1…j<columns> (rows a–e and f–j are two separate "
             "nodes per column, the centre channel between e and f is 0.3\" "
             "wide), rail holes T+<col>, T-<col>, B-<col>, B+<col>.")

BREADBOARD_SCHEMA = {
    "type": "object",
    "properties": {
        "board": {
            "type": "object",
            "description": "Optional. columns: 63 (full size, default) or 30 "
                           "(half size); split_rails: true if the rails are "
                           "interrupted in the middle.",
            "properties": {
                "columns": {"type": "integer"},
                "split_rails": {"type": "boolean"},
            },
        },
        "parts": {
            "type": "object",
            "description": (
                "{part ID: placement}. Rigid parts (DIP/SIP ICs, ne555, opamp, "
                "transistors, potentiometer, pico): {\"anchor\": hole of pin 1, "
                "\"rotation\": 0|90|180|270}. A DIP straddles the channel with "
                "pin 1 in row e and rotation 0 (pins run to the right along e, "
                "back along f); a Pico with pin 1 in row c and rotation 0 has "
                "pins 1–20 in row c and 40–21 in row h. Everything else: "
                "{\"legs\": {pin name: hole}}. Parts that do not sit on the "
                "board (antennas, speakers, batteries): {\"offboard\": true, "
                "\"legs\": {pin name: hole where its lead ends}}. " + _HOLE_DOC
            ),
        },
        "wires": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "from": {"type": "string"},
                    "to": {"type": "string"},
                    "color": {"type": "string",
                              "description": "Optional; by default GND is black, "
                                             "supplies red, signals colour-coded."},
                },
                "required": ["from", "to"],
            },
        },
    },
    "required": ["parts"],
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
        "name": "list_library",
        "description": (
            "List the real parts in the library (ICs, transistors, regulators, "
            "optocouplers …) with package and pinout from the datasheet. Use "
            "them in a circuit as {\"id\": \"U1\", \"part\": \"LM358\"} — pins "
            "and breadboard pinout then come for free. Optional 'query' filters "
            "by name or description."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
        },
    },
    {
        "name": "get_library_part",
        "description": "Show one library part: pins, package, pinout and the "
                       "datasheet it was taken from.",
        "inputSchema": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
    },
    {
        "name": "add_library_part",
        "description": (
            "Add a part that is missing from the library, so it stays "
            "available in CircuitStudio for good (stored in library/). "
            "Validated before it is stored.\n\n" + library.GUIDE
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "part": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "aliases": {"type": "array", "items": {"type": "string"},
                                    "description": "Other order codes, e.g. SN74HC04N"},
                        "type": {"type": "string", "enum": list(library.PART_TYPES)},
                        "description": {"type": "string"},
                        "package": {"type": "string",
                                    "description": "DIP-<n>, SIP-<n>, TO-92 or TO-220"},
                        "pins": {"type": "object",
                                 "description": "Only for type 'ic': schematic "
                                                "sides {left, right, top, bottom}"},
                        "pinout": {"type": "object",
                                   "description": "{pin name: physical pin number}"},
                        "source": {"type": "string",
                                   "description": "Datasheet URL + table/figure"},
                        "notes": {"type": "string"},
                    },
                    "required": ["name", "type", "package", "pinout", "source"],
                },
                "replace": {"type": "boolean",
                            "description": "Update a part added earlier"},
            },
            "required": ["part"],
        },
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
            "and the errors are returned instead. GND/VCC/label symbols work "
            "like in KiCad: put one next to each part that needs it instead "
            "of routing one long wire to a single symbol (see list_component_types)."
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
        "name": "update_circuit",
        "description": (
            "Change part of an existing netlist instead of rewriting all of it: "
            "add/replace/remove parts, nets and notes, connect or disconnect "
            "pins. Cheaper and less error-prone than write_circuit for edits. "
            "Same validation — on any error nothing is written. To tidy up a "
            "long wire, add a second GND/VCC/label symbol to the net next to "
            "the far pins — they are not wired to each other."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "changes": CHANGES_SCHEMA,
            },
            "required": ["project", "changes"],
        },
    },
    {
        "name": "get_breadboard",
        "description": "Read a project's breadboard plan (breadboard.json), if any.",
        "inputSchema": {
            "type": "object",
            "properties": {"project": {"type": "string"}},
            "required": ["project"],
        },
    },
    {
        "name": "write_breadboard",
        "description": (
            "Write the breadboard plan: which lead of every part goes into "
            "which hole, plus the jumper wires. The editor shows it in its "
            "'Breadboard' view. The plan is checked against the netlist and "
            "the result is returned: shorts, connections that are missing on "
            "the board, unused pins that sit in a live column. A plan that is "
            "broken in itself (unknown holes or parts, two leads in one hole, "
            "a DIP that does not fit) is NOT written. Parts with fixed pin "
            "spacing need 'package'/'pinout' in the circuit first (see "
            "list_component_types). GND/VCC/label symbols are not parts: "
            "bring their nets to the rails with wires. Once the human has "
            "rearranged the plan in the editor it is theirs: this tool then "
            "refuses, and changes go through update_breadboard (or, only if "
            "the human agrees to lose their arrangement, replace=true)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "breadboard": BREADBOARD_SCHEMA,
                "replace": {
                    "type": "boolean",
                    "description": "Overwrite a plan the human has edited. Ask "
                                   "them first — their arrangement is lost "
                                   "(a backup is kept).",
                },
            },
            "required": ["project", "breadboard"],
        },
    },
    {
        "name": "autoplace_breadboard",
        "description": (
            "Let CircuitStudio lay out the breadboard: chips along the centre "
            "channel, ground and supply on the rails, two-lead parts bridging "
            "the columns of their nets, jumpers for the rest. The result always "
            "passes the check; parts that cannot be placed (no pinout, no room) "
            "are listed. mode 'rest' (default) keeps everything already on the "
            "board and only adds — fine on a plan the human arranged. mode "
            "'all' starts over; on a human-edited plan it needs replace=true "
            "(ask them first). Creates the plan if there is none. A starting "
            "point for the human to rearrange, not an optimum — it knows "
            "nothing about RF layout, heat or mechanical fit."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "mode": {"type": "string", "enum": ["rest", "all"]},
                "replace": {"type": "boolean"},
            },
            "required": ["project"],
        },
    },
    {
        "name": "update_breadboard",
        "description": (
            "Change part of an existing breadboard plan and keep the rest — "
            "the way to work on a plan the human has already arranged. Place "
            "or move parts, unplug them, add or remove wires. Checked like "
            "write_breadboard: a change that breaks the plan itself (unknown "
            "hole, two leads in one hole, …) is not written; connection "
            "problems are reported."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "changes": {
                    "type": "object",
                    "properties": {
                        "place": {
                            "type": "object",
                            "description": "{part ID: placement} — added, or "
                                           "replacing where the part was. Same "
                                           "format as in write_breadboard.",
                        },
                        "remove_parts": {**_REFS, "description":
                                         "Part IDs to take off the board."},
                        "add_wires": {
                            "type": "array",
                            "items": BREADBOARD_SCHEMA["properties"]["wires"]["items"],
                        },
                        "remove_wires": {
                            "type": "array",
                            "description": "Wires to pull: {\"from\", \"to\"} "
                                           "(either direction), or a hole name — "
                                           "every wire ending there.",
                        },
                        "board": BREADBOARD_SCHEMA["properties"]["board"],
                    },
                },
            },
            "required": ["project", "changes"],
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
    {
        "name": "review_project",
        "description": (
            "Look at the result. Returns the rule-check warnings and, once the "
            "human has pressed 'Hand back' in the editor, a picture of the "
            "arrangement they actually made. Call this after the human says "
            "they are done — reviewing the automatic arrangement instead is "
            "pointless, it is only a starting point for them to drag around."
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
        lib_errors = library.check_spec(spec)
        if lib_errors:
            errors += lib_errors
            seen.add(cid)
            continue
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
        errors += footprints.check_spec(spec, comp)
        if "group" in spec and not isinstance(spec["group"], str):
            errors.append(f"Part '{cid}': 'group' must be a string")
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

    for ref in circuit.get("nc", []) or []:
        ref = str(ref)
        if "." not in ref:
            errors.append(f"'nc': '{ref}' is not an 'ID.Pin' reference")
            continue
        cid, pin = ref.split(".", 1)
        comp = comps.get(cid)
        if comp is None:
            errors.append(f"'nc': part '{cid}' does not exist")
            continue
        try:
            comp.pin(pin)
        except KeyError:
            available = ", ".join(p.name for p in comp.pins)
            errors.append(
                f"'nc': '{cid}' has no pin '{pin}'. Available: {available}")
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
    lines.append("")
    lines.append("Net ties: " + _TIE_DOC + " The rule check warns about a "
                 "symbol that serves no pin, a label whose value differs from "
                 "its net's name, and ground/supply symbols on a net whose "
                 "name says otherwise.")
    lines.append("")
    lines += footprints.summary()
    lines.append("")
    lines.append(f"Library: {len(library.catalog())} real parts with datasheet "
                 f"pinouts (list_library) — write {{\"id\": \"U1\", \"part\": "
                 f"\"LM358\"}} instead of pins/package/pinout. Missing one? "
                 f"add_library_part.")
    return "\n".join(lines)


def tool_list_library(args: dict[str, Any]) -> str:
    query = str(args.get("query") or "").strip().lower()
    entries = [e for e in library.catalog()
               if not query or query in (e["name"] + " " + " ".join(e.get("aliases") or [])
                                         + " " + e.get("description", "")).lower()]
    if not entries:
        return (f"No library part matches '{query}'. Add it with add_library_part."
                if query else "The library is empty.")
    lines = [f"{len(entries)} part(s) — use as {{\"id\": \"U1\", \"part\": \"<name>\"}}:"]
    lines += [library.describe(e) for e in entries]
    lines.append("")
    lines.append("Missing a part? add_library_part adds it for good (see its guide).")
    return "\n".join(lines)


def tool_get_library_part(args: dict[str, Any]) -> str:
    entry = library.find(str(args.get("name", "")))
    if entry is None:
        return (f"'{args.get('name')}' is not in the library — list_library shows "
                f"what is; add_library_part adds a missing part.")
    shown = {k: v for k, v in entry.items() if k != "file"}
    return json.dumps(shown, indent=2, ensure_ascii=False)


def tool_add_library_part(args: dict[str, Any]) -> str:
    entry = args.get("part")
    path, errors = library.save_user_part(entry if isinstance(entry, dict) else {},
                                          replace=bool(args.get("replace")))
    if errors:
        return ("NOT added — please fix:\n" + "\n".join(f"- {e}" for e in errors)
                + "\n\n" + library.GUIDE)
    return (f"Added {entry['name']} to the library: {path}\n"
            f"Use it as {{\"id\": \"U1\", \"part\": \"{entry['name']}\"}}. It stays "
            f"available in every project. Tell the human it was added from "
            f"{entry['source']} so they can double-check the pinout.")


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

    return _save(name, circuit)


def _save(name: str, circuit: dict[str, Any], preface: list[str] | None = None) -> str:
    """Validate, write circuit.json, place what is new, and report back."""
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
    msg = list(preface or []) + [
        f"Written: {project.circuit_path}",
        f"{len(scene.components)} parts, {len(scene.nets)} nets"
        + (f", {len(scene.notes)} notes." if scene.notes else "."),
    ]
    if new_ids:
        msg.append(f"Not arranged yet (marked orange in the editor): "
                   f"{', '.join(sorted(new_ids))}")
    else:
        msg.append("All parts already have a position from the layout.")

    warnings = erc_check(circuit, scene.components)
    if warnings:
        msg.append("")
        msg.append(format_report(warnings))
    project.load_breadboard()
    if project.breadboard is not None:
        # A changed netlist can break a breadboard plan that used to be fine.
        msg.append("")
        msg.append(Breadboard(project.breadboard, circuit).report())
    msg.append("")
    msg.append("The human now arranges the parts in the editor (open_editor). "
               "When they say they are done, call review_project to see what "
               "they made of it.")
    return "\n".join(msg)


def _strs(changes: dict[str, Any], key: str) -> list[str]:
    value = changes.get(key) or []
    if not isinstance(value, list):
        raise ValueError(f"'{key}' must be a list.")
    return [str(v) for v in value]


def _objs(changes: dict[str, Any], key: str) -> list[dict[str, Any]]:
    value = changes.get(key) or []
    if not isinstance(value, list) or not all(isinstance(v, dict) for v in value):
        raise ValueError(f"'{key}' must be a list of objects.")
    return value


def apply_changes(circuit: dict[str, Any],
                  changes: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Return a patched copy of `circuit` plus a human-readable change log.

    Removing something that is not there is reported, not treated as an
    error: the model's picture of the file may simply be slightly stale.
    """
    circuit = json.loads(json.dumps(circuit))       # deep copy, JSON-safe
    comps: list[dict[str, Any]] = [c for c in circuit.get("components", [])
                                   if isinstance(c, dict)]
    nets: list[dict[str, Any]] = [n for n in circuit.get("nets", [])
                                  if isinstance(n, dict)]
    notes: list[dict[str, Any]] = [n for n in circuit.get("notes", []) or []
                                   if isinstance(n, dict)]
    nc: list[str] = [str(r) for r in circuit.get("nc", []) or []]
    log: list[str] = []

    def net_named(name: str) -> dict[str, Any] | None:
        return next((n for n in nets if str(n.get("name")) == name), None)

    def drop_pins(pred) -> list[str]:
        dropped = []
        for n in nets:
            pins = [str(r) for r in n.get("pins", [])]
            keep = [r for r in pins if not pred(r)]
            dropped += [r for r in pins if pred(r)]
            n["pins"] = keep
        return dropped

    if "title" in changes:
        circuit["title"] = str(changes["title"])
        log.append(f"title set to '{circuit['title']}'")

    for cid in _strs(changes, "remove_components"):
        before = len(comps)
        comps = [c for c in comps if str(c.get("id")) != cid]
        if len(comps) == before:
            log.append(f"part {cid}: not found, nothing removed")
            continue
        dropped = drop_pins(lambda r, cid=cid: r.split(".", 1)[0] == cid)
        dropped += [r for r in nc if r.split(".", 1)[0] == cid]
        nc = [r for r in nc if r.split(".", 1)[0] != cid]
        log.append(f"removed part {cid}"
                   + (f" and its connections {', '.join(dropped)}" if dropped else ""))

    for name in _strs(changes, "remove_nets"):
        before = len(nets)
        nets = [n for n in nets if str(n.get("name")) != name]
        log.append(f"removed net {name}" if len(nets) < before
                   else f"net {name}: not found, nothing removed")

    for nid in _strs(changes, "remove_notes"):
        before = len(notes)
        notes = [n for n in notes if str(n.get("id")) != nid]
        log.append(f"removed note {nid}" if len(notes) < before
                   else f"note {nid}: not found, nothing removed")

    for ref in _strs(changes, "remove_nc"):
        if ref in nc:
            nc = [r for r in nc if r != ref]
            log.append(f"{ref} no longer marked as no-connect")
        else:
            log.append(f"{ref}: was not marked as no-connect")

    for ref in _strs(changes, "disconnect"):
        dropped = drop_pins(lambda r, ref=ref: r == ref)
        log.append(f"disconnected {ref}" if dropped else f"{ref}: was not connected")

    for spec in _objs(changes, "upsert_components"):
        cid = str(spec.get("id", ""))
        idx = next((i for i, c in enumerate(comps) if str(c.get("id")) == cid), None)
        if idx is None:
            comps.append(spec)
            log.append(f"added part {cid}")
        else:
            comps[idx] = spec
            log.append(f"replaced part {cid}")

    for spec in _objs(changes, "upsert_nets"):
        name = str(spec.get("name", ""))
        idx = next((i for i, n in enumerate(nets) if str(n.get("name")) == name), None)
        if idx is None:
            nets.append(spec)
            log.append(f"added net {name}")
        else:
            nets[idx] = spec
            log.append(f"replaced net {name}")

    for spec in _objs(changes, "upsert_notes"):
        nid = str(spec.get("id", ""))
        idx = next((i for i, n in enumerate(notes) if str(n.get("id")) == nid), None)
        if idx is None:
            notes.append(spec)
            log.append(f"added note {nid}")
        else:
            notes[idx] = spec
            log.append(f"replaced note {nid}")

    for item in _objs(changes, "connect"):
        name = str(item.get("net", ""))
        refs = [str(r) for r in item.get("pins", []) or []]
        target = net_named(name)
        if target is None:
            target = {"name": name, "pins": []}
            nets.append(target)
        for ref in refs:
            for n in nets:
                if n is not target and ref in [str(r) for r in n.get("pins", [])]:
                    n["pins"] = [r for r in n["pins"] if str(r) != ref]
                    log.append(f"moved {ref} from net {n.get('name')} to {name}")
            if ref not in [str(r) for r in target["pins"]]:
                target["pins"].append(ref)
            if ref in nc:
                nc = [r for r in nc if r != ref]
                log.append(f"{ref} was marked no-connect; not any more")
        log.append(f"net {name}: connected {', '.join(refs)}")

    for ref in _strs(changes, "add_nc"):
        if ref not in nc:
            nc.append(ref)
        log.append(f"{ref} marked as deliberately unconnected")

    empty = [str(n.get("name")) for n in nets if not n.get("pins")]
    if empty:
        nets = [n for n in nets if n.get("pins")]
        log.append(f"dropped nets left without pins: {', '.join(empty)}")

    circuit["components"] = comps
    circuit["nets"] = nets
    circuit["notes"] = notes
    if nc or "nc" in circuit:
        circuit["nc"] = nc
    return circuit, log


def tool_update_circuit(args: dict[str, Any]) -> str:
    name = _require_name(args)
    changes = args.get("changes")
    if not isinstance(changes, dict):
        raise ValueError("'changes' must be an object.")
    path = PROJECTS_DIR / f"{name}.circuit.json"
    if not path.exists():
        return (f"Project '{name}' does not exist yet — create it with "
                f"write_circuit first.")
    try:
        current = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return (f"{path.name} is not valid JSON ({exc}); rewrite it completely "
                f"with write_circuit.")
    if not isinstance(current, dict):
        return f"{path.name} is not a JSON object; rewrite it with write_circuit."
    circuit, log = apply_changes(current, changes)
    if not log:
        return "No changes given — nothing written."
    result = _save(name, circuit, preface=["Changes:"] + [f"- {l}" for l in log])
    if result.startswith("NOT written"):
        return result + "\n\nChanges that were attempted:\n" + "\n".join(
            f"- {l}" for l in log)
    return result


def tool_get_breadboard(args: dict[str, Any]) -> str:
    name = _require_name(args)
    project = Project(PROJECTS_DIR, name)
    if not project.breadboard_path.exists():
        return (f"Project '{name}' has no breadboard plan yet — write one with "
                f"write_breadboard.")
    return project.breadboard_path.read_text(encoding="utf-8")


def tool_write_breadboard(args: dict[str, Any]) -> str:
    name = _require_name(args)
    plan = args.get("breadboard")
    if not isinstance(plan, dict):
        raise ValueError("'breadboard' must be an object.")
    project = Project(PROJECTS_DIR, name)
    if not project.circuit_path.exists():
        return f"Project '{name}' does not exist yet — write the circuit first."
    project.load()
    current = project.breadboard or {}
    if current.get(HUMAN_EDIT) and not args.get("replace"):
        return (f"NOT written — the human rearranged this breadboard in the editor "
                f"({current[HUMAN_EDIT]}), and a new plan would throw that away. "
                f"Use update_breadboard for targeted changes (get_breadboard shows "
                f"the current state). Only if the human explicitly wants to start "
                f"over, call write_breadboard again with replace=true.")
    plan = {k: v for k, v in plan.items() if k != HUMAN_EDIT}
    bb = Breadboard(plan, project.circuit)
    if bb.structure_errors:
        return ("NOT written — please fix:\n"
                + "\n".join(f"- {e}" for e in bb.structure_errors))
    project.save_breadboard(plan, backup=True)
    return "\n".join([
        f"Written: {project.breadboard_path}",
        bb.report(),
        "",
        "The human sees it in the editor's 'Breadboard' view (open_editor).",
    ])


def apply_breadboard_changes(plan: dict[str, Any],
                             changes: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Patched copy of a breadboard plan plus a change log."""
    plan = json.loads(json.dumps(plan))
    parts = plan.get("parts")
    parts = dict(parts) if isinstance(parts, dict) else {}
    wires = [w for w in plan.get("wires") or [] if isinstance(w, dict)]
    log: list[str] = []

    board = changes.get("board")
    if isinstance(board, dict):
        merged = dict(plan.get("board") or {})
        merged.update(board)
        plan["board"] = merged
        log.append(f"board: {', '.join(f'{k}={v}' for k, v in board.items())}")

    for cid in _strs(changes, "remove_parts"):
        if parts.pop(cid, None) is None:
            log.append(f"{cid}: was not on the board")
        else:
            log.append(f"unplugged {cid}")

    place = changes.get("place") or {}
    if not isinstance(place, dict):
        raise ValueError("'place' must be an object {part ID: placement}.")
    for cid, entry in place.items():
        log.append(f"{'moved' if cid in parts else 'placed'} {cid}")
        parts[str(cid)] = entry

    def norm(h: Any) -> str:
        hole = parse_hole(h, 999)
        return hole.name if hole else str(h)

    removals = changes.get("remove_wires") or []
    if not isinstance(removals, list):
        raise ValueError("'remove_wires' must be a list.")
    for item in removals:
        before = len(wires)
        if isinstance(item, dict):
            ends = {norm(item.get("from")), norm(item.get("to"))}
            wires = [w for w in wires if {norm(w.get("from")), norm(w.get("to"))} != ends]
            what = f"wire {item.get('from')} – {item.get('to')}"
        else:
            hole = norm(item)
            wires = [w for w in wires if hole not in (norm(w.get("from")), norm(w.get("to")))]
            what = f"wires at {item}"
        log.append(f"removed {what}" if len(wires) < before else f"{what}: none found")

    for w in _objs(changes, "add_wires"):
        wires.append(w)
        log.append(f"added wire {w.get('from')} – {w.get('to')}")

    plan["parts"] = parts
    plan["wires"] = wires
    return plan, log


def tool_autoplace_breadboard(args: dict[str, Any]) -> str:
    name = _require_name(args)
    mode = args.get("mode") or "rest"
    if mode not in ("rest", "all"):
        raise ValueError("'mode' must be 'rest' or 'all'.")
    project = Project(PROJECTS_DIR, name)
    if not project.circuit_path.exists():
        return f"Project '{name}' does not exist yet — write the circuit first."
    project.load()
    current = project.breadboard or {}
    if mode == "all" and current.get(HUMAN_EDIT) and not args.get("replace"):
        return (f"NOT changed — the human arranged this breadboard "
                f"({current[HUMAN_EDIT]}); 'all' would throw that away. Use "
                f"mode 'rest' to add what is missing, or replace=true if they "
                f"want to start over.")
    before = len(current.get("parts") or {}) if mode == "rest" else 0
    plan, log, failed = autoplace(project.circuit, project.breadboard, mode)
    project.save_breadboard(plan, backup=True)
    bb = Breadboard(plan, project.circuit)
    lines = [f"Written: {project.breadboard_path}",
             f"Placed {len(plan['parts']) - before} part(s), "
             f"{len(plan['wires'])} wire(s) on the board now."]
    if failed:
        lines.append("Could not place:")
        lines += [f"- {f}" for f in failed]
    lines += ["", bb.report(), "",
              "The human sees it in the editor's 'Breadboard' view and can "
              "rearrange it from there."]
    return "\n".join(lines)


def tool_update_breadboard(args: dict[str, Any]) -> str:
    name = _require_name(args)
    changes = args.get("changes")
    if not isinstance(changes, dict):
        raise ValueError("'changes' must be an object.")
    project = Project(PROJECTS_DIR, name)
    if not project.circuit_path.exists():
        return f"Project '{name}' does not exist yet — write the circuit first."
    project.load()
    if project.breadboard is None:
        return (project.breadboard_error or
                f"Project '{name}' has no breadboard plan yet — write one with "
                f"write_breadboard.")
    before = set(Breadboard(project.breadboard, project.circuit).structure_errors)
    plan, log = apply_breadboard_changes(project.breadboard, changes)
    if not log:
        return "No changes given — nothing written."
    bb = Breadboard(plan, project.circuit)
    new = [e for e in bb.structure_errors if e not in before]
    if new:
        return ("NOT written — the change would break the plan:\n"
                + "\n".join(f"- {e}" for e in new)
                + "\n\nChanges that were attempted:\n"
                + "\n".join(f"- {l}" for l in log))
    project.save_breadboard(plan)
    return "\n".join(["Changes:"] + [f"- {l}" for l in log] + [
        f"Written: {project.breadboard_path}", bb.report()])


def _open_browser(url: str) -> None:
    """webbrowser may start helpers that inherit our stdout (the protocol
    channel), so do it from a child whose output goes nowhere."""
    subprocess.Popen(
        [sys.executable, "-c", "import sys, webbrowser; webbrowser.open(sys.argv[1])", url],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def tool_open_editor(args: dict[str, Any]) -> str:
    name = _require_name(args)
    running = find_running_editor(PROJECTS_DIR, name)
    if running:
        _open_browser(running)
        return (f"The editor for '{name}' is already running at {running} — "
                f"opened it in the browser instead of starting a second one.")
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


MAX_IMAGE_BYTES = 4 * 1024 * 1024


def tool_review_project(args: dict[str, Any]) -> list[dict[str, Any]]:
    """Rule check plus, if it exists, the picture the human handed back."""
    name = _require_name(args)
    project = Project(PROJECTS_DIR, name)
    if not project.circuit_path.exists():
        return [_text(f"Project '{name}' does not exist yet.")]
    project.load()
    scene = Scene(project)
    state = project.review_state()

    lines = [f"Project '{name}': {len(scene.components)} parts, "
             f"{len(scene.nets)} nets."]
    if scene.errors:
        lines.append("")
        lines.append("Problems while drawing:")
        lines += [f"- {e}" for e in scene.errors]
    lines.append("")
    # scene.erc also knows the arrangement, e.g. a GND symbol left hanging.
    lines.append(format_report(scene.erc))
    lines.append("")
    bb_blocks = _breadboard_review(project)

    if not state["reviewed"]:
        lines.append(
            "The human has not handed this arrangement back yet, so there is no "
            "picture. What the auto-placement produced is not worth looking at — "
            "it only exists so they have something to drag around. Ask them to "
            "press 'Hand back' in the editor when the layout is ready.")
        return [_text("\n".join(lines))] + bb_blocks

    if state["current"]:
        lines.append(f"Handed back {state['at']} — the picture below is the "
                     f"arrangement as it stands.")
    else:
        lines.append(f"Handed back {state['at']}, but the layout has been "
                     f"changed since. The picture below is the older state; ask "
                     f"the human to hand it back again if that matters.")

    out: list[dict[str, Any]] = [_text("\n".join(lines))]
    png = project.png_path
    if png.exists():
        data = png.read_bytes()
        if len(data) <= MAX_IMAGE_BYTES:
            out.append({"type": "image",
                        "data": base64.b64encode(data).decode("ascii"),
                        "mimeType": "image/png"})
        else:
            out.append(_text(f"(The picture is {len(data) // 1024} KB, too "
                             f"large to inline. It is at {png}.)"))
    else:
        out.append(_text(f"(No picture file — the browser could not rasterise "
                         f"the drawing. The SVG is at {project.svg_path}.)"))
    return out + bb_blocks


def _image(path: Path) -> dict[str, Any]:
    data = path.read_bytes()
    if len(data) > MAX_IMAGE_BYTES:
        return _text(f"(The picture is {len(data) // 1024} KB, too large to "
                     f"inline. It is at {path}.)")
    return {"type": "image", "data": base64.b64encode(data).decode("ascii"),
            "mimeType": "image/png"}


def _breadboard_review(project: Project) -> list[dict[str, Any]]:
    """The breadboard check, plus the human's picture of it if handed back."""
    if project.breadboard is None and not project.breadboard_error:
        return []
    if project.breadboard is None:
        return [_text(str(project.breadboard_error))]
    lines = [Breadboard(project.breadboard, project.circuit).report()]
    if project.breadboard.get(HUMAN_EDIT):
        lines.append(f"The human last changed the plan in the editor at "
                     f"{project.breadboard[HUMAN_EDIT]} — change it with "
                     f"update_breadboard, not write_breadboard.")
    state = project.breadboard_review_state()
    png = project.breadboard_review_png_path
    if not state["reviewed"] or not png.exists():
        return [_text("\n".join(lines))]
    lines.append(f"Breadboard handed back {state['at']}"
                 + (" — the picture below is the plan as it stands."
                    if state["current"] else
                    ", but it has been changed since; the picture is older."))
    return [_text("\n".join(lines)), _image(png)]


def _text(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text}


HANDLERS = {
    "list_projects": tool_list_projects,
    "list_library": tool_list_library,
    "get_library_part": tool_get_library_part,
    "add_library_part": tool_add_library_part,
    "list_component_types": tool_list_component_types,
    "get_circuit": tool_get_circuit,
    "write_circuit": tool_write_circuit,
    "update_circuit": tool_update_circuit,
    "get_breadboard": tool_get_breadboard,
    "write_breadboard": tool_write_breadboard,
    "update_breadboard": tool_update_breadboard,
    "autoplace_breadboard": tool_autoplace_breadboard,
    "open_editor": tool_open_editor,
    "review_project": tool_review_project,
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
            result = handler(args)
            # Handlers return either plain text or ready-made content blocks
            # (review_project adds an image).
            content = (result if isinstance(result, list)
                       else [{"type": "text", "text": result}])
            return _result(msg_id, {"content": content})
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
