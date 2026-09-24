"""Turns a Project into geometry.

Everything the browser draws and everything the exported SVG contains is derived
from the *same* primitive lists here — that's what keeps the editor WYSIWYG.
"""
from __future__ import annotations

import json
from typing import Any

from .document import Project, is_ground_net, is_supply_net, net_color, net_width
from .router import (
    ROUTE_GRID, Router, Segment, find_junctions,
)
from .symbols import Component, mirror_symbol, xml_escape

PADDING = 80

# Note boxes. Text is wrapped here rather than in the browser so the editor and
# the exported SVG break lines at exactly the same place.
NOTE_FONT = 11
NOTE_LINE_H = 15
NOTE_PAD = 8
NOTE_CHAR_W = 0.55  # average glyph width of sans-serif, as a fraction of size
NOTE_FILL = "#FFF9E3"
NOTE_STROKE = "#C8A951"
NOTE_TEXT = "#4A3F1E"
NOTE_LEADER = "#B08A3E"


def wrap_note_text(text: str, width: float) -> list[str]:
    """Greedy word wrap to the box width; explicit newlines are kept."""
    usable = max(width - 2 * NOTE_PAD, 20)
    per_line = max(int(usable / (NOTE_FONT * NOTE_CHAR_W)), 4)
    lines: list[str] = []
    for raw in str(text).split("\n"):
        words = raw.split()
        if not words:
            lines.append("")
            continue
        cur = words[0]
        for word in words[1:]:
            if len(cur) + 1 + len(word) <= per_line:
                cur += " " + word
            else:
                lines.append(cur)
                cur = word
        lines.append(cur)
    return lines or [""]


def _net_priority(name: str, pin_count: int) -> tuple[int, int]:
    """GND first, then supply rails, then by size — big nets get clean runs."""
    if is_ground_net(name):
        return (0, -pin_count)
    if is_supply_net(name):
        return (1, -pin_count)
    return (2, -pin_count)


