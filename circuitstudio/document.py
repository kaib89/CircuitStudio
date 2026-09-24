"""The two-document model.

`<name>.circuit.json`  — logic only: components + nets. Owned by the LLM.
`<name>.layout.json`   — looks only: positions, rotation, view. Owned by the human.

Keeping them apart is what makes the workflow work: the LLM can rewrite the
circuit without destroying a hand-tuned layout, and dragging things around never
touches the electrical description.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .registry import build_component
from .symbols import Component

DEFAULT_GRID = 20
AUTO_STEP = 140  # px between auto-placed components
DEFAULT_NOTE_W = 220

_SAFE_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# Net names that are treated as power rails
_VCC_NAMES = {"vcc", "vdd", "v+", "5v", "3v3", "3.3v", "9v", "12v", "24v", "vin", "vbat"}
_GND_NAMES = {"gnd", "vss", "v-", "0v", "agnd", "dgnd", "pgnd"}


def is_supply_net(name: str) -> bool:
    n = name.lower().strip()
    return n in _VCC_NAMES or n.startswith(("vcc", "vdd", "v+", "+", "5v", "3v"))


def is_ground_net(name: str) -> bool:
    n = name.lower().strip()
    return n in _GND_NAMES or n.startswith(("gnd", "vss"))


def net_color(name: str) -> str:
    if is_supply_net(name):
        return "#CC0000"
    if is_ground_net(name):
        return "#000000"
    return "#333333"


def net_width(name: str) -> float:
    if is_ground_net(name):
        return 2.5
    if is_supply_net(name):
        return 2.0
    return 1.5


def is_safe_name(name: str) -> bool:
    return bool(_SAFE_NAME.match(name))


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"{path.name}: expected a JSON object at top level")
    return data


def _write_json(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    tmp.replace(path)


class Project:
    def __init__(self, folder: Path, name: str):
        if not is_safe_name(name):
            raise ValueError(f"Invalid project name: {name!r}")
        self.folder = folder
        self.name = name
        self.circuit: dict[str, Any] = {"title": name, "components": [], "nets": []}
        self.layout: dict[str, Any] = {"grid": DEFAULT_GRID, "positions": {},
                                       "wires": {}, "view": None}
        self.version = 0
        self._circuit_mtime: float = 0.0
        self._backed_up = False
        self.routes_dirty = False    # set by Scene when it produced new wires

    # ── Paths ────────────────────────────────────────────────────────────────

    @property
    def circuit_path(self) -> Path:
        return self.folder / f"{self.name}.circuit.json"

    @property
    def layout_path(self) -> Path:
        return self.folder / f"{self.name}.layout.json"

    @property
    def layout_backup_path(self) -> Path:
        return self.folder / f"{self.name}.layout.bak.json"

    @property
    def svg_path(self) -> Path:
        return self.folder / f"{self.name}.svg"

    # ── Load / save ──────────────────────────────────────────────────────────

    def load(self) -> "Project":
        circuit = _read_json(self.circuit_path)
        if circuit is not None:
            self.circuit = circuit
            self._circuit_mtime = self.circuit_path.stat().st_mtime
        self.circuit.setdefault("title", self.name)
        self.circuit.setdefault("components", [])
        self.circuit.setdefault("nets", [])
        self.circuit.setdefault("notes", [])
        return self.load_layout()

    def load_layout(self) -> "Project":
        """Load only layout.json (plus auto-placement for the current circuit)."""
        layout = _read_json(self.layout_path)
        if layout is not None:
            self.layout = layout
        self.layout.setdefault("grid", DEFAULT_GRID)
        self.layout.setdefault("showGrid", True)
        self.layout.setdefault("positions", {})
        self.layout.setdefault("wires", {})
        self.layout.setdefault("notes", {})
        self.layout.setdefault("routes", {})
        self.layout.setdefault("view", None)

        self.autoplace()
        self.autoplace_notes()
        self.version += 1
        return self

    def reload_circuit_if_changed(self) -> bool:
        """Pick up out-of-band edits (LLM writing circuit.json). Layout survives."""
        if not self.circuit_path.exists():
            return False
        mtime = self.circuit_path.stat().st_mtime
        if mtime == self._circuit_mtime:
            return False
        try:
            circuit = _read_json(self.circuit_path)
        except (json.JSONDecodeError, ValueError):
            return False  # half-written file; try again next poll
        if circuit is None:
            return False
        self._circuit_mtime = mtime
        self.circuit = circuit
        self.circuit.setdefault("title", self.name)
        self.circuit.setdefault("components", [])
        self.circuit.setdefault("nets", [])
        self.circuit.setdefault("notes", [])
        self.autoplace()
        self.autoplace_notes()
        self.version += 1
        return True

    def save_layout(self, backup: bool = False) -> None:
        """Write layout.json, keeping a recoverable copy of an older state.

        The backup is taken on the first save of this session (i.e. the layout
        as it was when the editor was opened, or before the assistant wrote the
        circuit) and again whenever `backup` is set, e.g. right before an
        auto-arrange. Backing up on *every* save would be useless: panning
        alone saves the view, so the copy would be overwritten within a second
        of the mistake it is meant to undo.
        """
        if (backup or not self._backed_up) and self.layout_path.exists():
            try:
                self.layout_backup_path.write_bytes(self.layout_path.read_bytes())
                self._backed_up = True
            except OSError:
                pass
        _write_json(self.layout_path, self.layout)

    def save_routes(self) -> None:
        """Persist wires a Scene just (re)computed, so they are reused next time."""
        if self.routes_dirty:
            self.routes_dirty = False
            self.save_layout()

    def clear_routes(self) -> None:
        """Forget all stored wires; the next Scene routes everything afresh."""
        self.layout["routes"] = {}
        self._route_cache = None
        self.version += 1

    def save_circuit(self) -> None:
        _write_json(self.circuit_path, self.circuit)
        self._circuit_mtime = self.circuit_path.stat().st_mtime

    # ── Placement ────────────────────────────────────────────────────────────

    def _adjacency(self) -> dict[str, set[str]]:
        adj: dict[str, set[str]] = {}
        for net in self.circuit.get("nets", []):
            comp_ids = [str(p).split(".")[0] for p in net.get("pins", [])]
            for i, a in enumerate(comp_ids):
                for b in comp_ids[i + 1:]:
                    if a != b:
                        adj.setdefault(a, set()).add(b)
                        adj.setdefault(b, set()).add(a)
        return adj

    def autoplace(self) -> list[str]:
        """Give every component without a stored position a rough spot.

        Stale entries for deleted components are deliberately kept — if the LLM
        re-adds the same ID later it lands back where you put it.
        """
        positions: dict[str, Any] = self.layout["positions"]
        ids = [str(s.get("id", "")) for s in self.circuit.get("components", [])]
        unplaced = [i for i in ids if i and i not in positions]
        if not unplaced:
            return []

        occupied: set[tuple[int, int]] = set()
        for cid in ids:
            p = positions.get(cid)
            if p:
                occupied.add((round(p.get("x", 0) / AUTO_STEP),
                              round(p.get("y", 0) / AUTO_STEP)))

        adj = self._adjacency()

        def free_cell(cell: tuple[int, int]) -> tuple[int, int]:
            if cell not in occupied:
                return cell
            for radius in range(1, 40):
                for dr in range(-radius, radius + 1):
                    for dc in range(-radius, radius + 1):
                        if abs(dr) != radius and abs(dc) != radius:
                            continue
                        cand = (cell[0] + dc, cell[1] + dr)
                        if cand not in occupied:
                            return cand
            return (cell[0] + len(occupied) + 1, cell[1])

        for cid in unplaced:
            anchors = [positions[n] for n in adj.get(cid, ()) if n in positions]
            if anchors:
                ax = sum(a.get("x", 0) for a in anchors) / len(anchors)
                ay = sum(a.get("y", 0) for a in anchors) / len(anchors)
                cell = (round(ax / AUTO_STEP), round(ay / AUTO_STEP))
            else:
                cell = (0, 0)
            cell = free_cell(cell)
            occupied.add(cell)
            positions[cid] = {
                "x": cell[0] * AUTO_STEP,
                "y": cell[1] * AUTO_STEP,
                "rotation": 0,
                "auto": True,
            }
        return unplaced

    def set_positions(self, updates: dict[str, Any]) -> None:
        """Store positions as given.

        Grid snapping happens in the editor, not here: pin pitches differ per
        symbol (ICs 40 px, two-terminal parts 50 px), so forcing every component
        onto the grid would make some pins impossible to line up. The editor
        decides; this only guards against float noise.
        """
        positions: dict[str, Any] = self.layout["positions"]
        for cid, p in updates.items():
            if not isinstance(p, dict):
                continue
            entry = positions.setdefault(cid, {})
            entry["x"] = round(float(p.get("x", entry.get("x", 0))), 1)
            entry["y"] = round(float(p.get("y", entry.get("y", 0))), 1)
            entry["rotation"] = int(p.get("rotation", entry.get("rotation", 0))) % 360
            entry["flip"] = bool(p.get("flip", entry.get("flip", False)))
            entry["locked"] = bool(p.get("locked", entry.get("locked", False)))
            entry.pop("auto", None)  # touched by a human => no longer auto
        self.version += 1

    def set_waypoints(self, edge: str, points: list[Any]) -> None:
        """Guide one wire (a pin-to-pin connection) through the given points.

        `edge` is the key produced by router.edge_key, e.g. "R1.2|U1.DIS".
        An empty list removes the guidance and returns the wire to auto-routing.

        Snapping is the editor's job (grid, or a pin's x/y when one is close):
        pins are often off the grid, and forcing waypoints onto it would put a
        jog into every wire that should run straight into such a pin.
        """
        wires: dict[str, Any] = self.layout.setdefault("wires", {})
        cleaned: list[list[float]] = []
        for p in points:
            if not isinstance(p, (list, tuple)) or len(p) != 2:
                continue
            try:
                cleaned.append([round(float(p[0]), 1), round(float(p[1]), 1)])
            except (TypeError, ValueError):
                continue
        if cleaned:
            wires[edge] = cleaned
        else:
            wires.pop(edge, None)
        self.version += 1

    # ── Notes ────────────────────────────────────────────────────────────────

    def notes(self) -> list[dict[str, Any]]:
        """Annotations authored in the circuit document (text + anchor)."""
        return [n for n in self.circuit.get("notes", []) if isinstance(n, dict)]

    def autoplace_notes(self) -> list[str]:
        """Give new notes a spot next to whatever they are anchored to.

        Notes anchored to nearby parts would otherwise land on top of each
        other, so an occupied slot pushes the next note further down.
        """
        placed: dict[str, Any] = self.layout.setdefault("notes", {})
        positions = self.layout["positions"]
        step = 95.0
        taken = [(float(v.get("x", 0)), float(v.get("y", 0)))
                 for v in placed.values() if isinstance(v, dict)]
        added: list[str] = []
        for n in self.notes():
            nid = str(n.get("id", ""))
            if not nid or nid in placed:
                continue
            anchor = str(n.get("anchor", "")).split(".")[0]
            base = positions.get(anchor)
            x = float(base.get("x", 0)) + 150 if base else 0.0
            y = float(base.get("y", 0)) - 110 if base else 0.0
            while any(abs(px - x) < DEFAULT_NOTE_W and abs(py - y) < step
                      for px, py in taken):
                y += step
            taken.append((x, y))
            placed[nid] = {"x": x, "y": y, "w": DEFAULT_NOTE_W, "hidden": False}
            added.append(nid)
        return added

    def set_note(self, note_id: str, x: Any = None, y: Any = None,
                 w: Any = None, hidden: Any = None) -> None:
        notes: dict[str, Any] = self.layout.setdefault("notes", {})
        entry = notes.setdefault(
            note_id, {"x": 0.0, "y": 0.0, "w": DEFAULT_NOTE_W, "hidden": False})
        if x is not None:
            entry["x"] = round(float(x), 1)
        if y is not None:
            entry["y"] = round(float(y), 1)
        if w is not None:
            entry["w"] = max(90.0, round(float(w), 1))
        if hidden is not None:
            entry["hidden"] = bool(hidden)
        self.version += 1

    # ── Instantiation ────────────────────────────────────────────────────────

    def instantiate(self) -> tuple[list[Component], list[str]]:
        """Build positioned symbol objects. Returns (components, errors)."""
        comps: list[Component] = []
        errors: list[str] = []
        positions = self.layout["positions"]
        for spec in self.circuit.get("components", []):
            try:
                comp = build_component(spec)
            except (ValueError, KeyError, TypeError) as exc:
                errors.append(str(exc))
                continue
            pos = positions.get(comp.comp_id, {})
            comp.x = float(pos.get("x", 0))
            comp.y = float(pos.get("y", 0))
            comp.rotation = int(pos.get("rotation", 0)) % 360
            comp.flip = bool(pos.get("flip", False))
            comps.append(comp)
        return comps, errors

    def auto_placed_ids(self) -> list[str]:
        return [cid for cid, p in self.layout["positions"].items() if p.get("auto")]


def list_projects(folder: Path) -> list[str]:
    if not folder.exists():
        return []
    names = [p.name[: -len(".circuit.json")]
             for p in folder.glob("*.circuit.json")]
    return sorted(n for n in names if is_safe_name(n))
