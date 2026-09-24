"""The two-document model.

`<name>.circuit.json`  — logic only: components + nets. Owned by the LLM.
`<name>.layout.json`   — looks only: positions, rotation, view. Owned by the human.

Keeping them apart is what makes the workflow work: the LLM can rewrite the
circuit without destroying a hand-tuned layout, and dragging things around never
touches the electrical description.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from .registry import build_component
from .symbols import Component

DEFAULT_GRID = 20
AUTO_STEP = 60   # cell size of the auto-placement grid; big parts take several
AUTO_GAP = 25    # breathing room around a symbol, in px
DEFAULT_NOTE_W = 220

_SAFE_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# Net names that are treated as power rails
_VCC_NAMES = {"vcc", "vdd", "v+", "5v", "3v3", "3.3v", "9v", "12v", "24v", "vin", "vbat"}
_GND_NAMES = {"gnd", "vss", "v-", "0v", "agnd", "dgnd", "pgnd"}


def net_color(name: str) -> str:
    n = name.lower().strip()
    if n in _VCC_NAMES or any(n.startswith(p) for p in ("vcc", "vdd", "v+", "+", "5v", "3v")):
        return "#CC0000"
    if n in _GND_NAMES or n.startswith("gnd") or n.startswith("vss"):
        return "#000000"
    return "#333333"


def net_width(name: str) -> float:
    n = name.lower().strip()
    if n in _GND_NAMES or n.startswith("gnd") or n.startswith("vss"):
        return 2.5
    if n in _VCC_NAMES or any(n.startswith(p) for p in ("vcc", "vdd")):
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

    @property
    def png_path(self) -> Path:
        return self.folder / f"{self.name}.png"

    # ── Review ───────────────────────────────────────────────────────────────

    def layout_fingerprint(self) -> str:
        """Short digest of everything the human arranges.

        Lets `review` state say not just *that* the layout was signed off, but
        whether it has been touched since — a stale picture is worse than none.
        """
        payload = json.dumps(
            {"positions": self.layout.get("positions", {}),
             "wires": self.layout.get("wires", {}),
             "notes": self.layout.get("notes", {})},
            sort_keys=True, ensure_ascii=False)
        return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]

    def mark_reviewed(self) -> dict[str, Any]:
        """Record that the arrangement is finished and ready to be handed back."""
        info = {
            "at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "layout": self.layout_fingerprint(),
            "parts": len(self.circuit.get("components", [])),
        }
        self.layout["review"] = info
        self.version += 1
        return info

    def review_state(self) -> dict[str, Any]:
        info = self.layout.get("review")
        if not isinstance(info, dict):
            return {"reviewed": False, "current": False}
        return {
            "reviewed": True,
            "at": info.get("at", ""),
            "current": info.get("layout") == self.layout_fingerprint(),
        }

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
        self.circuit.setdefault("nc", [])

        layout = _read_json(self.layout_path)
        if layout is not None:
            self.layout = layout
        self.layout.setdefault("grid", DEFAULT_GRID)
        self.layout.setdefault("showGrid", True)
        self.layout.setdefault("positions", {})
        self.layout.setdefault("wires", {})
        self.layout.setdefault("notes", {})
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
        self.circuit.setdefault("nc", [])
        self.autoplace()
        self.autoplace_notes()
        self.version += 1
        return True

    def save_layout(self) -> None:
        # Keep the previous state around: an accidental auto-arrange or a bad
        # drag is otherwise unrecoverable once the editor is closed and the
        # in-memory undo stack is gone.
        if self.layout_path.exists():
            try:
                self.layout_backup_path.write_bytes(self.layout_path.read_bytes())
            except OSError:
                pass
        _write_json(self.layout_path, self.layout)

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

        Not a real placer — the whole point of CircuitStudio is that a human
        does the arranging. It only has to produce something readable enough to
        start dragging from, which means two things above all: nothing overlaps,
        and connected parts end up near each other.

        Stale entries for deleted components are deliberately kept — if the LLM
        re-adds the same ID later it lands back where you put it.
        """
        positions: dict[str, Any] = self.layout["positions"]
        specs = {str(s.get("id", "")): s for s in self.circuit.get("components", [])
                 if str(s.get("id", ""))}
        ids = list(specs)
        unplaced = [i for i in ids if i not in positions]
        if not unplaced:
            return []

        footprints = {cid: self._footprint_cells(specs[cid]) for cid in ids}
        occupied: set[tuple[int, int]] = set()
        for cid in ids:
            p = positions.get(cid)
            if p:
                self._occupy(occupied, self._to_cell(p), footprints[cid])

        adj = self._adjacency()

        def free_cell(cell: tuple[int, int], size: tuple[int, int]) -> tuple[int, int]:
            if not self._collides(occupied, cell, size):
                return cell
            for radius in range(1, 60):
                for dr in range(-radius, radius + 1):
                    for dc in range(-radius, radius + 1):
                        if abs(dr) != radius and abs(dc) != radius:
                            continue
                        cand = (cell[0] + dc, cell[1] + dr)
                        if not self._collides(occupied, cand, size):
                            return cand
            return (cell[0] + 2 * len(occupied) + 1, cell[1])

        for cid in self._placement_order(unplaced, adj, positions):
            anchors = [positions[n] for n in adj.get(cid, ()) if n in positions]
            if anchors:
                ax = sum(a.get("x", 0) for a in anchors) / len(anchors)
                ay = sum(a.get("y", 0) for a in anchors) / len(anchors)
                cell = (round(ax / AUTO_STEP), round(ay / AUTO_STEP))
                cell = self._preferred_cell(specs[cid], cell)
            else:
                cell = (0, 0)
            cell = free_cell(cell, footprints[cid])
            self._occupy(occupied, cell, footprints[cid])
            positions[cid] = {
                "x": cell[0] * AUTO_STEP,
                "y": cell[1] * AUTO_STEP,
                "rotation": 0,
                "auto": True,
            }
        return unplaced

    # ── Auto-placement helpers ───────────────────────────────────────────────

    @staticmethod
    def _to_cell(pos: dict[str, Any]) -> tuple[int, int]:
        return (round(float(pos.get("x", 0)) / AUTO_STEP),
                round(float(pos.get("y", 0)) / AUTO_STEP))

    def _footprint_cells(self, spec: dict[str, Any]) -> tuple[int, int]:
        """How many cells a symbol needs, labels included.

        The old placer used one fixed cell for everything, so an ESP32 board
        (144 px tall) landed inside a 140 px slot and overlapped its neighbour.
        """
        try:
            comp = build_component(spec)
        except (ValueError, KeyError, TypeError):
            return (1, 1)
        boxes = [comp.body_bbox(), *comp.label_boxes()]
        x0 = min(b[0] for b in boxes)
        y0 = min(b[1] for b in boxes)
        x1 = max(b[2] for b in boxes)
        y1 = max(b[3] for b in boxes)
        # Symbols sit at their centre, so the half-extent decides the span.
        half_w = max(abs(x0), abs(x1)) + AUTO_GAP
        half_h = max(abs(y0), abs(y1)) + AUTO_GAP
        return (max(1, int(math.ceil(2 * half_w / AUTO_STEP))),
                max(1, int(math.ceil(2 * half_h / AUTO_STEP))))

    @staticmethod
    def _cells_of(cell: tuple[int, int], size: tuple[int, int]):
        cw, ch = size
        x0 = cell[0] - (cw - 1) // 2
        y0 = cell[1] - (ch - 1) // 2
        for dx in range(cw):
            for dy in range(ch):
                yield (x0 + dx, y0 + dy)

    @classmethod
    def _occupy(cls, occupied: set[tuple[int, int]], cell: tuple[int, int],
                size: tuple[int, int]) -> None:
        occupied.update(cls._cells_of(cell, size))

    @classmethod
    def _collides(cls, occupied: set[tuple[int, int]], cell: tuple[int, int],
                  size: tuple[int, int]) -> bool:
        return any(c in occupied for c in cls._cells_of(cell, size))

    @staticmethod
    def _preferred_cell(spec: dict[str, Any],
                        cell: tuple[int, int]) -> tuple[int, int]:
        """Power symbols read best below (ground) or above (supply) their net."""
        ctype = str(spec.get("type", "")).lower()
        if ctype == "ground":
            return (cell[0], cell[1] + 1)
        if ctype in ("vcc", "vdd"):
            return (cell[0], cell[1] - 1)
        return cell

    @staticmethod
    def _placement_order(unplaced: list[str], adj: dict[str, set[str]],
                         positions: dict[str, Any]) -> list[str]:
        """Place well-connected parts first, then walk outwards along the nets.

        In document order a part is usually placed before any of its neighbours,
        so the anchor average has nothing to work with and everything piles up
        around the origin.
        """
        todo = set(unplaced)
        order: list[str] = []
        # Seeds: parts next to something already positioned, else the hub.
        while todo:
            seeds = sorted(
                (c for c in todo if any(n in positions or n in order
                                        for n in adj.get(c, ()))),
                key=lambda c: -len(adj.get(c, ())),
            )
            start = seeds[0] if seeds else max(
                sorted(todo), key=lambda c: len(adj.get(c, ())))
            queue = [start]
            while queue:
                cid = queue.pop(0)
                if cid not in todo:
                    continue
                todo.discard(cid)
                order.append(cid)
                queue.extend(sorted(n for n in adj.get(cid, ()) if n in todo))
        return order

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
        """
        wires: dict[str, Any] = self.layout.setdefault("wires", {})
        grid = max(1, int(self.layout.get("grid", DEFAULT_GRID)))
        cleaned: list[list[float]] = []
        for p in points:
            if not isinstance(p, (list, tuple)) or len(p) != 2:
                continue
            cleaned.append([round(float(p[0]) / grid) * grid,
                            round(float(p[1]) / grid) * grid])
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
