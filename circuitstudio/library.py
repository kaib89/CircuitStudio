"""Parts library: real parts with their pins and physical pinout.

Instead of spelling out pins and pin numbers, a circuit can name the part:

    {"id": "U1", "type": "ic", "part": "74HCU04"}
    {"id": "Q1", "type": "npn", "part": "BC547"}

and gets pins, package and pinout from the library. That removes the most
likely mistake — a pinout the model misremembered — for every part that is
in here.

Two sources, one format (one JSON file per part):

- circuitstudio/library/  built in, each entry checked against the datasheet
                          named in its "source"
- library/                (next to projects/) parts the assistant added with
                          add_library_part; kept in git like everything else

A library part never overrides a built-in one of the same name, and every
part must say where its pinout comes from.
"""
from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path
from typing import Any

BUILTIN_DIR = Path(__file__).resolve().parent / "library"
USER_DIR = Path(__file__).resolve().parent.parent / "library"

# Types a library part can have: the ones whose pins or pinout it can supply.
PART_TYPES = ("ic", "npn", "pnp", "nmos", "pmos", "opamp", "potentiometer")
_SAFE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+-]{0,39}$")
_SIDES = ("left", "right", "top", "bottom")

_cache: dict[str, Any] = {"key": None, "parts": {}}

GUIDE = """How to add a part to the library (add_library_part):
1. Find the MANUFACTURER datasheet (TI, onsemi, ST, Microchip, Nexperia, …),
   not a shop page or a hobby site. Pinouts differ between manufacturers
   for some parts (e.g. 2N2222 variants, BC547 is CBE while 2N3904 is EBC).
2. Pick the through-hole package that goes on a breadboard: DIP-<n> for ICs,
   TO-92/TO-220 (three leads in a row, written as SIP-3 or TO-92/TO-220).
3. Name the pins as the datasheet does ("1A", "1Y", "VCC", "GND"). Names
   must be unique — suffix repeats with the pin number (NC_4, GAIN_8).
4. For type 'ic' also give the schematic layout "pins": {"left": inputs and
   the rest, "right": outputs, "top": positive supply, "bottom": ground or
   negative supply}. Transistors (npn/pnp/nmos/pmos) have fixed pin names
   (emitter/base/collector, gate/drain/source) and need no "pins".
5. "pinout": {pin name: physical pin number} for EVERY pin, read from the
   package drawing seen from the top (DIP: notch left, pin 1 bottom left;
   TO-92/TO-220: printed side facing you, leads down, pin 1 on the left).
6. "source": the datasheet URL plus the table or figure you read it from.
   Without a source the part is not added. If anything was ambiguous, say
   so in "notes" and ask the human to double-check.
7. Then use it: {"id": "U3", "type": "ic", "part": "<name>"}."""


def norm(name: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(name).upper())


def _read_dir(folder: Path, origin: str) -> list[dict[str, Any]]:
    out = []
    if not folder.is_dir():
        return out
    for path in sorted(folder.glob("*.json")):
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue        # a broken file must not take the library down
        if isinstance(entry, dict) and entry.get("name"):
            entry["origin"] = origin
            entry["file"] = path.name
            out.append(entry)
    return out


def _signature() -> tuple:
    """Changes when a part file is added, replaced or removed — writes go
    through a rename, which touches the folder. The MCP server and the
    editor are separate processes, so each notices the other's additions."""
    sig = []
    for folder in (BUILTIN_DIR, USER_DIR):
        try:
            sig.append(folder.stat().st_mtime)
        except OSError:
            sig.append(None)
    return tuple(sig)


def parts() -> dict[str, dict[str, Any]]:
    """All parts by normalised name and alias. Built-in entries win."""
    key = _signature()
    if _cache["key"] == key:
        return _cache["parts"]
    table: dict[str, dict[str, Any]] = {}
    for entry in _read_dir(USER_DIR, "user") + _read_dir(BUILTIN_DIR, "builtin"):
        for name in [entry["name"]] + list(entry.get("aliases") or []):
            table[norm(name)] = entry     # built-in, read last, overrides
    _cache.update(key=key, parts=table)
    return table


def find(name: str) -> dict[str, Any] | None:
    return parts().get(norm(name))


def catalog() -> list[dict[str, Any]]:
    seen, out = set(), []
    for entry in parts().values():
        if id(entry) not in seen:
            seen.add(id(entry))
            out.append(entry)
    return sorted(out, key=lambda e: (e["origin"] != "builtin", e.get("type", ""),
                                      norm(e["name"])))


def expand(spec: dict[str, Any]) -> dict[str, Any]:
    """The spec with everything the named part supplies filled in.

    What the circuit says explicitly wins, so a part can still be adapted
    (another value, a different pinout for a second source).
    """
    name = spec.get("part")
    if not name:
        return spec
    entry = find(str(name))
    if entry is None:
        return spec
    out = dict(spec)
    out.setdefault("type", entry["type"])
    if not out.get("value"):
        out["value"] = entry["name"]
    for key in ("package", "pinout"):
        if key in entry:
            out.setdefault(key, entry[key])
    if entry["type"] == "ic" and "pins" in entry:
        out.setdefault("pins", entry["pins"])
    return out