class Scene:
    def __init__(self, project: Project):
        self.project = project
        self.components, self.errors = project.instantiate()
        self._by_id: dict[str, Component] = {c.comp_id: c for c in self.components}

        self.nets: list[dict[str, Any]] = []
        for raw in project.circuit.get("nets", []):
            name = str(raw.get("name", ""))
            self.nets.append({
                "name": name,
                "pins": [str(p) for p in raw.get("pins", [])],
                "color": net_color(name),
                "width": net_width(name),
            })

        self.wires: list[list[Segment]] = [[] for _ in self.nets]
        self.net_edges: list[list[dict[str, Any]]] = [[] for _ in self.nets]
        self.net_pins: list[list[tuple[float, float]]] = [[] for _ in self.nets]
        self.junctions: set[tuple[float, float]] = set()
        self.rerouted = 0            # edges that needed a fresh A* search
        self._route()
        self.net_labels = self._build_net_labels()
        self.notes = self._build_notes()

    # ── Routing ──────────────────────────────────────────────────────────────

    def _pin_positions(self, net: dict[str, Any]) -> tuple[list[tuple[float, float]],
                                                           list[str]]:
        pts: list[tuple[float, float]] = []
        keys: list[str] = []
        for ref in net["pins"]:
            if "." not in ref:
                self.errors.append(f"Net '{net['name']}': bad pin ref '{ref}'")
                continue
            comp_id, pin_name = ref.split(".", 1)
            comp = self._by_id.get(comp_id)
            if comp is None:
                self.errors.append(
                    f"Net '{net['name']}': unknown component '{comp_id}'")
                continue
            try:
                pts.append(comp.abs_pin_pos(pin_name))
                keys.append(ref)
            except KeyError:
                self.errors.append(
                    f"Net '{net['name']}': '{comp_id}' has no pin '{pin_name}'")
        return pts, keys

    def _route_key(self) -> str:
        """Everything routing depends on — and nothing else, so moving a note,
        saving the view or toggling the grid reuses the previous routes."""
        positions = self.project.layout.get("positions", {})
        return json.dumps([
            self.project.circuit.get("components", []),
            [(c.comp_id, c.x, c.y, c.rotation, c.flip) for c in self.components],
            [(n["name"], n["pins"]) for n in self.nets],
            self.project.layout.get("wires") or {},
            sorted(positions),
        ], sort_keys=True, default=str)

    def _route(self) -> None:
        if not self.components:
            return
        key = self._route_key()
        cached = getattr(self.project, "_route_cache", None)
        if cached and cached[0] == key:
            (self.wires, self.net_edges, self.net_pins,
             self.junctions, pin_errors) = cached[1]
            self.errors.extend(pin_errors)
            return

        n_errors = len(self.errors)
        waypoints = self.project.layout.get("wires") or {}
        router = Router(self.components, grid=ROUTE_GRID, inflate=1)
        order = sorted(
            range(len(self.nets)),
            key=lambda i: _net_priority(self.nets[i]["name"], len(self.nets[i]["pins"])),
        )
        stored = self.project.layout.get("routes") or {}
        for i in order:
            pts, keys = self._pin_positions(self.nets[i])
            self.net_pins[i] = pts
            edges = router.route_net(pts, keys, waypoints=waypoints, stored=stored)
            self.net_edges[i] = edges
            self.wires[i] = [s for e in edges for leg in e["legs"] for s in leg]
        self.junctions = find_junctions(self.wires, self.net_pins)
        self.rerouted = router.routed
        if router.routes != stored:
            # Persisted by the caller (Project.save_routes) so the next run —
            # and the next session — starts from these wires.
            self.project.layout["routes"] = router.routes
            self.project.routes_dirty = True  # type: ignore[attr-defined]
        self.project._route_cache = (key, (  # type: ignore[attr-defined]
            self.wires, self.net_edges, self.net_pins, self.junctions,
            self.errors[n_errors:]))

    # ── Notes ─────────────────────────────────────────────────────────────

    def _anchor_point(self, ref: str) -> tuple[float, float] | None:
        if not ref:
            return None
        if "." in ref:
            comp_id, pin_name = ref.split(".", 1)
            comp = self._by_id.get(comp_id)
            if comp is None:
                return None
            try:
                return comp.abs_pin_pos(pin_name)
            except KeyError:
                return None
        comp = self._by_id.get(ref)
        return (comp.x, comp.y) if comp else None

    def _build_notes(self) -> list[dict[str, Any]]:
        layout_notes = self.project.layout.get("notes", {})
        out: list[dict[str, Any]] = []
        for spec in self.project.notes():
            nid = str(spec.get("id", ""))
            if not nid:
                self.errors.append("Note without an 'id' was skipped")
                continue
            place = layout_notes.get(nid, {})
            w = float(place.get("w", 220))
            lines = wrap_note_text(spec.get("text", ""), w)
            h = 2 * NOTE_PAD + len(lines) * NOTE_LINE_H
            x = float(place.get("x", 0))
            y = float(place.get("y", 0))

            anchor_ref = str(spec.get("anchor", "") or "")
            anchor = self._anchor_point(anchor_ref)
            if anchor_ref and anchor is None:
                self.errors.append(
                    f"Note '{nid}': anchor '{anchor_ref}' does not exist")

            leader = None
            if anchor:
                # start on the border of the box nearest to the anchor
                sx = min(max(anchor[0], x), x + w)
                sy = min(max(anchor[1], y), y + h)
                leader = [[sx, sy], [anchor[0], anchor[1]]]

            out.append({
                "id": nid,
                "text": spec.get("text", ""),
                "lines": lines,
                "x": x, "y": y, "w": w, "h": h,
                "anchor": list(anchor) if anchor else None,
                "leader": leader,
                "hidden": bool(place.get("hidden", False)),
            })
        return out

    # ── Net labels ───────────────────────────────────────────────────────────

    def _build_net_labels(self) -> list[dict[str, Any]]:
        boxes = []
        for c in self.components:
            m = 6
            x0, y0, x1, y1 = c.extent_bbox()
            boxes.append((x0 + m, y0 + m, x1 - m, y1 - m))

        def inside_any(x: float, y: float) -> bool:
            return any(x0 < x < x1 and y0 < y < y1 for x0, y0, x1, y1 in boxes)

        out: list[dict[str, Any]] = []
        for net, segs in zip(self.nets, self.wires):
            if not segs or len(net["pins"]) < 2:
                continue
            best = None
            for (x1, y1), (x2, y2) in segs:
                length = abs(x2 - x1) + abs(y2 - y1)
                if length < 35:
                    continue
                mx, my = (x1 + x2) / 2, (y1 + y2) / 2
                if inside_any(mx, my):
                    continue
                if best is None or length > best[0]:
                    best = (length, mx, my, abs(y1 - y2) < 0.5)
            if best is None:
                continue
            _, mx, my, horizontal = best
            name = net["name"]
            bg_w = max(len(name) * 5.5 + 6, 16)
            bg_h = 12
            if horizontal:
                bg_x, bg_y = mx - bg_w / 2, my - bg_h - 1
                tx, ty = mx, my - 4
            else:
                bg_x, bg_y = mx + 4, my - bg_h / 2
                tx, ty = mx + 4 + bg_w / 2, my + 3
            out.append({
                "text": name, "color": net["color"],
                "x": tx, "y": ty,
                "bg": {"x": bg_x, "y": bg_y, "w": bg_w, "h": bg_h},
            })
        return out

    # ── Geometry ─────────────────────────────────────────────────────────────

    def bounds(self) -> tuple[float, float, float, float]:
        if not self.components:
            return (0.0, 0.0, 400.0, 300.0)
        boxes = [c.extent_bbox() for c in self.components]
        x0 = min(b[0] for b in boxes)
        y0 = min(b[1] for b in boxes)
        x1 = max(b[2] for b in boxes)
        y1 = max(b[3] for b in boxes)
        # A* may detour around a part and leave the component area.
        for segs in self.wires:
            for (ax, ay), (bx, by) in segs:
                x0, x1 = min(x0, ax, bx), max(x1, ax, bx)
                y0, y1 = min(y0, ay, by), max(y1, ay, by)
        for lab in self.net_labels:
            bg = lab["bg"]
            x0, y0 = min(x0, bg["x"]), min(y0, bg["y"])
            x1, y1 = max(x1, bg["x"] + bg["w"]), max(y1, bg["y"] + bg["h"])
        for n in self.notes:
            if n["hidden"]:
                continue
            x0 = min(x0, n["x"])
            y0 = min(y0, n["y"])
            x1 = max(x1, n["x"] + n["w"])
            y1 = max(y1, n["y"] + n["h"])
        return (x0 - PADDING, y0 - PADDING,
                (x1 - x0) + 2 * PADDING, (y1 - y0) + 2 * PADDING)

    # ── Output ───────────────────────────────────────────────────────────────

    @staticmethod
    def symbol_svg(c: Component) -> str:
        """Symbol markup, mirrored if the component is flipped."""
        return mirror_symbol(c.svg_symbol()) if c.flip else c.svg_symbol()

    def _component_dicts(self) -> list[dict[str, Any]]:
        auto = set(self.project.auto_placed_ids())
        positions = self.project.layout.get("positions", {})
        out = []
        for c in self.components:
            bx0, by0, bx1, by1 = c._body_local()
            if c.flip:
                bx0, bx1 = -bx1, -bx0
            pad = 12
            out.append({
                "id": c.comp_id,
                "type": c.comp_type,
                "x": c.x, "y": c.y, "rotation": c.rotation, "flip": c.flip,
                "svg": self.symbol_svg(c),
                "hit": [bx0 - pad, by0 - pad,
                        (bx1 - bx0) + 2 * pad, (by1 - by0) + 2 * pad],
                "pins": [
                    {"name": p.name,
                     "x": c.abs_pin_pos(p.name)[0],
                     "y": c.abs_pin_pos(p.name)[1]}
                    for p in c.pins
                ],
                "auto": c.comp_id in auto,
                "locked": bool(positions.get(c.comp_id, {}).get("locked", False)),
            })
        return out

    def to_dict(self) -> dict[str, Any]:
        x, y, w, h = self.bounds()
        return {
            "title": self.project.circuit.get("title", self.project.name),
            "grid": self.project.layout.get("grid", 20),
            "viewBox": [x, y, w, h],
            "components": self._component_dicts(),
            "wires": [
                {"net": n["name"], "color": n["color"], "width": n["width"],
                 "pins": n["pins"], "segments": s, "edges": e}
                for n, s, e in zip(self.nets, self.wires, self.net_edges) if s
            ],
            "junctions": sorted(self.junctions),
            "netLabels": self.net_labels,
            "notes": self.notes,
            "errors": self.errors,
        }

    def note_svg(self, n: dict[str, Any]) -> str:
        """One annotation box plus its dashed leader.

        The leader is dashed and runs diagonally on purpose — wires here are
        always orthogonal and solid, so the two can never be confused.
        """
        parts: list[str] = []
        if n["leader"]:
            (sx, sy), (ax, ay) = n["leader"]
            parts.append(
                f'<line x1="{sx:.1f}" y1="{sy:.1f}" x2="{ax:.1f}" y2="{ay:.1f}" '
                f'stroke="{NOTE_LEADER}" stroke-width="1.2" stroke-dasharray="5 4"/>'
                f'<circle cx="{ax:.1f}" cy="{ay:.1f}" r="3" fill="{NOTE_LEADER}"/>'
            )
        parts.append(
            f'<rect x="{n["x"]:.1f}" y="{n["y"]:.1f}" width="{n["w"]:.1f}" '
            f'height="{n["h"]:.1f}" rx="5" fill="{NOTE_FILL}" '
            f'stroke="{NOTE_STROKE}" stroke-width="1.2"/>'
        )
        tx = n["x"] + NOTE_PAD
        ty = n["y"] + NOTE_PAD + NOTE_FONT
        spans = "".join(
            f'<tspan x="{tx:.1f}" dy="{0 if i == 0 else NOTE_LINE_H}">'
            f'{xml_escape(line)}</tspan>'
            for i, line in enumerate(n["lines"])
        )
        parts.append(
            f'<text x="{tx:.1f}" y="{ty:.1f}" font-family="sans-serif" '
            f'font-size="{NOTE_FONT}" fill="{NOTE_TEXT}">{spans}</text>'
        )
        return "".join(parts)

    def to_svg(self) -> str:
        x, y, w, h = self.bounds()
        parts = [
            f'<svg xmlns="http://www.w3.org/2000/svg" '
            f'viewBox="{x:.1f} {y:.1f} {w:.1f} {h:.1f}" '
            f'width="{w:.0f}" height="{h:.0f}">',
            f'<title>{xml_escape(self.project.circuit.get("title", self.project.name))}</title>',
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" fill="#FFFFFF"/>',
        ]

        for net, segs in zip(self.nets, self.wires):
            if not segs:
                continue
            lines = [f'<g id="net_{xml_escape(net["name"])}">']
            for (x1, y1), (x2, y2) in segs:
                lines.append(
                    f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" '
                    f'stroke="{net["color"]}" stroke-width="{net["width"]}" '
                    f'stroke-linecap="round"/>'
                )
            lines.append("</g>")
            parts.append("\n".join(lines))

        if self.junctions:
            parts.append(
                '<g id="junctions">'
                + "".join(f'<circle cx="{jx:.1f}" cy="{jy:.1f}" r="4" fill="#000000"/>'
                          for jx, jy in sorted(self.junctions))
                + "</g>"
            )

        for lab in self.net_labels:
            bg = lab["bg"]
            parts.append(
                f'<rect x="{bg["x"]:.1f}" y="{bg["y"]:.1f}" width="{bg["w"]:.1f}" '
                f'height="{bg["h"]}" fill="#ffffff" fill-opacity="0.92"/>'
                f'<text x="{lab["x"]:.1f}" y="{lab["y"]:.1f}" text-anchor="middle" '
                f'font-family="sans-serif" font-size="9" font-style="italic" '
                f'fill="{lab["color"]}">{xml_escape(lab["text"])}</text>'
            )

        for c in self.components:
            transform = f"translate({c.x:.1f},{c.y:.1f})"
            if c.rotation:
                transform += f" rotate({c.rotation})"
            parts.append(
                f'<g id="comp_{xml_escape(c.comp_id)}" transform="{transform}">\n'
                f'{self.symbol_svg(c)}\n</g>'
            )

        for n in self.notes:
            if not n["hidden"]:
                parts.append(f'<g id="note_{xml_escape(n["id"])}">'
                             f'{self.note_svg(n)}</g>')

        parts.append("</svg>")
        return "\n".join(parts)