def check_spec(spec: dict[str, Any]) -> list[str]:
    """Errors in a circuit part's use of 'part'."""
    name = spec.get("part")
    if name is None:
        return []
    cid = spec.get("id", "?")
    entry = find(str(name))
    if entry is None:
        known = ", ".join(e["name"] for e in catalog())
        return [f"Part '{cid}': '{name}' is not in the library. Known: {known}. "
                f"Add it with add_library_part (see its guide), or give pins, "
                f"package and pinout directly."]
    ctype = str(spec.get("type", entry["type"])).lower()
    if ctype != entry["type"]:
        return [f"Part '{cid}': {entry['name']} is a '{entry['type']}', not "
                f"'{ctype}' — use \"type\": \"{entry['type']}\" or leave type off"]
    return []


def validate(entry: Any) -> list[str]:
    """Everything that can be checked about a new part without the datasheet."""
    from .footprints import footprint, parse_package
    from .registry import build_component

    if not isinstance(entry, dict):
        return ["The part must be an object."]
    errors: list[str] = []
    name = str(entry.get("name", ""))
    if not _SAFE.match(name):
        errors.append("'name' is required: letters, digits and _ . + - "
                      "(e.g. \"74HC4051\", \"LM2596-ADJ\")")
    ctype = str(entry.get("type", ""))
    if ctype not in PART_TYPES:
        errors.append(f"'type' must be one of {', '.join(PART_TYPES)}")
    if len(str(entry.get("source", "")).strip()) < 12:
        errors.append("'source' is required: the manufacturer datasheet (URL) and "
                      "the table or figure the pinout was read from")
    aliases = entry.get("aliases", [])
    if not isinstance(aliases, list) or not all(isinstance(a, str) for a in aliases):
        errors.append("'aliases' must be a list of names")
    pkg = entry.get("package")
    if not isinstance(pkg, str) or parse_package(pkg) is None:
        errors.append("'package' must be DIP-<n>, SIP-<n>, TO-92 or TO-220")
    if ctype == "ic":
        pins = entry.get("pins")
        if not isinstance(pins, dict) or set(pins) - set(_SIDES) or not all(
                isinstance(v, list) for v in pins.values()):
            errors.append("type 'ic' needs \"pins\": {\"left\": [...], \"right\": "
                          "[...], \"top\": [...], \"bottom\": [...]}")
    elif "pins" in entry:
        errors.append(f"a '{ctype}' has fixed pin names — leave 'pins' off")
    if not isinstance(entry.get("pinout"), dict):
        errors.append("'pinout' is required: {pin name: physical pin number}")
    if errors:
        return errors

    spec = {"id": "X1", "type": ctype, "package": pkg, "pinout": entry["pinout"]}
    if ctype == "ic":
        spec["pins"] = entry["pins"]
    try:
        comp = build_component(spec)
    except (ValueError, TypeError) as exc:
        return [str(exc)]
    names = [p.name for p in comp.pins]
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        return [f"pin name(s) {', '.join(dupes)} used more than once — suffix "
                f"them with the pin number (NC_4)"]
    fp, fp_errors = footprint(spec, comp)
    errors += [e.replace("Part 'X1': ", "").replace("'X1' ", "the part ")
               for e in fp_errors]
    if fp is not None and len(fp.numbers) != fp.count and ctype == "ic":
        unused = sorted(set(range(1, fp.count + 1)) - set(fp.numbers.values()))
        errors.append(f"package {pkg} has pins {', '.join(map(str, unused))} that "
                      f"no pin name covers — name them too (e.g. NC_{unused[0]})")
    return errors


def save_user_part(entry: dict[str, Any], replace: bool = False) -> tuple[Path | None, list[str]]:
    """Validate and store a part in the user library."""
    errors = validate(entry)
    if errors:
        return None, errors
    name = entry["name"]
    for n in [name] + list(entry.get("aliases") or []):
        other = find(n)
        if other is None:
            continue
        if other["origin"] == "builtin":
            return None, [f"'{n}' is a built-in part ({other['name']}); the built-in "
                          f"one stays — pick another name"]
        if norm(other["name"]) != norm(name):
            return None, [f"'{n}' already names the library part {other['name']}"]
        if not replace:
            return None, [f"{other['name']} is already in the library (added "
                          f"{other.get('added', '?')}). Pass replace=true to update it."]
    stored = {k: v for k, v in entry.items() if k not in ("origin", "file")}
    stored["added"] = date.today().isoformat()
    USER_DIR.mkdir(parents=True, exist_ok=True)
    path = USER_DIR / f"{re.sub(r'[^A-Za-z0-9_.+-]', '_', name)}.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(stored, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)
    _cache["key"] = None
    return path, []


def describe(entry: dict[str, Any]) -> str:
    """One line per part for listings."""
    origin = "" if entry["origin"] == "builtin" else f"  [added {entry.get('added', '?')}]"
    alias = f" (also {', '.join(entry['aliases'])})" if entry.get("aliases") else ""
    return (f"- {entry['name']}{alias}: {entry['type']}, {entry.get('package', '?')}"
            f" — {entry.get('description', '')}{origin}")
