"""Breadboard drawing — one SVG for the editor and the export alike.

Python owns the geometry here as well, so what the editor shows is exactly
what gets exported. The editor only adds hover highlighting on top, driven by
the `data-node` attributes: every hole, lead and wire carries the electrical
node it belongs to, so pointing at one lights up everything connected to it.
"""
from __future__ import annotations

import math
from typing import Any
from xml.sax.saxutils import escape as xml_escape

from .breadboard import RAILS, ROW_Y, Breadboard, Hole, Part
from .document import Project, is_ground_net, is_supply_net
from .footprints import RIGID

P = 18.0                      # one pitch (0.1") in px
RAIL_Y = {"T+": -3.3, "T-": -2.3, "B-": 13.3, "B+": 14.3}
BOARD_TOP, BOARD_BOTTOM = -4.4, 15.4
OFFBOARD_GAP = 2.8            # pitches between board edge and off-board parts

BOARD_FILL = "#F4F1EA"
_WIRE_PALETTE = ["#1F9E89", "#8E44AD", "#E67E22", "#2E86DE", "#16A085",
                 "#D35400", "#6C5CE7", "#B7950B"]
_LED_COLORS = [(("red", "rot"), "#E03131"), (("green", "grün", "gruen"), "#2F9E44"),
               (("blue", "blau"), "#1C7ED6"), (("yellow", "gelb"), "#F5C518"),
               (("white", "weiß", "weiss"), "#F1F3F5"), (("orange",), "#F76707")]
_BODY_LEN = {"resistor": 2.0, "inductor": 1.9, "fuse": 1.8, "crystal": 1.4,
             "diode": 1.5, "zener": 1.5, "capacitor_pol": 1.2}


def _f(v: float) -> str:
    return f"{v:.1f}"


def hole_xy(h: Hole) -> tuple[float, float]:
    y = RAIL_Y[h.row] if h.rail else ROW_Y[h.row]
    return h.col * P, y * P


def _esc(s: Any) -> str:
    return xml_escape(str(s), {'"': "&quot;"})


class BreadboardScene:
    def __init__(self, project: Project):
        self.project = project
        self.bb = Breadboard(project.breadboard, project.circuit)
        self.cols = self.bb.columns
        self._net_colors: dict[str, str] = {}
        self._offboard_bottom = BOARD_BOTTOM * P
        self._labels: list[str] = []     # drawn last, above parts and wires

    # ── helpers ──────────────────────────────────────────────────────────────

    def _node(self, h: Hole) -> str:
        return _esc(self.bb.root(h))

    def _net_color(self, net: str) -> str:
        if is_ground_net(net):
            return "#222222"
        if is_supply_net(net):
            return "#D63031"
        if net not in self._net_colors:
            self._net_colors[net] = _WIRE_PALETTE[len(self._net_colors) % len(_WIRE_PALETTE)]
        return self._net_colors[net]

    def _nets_at(self, h: Hole) -> list[str]:
        root = self.bb.root(h)
        pins = [p for part in self.bb.parts.values() for p in part.pins
                if self.bb.root(p.hole) == root]
        return self.bb.nets_in(pins)

    def bounds(self) -> tuple[float, float, float, float]:
        x0, x1 = -1.8 * P, (self.cols + 2.8) * P
        y0 = -7.2 * P
        y1 = max(self._offboard_bottom, BOARD_BOTTOM * P) + 1.2 * P
        return x0, y0, x1 - x0, y1 - y0

    # ── board ────────────────────────────────────────────────────────────────

    def _board(self, editor: bool) -> list[str]:
        n = self.cols
        out = [
            f'<rect x="{_f(-0.8 * P)}" y="{_f(BOARD_TOP * P)}" '
            f'width="{_f((n + 2.6) * P)}" height="{_f((BOARD_BOTTOM - BOARD_TOP) * P)}" '
            f'rx="10" fill="{BOARD_FILL}" stroke="#D6CFC0" stroke-width="1.5"/>',
            f'<rect x="{_f(-0.8 * P)}" y="{_f(4.75 * P)}" width="{_f((n + 2.6) * P)}" '
            f'height="{_f(1.5 * P)}" fill="#E6E0D4"/>',
        ]
        # Rail stripes: red outside, blue inside — as printed on most boards.
        halves = [(0.4, n + 0.6)]
        if self.bb.split_rails:
            mid = n // 2 + 0.5
            halves = [(0.4, mid - 0.4), (mid + 0.4, n + 0.6)]
        for y, color in ((-3.95, "#D63031"), (-1.65, "#2E6FD8"),
                         (12.65, "#2E6FD8"), (14.95, "#D63031")):
            for a, b in halves:
                out.append(f'<line x1="{_f(a * P)}" y1="{_f(y * P)}" x2="{_f(b * P)}" '
                           f'y2="{_f(y * P)}" stroke="{color}" stroke-width="1.6"/>')
        for rail, sign, color in (("T+", "+", "#D63031"), ("T-", "−", "#2E6FD8"),
                                  ("B-", "−", "#2E6FD8"), ("B+", "+", "#D63031")):
            y = RAIL_Y[rail] * P + 5
            for x in (-0.1 * P, (n + 1.1) * P):
                out.append(f'<text x="{_f(x)}" y="{_f(y)}" text-anchor="middle" '
                           f'font-family="sans-serif" font-size="14" font-weight="bold" '
                           f'fill="{color}">{sign}</text>')
        # Row letters and column numbers.
        for row, y in ROW_Y.items():
            for x in (-0.1 * P, (n + 1.1) * P):
                out.append(f'<text x="{_f(x)}" y="{_f(y * P + 4)}" text-anchor="middle" '
                           f'font-family="sans-serif" font-size="10" fill="#8C8577">'
                           f'{row}</text>')
        for c in range(1, n + 1):
            if c == 1 or c % 5 == 0:
                out.append(f'<text x="{_f(c * P)}" y="{_f(-5.0 * P)}" text-anchor="middle" '
                           f'font-family="sans-serif" font-size="10" fill="#8C8577">'
                           f'{c}</text>')

        if editor:
            out += self._node_hits()
        # Holes.
        holes = []
        for c in range(1, n + 1):
            for row in list(ROW_Y) + list(RAILS):
                h = Hole(f"{row}{c}", c, row)
                x, y = hole_xy(h)
                holes.append(f'<rect class="hole" x="{_f(x - 3)}" y="{_f(y - 3)}" '
                             f'width="6" height="6" rx="1" fill="#B9B2A4" '
                             f'data-node="{self._node(h)}" data-hole="{h.name}"/>')
        out.append('<g id="bb_holes">' + "".join(holes) + "</g>")
        return out

    def _node_hits(self) -> list[str]:
        """Invisible hover targets: one per half column and rail, so the
        pointer does not have to hit a 6 px hole to light up a node."""
        out = []
        for c in range(1, self.cols + 1):
            for top_row, rows in (("j", 5), ("e", 5)):
                h = Hole(f"{top_row}{c}", c, top_row)
                _, y = hole_xy(h)
                out.append(f'<rect class="nodehit" x="{_f(c * P - P / 2)}" '
                           f'y="{_f(y - P / 2)}" width="{_f(P)}" height="{_f(rows * P)}" '
                           f'data-node="{self._node(h)}"/>')
        for rail in RAILS:
            groups = [(1, self.cols)]
            if self.bb.split_rails:
                groups = [(1, self.cols // 2), (self.cols // 2 + 1, self.cols)]
            for a, b in groups:
                h = Hole(f"{rail}{a}", a, rail)
                _, y = hole_xy(h)
                out.append(f'<rect class="nodehit" x="{_f(a * P - P / 2)}" '
                           f'y="{_f(y - P / 2)}" width="{_f((b - a + 1) * P)}" '
                           f'height="{_f(P)}" data-node="{self._node(h)}"/>')
        return out

    # ── parts ────────────────────────────────────────────────────────────────

    def _pad(self, h: Hole, title: str, fill: str = "#DDDDDD", r: float = 3.2) -> str:
        x, y = hole_xy(h)
        return (f'<circle class="pad" cx="{_f(x)}" cy="{_f(y)}" r="{r}" fill="{fill}" '
                f'stroke="#555" stroke-width="0.8" data-node="{self._node(h)}">'
                f'<title>{_esc(title)}</title></circle>')

    def _rigid(self, part: Part) -> str:
        fp = part.fp
        assert fp is not None
        pts = {p.number: p for p in part.pins if p.number is not None}
        xs = [hole_xy(p.hole)[0] for p in part.pins]
        ys = [hole_xy(p.hole)[1] for p in part.pins]
        cx, cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
        # Long axis: from pin 1 towards the last pin of the first row.
        first_row_end = fp.count // 2 if fp.package.startswith(("DIP", "Pico")) \
            else fp.count
        ax, ay = hole_xy(pts[1].hole)
        bx, by = hole_xy(pts[max(first_row_end, 1)].hole)
        horizontal = abs(bx - ax) >= abs(by - ay)
        title = f"{part.cid} · {part.value or part.ctype} · {fp.package}, pin 1 in " \
                f"{pts[1].hole.name}"
        out = [f'<g class="bbpart" data-part="{_esc(part.cid)}"><title>{_esc(title)}</title>']

        if fp.package == "Pico":
            m_long, m_short = 0.9 * P, 0.75 * P
            fill, text_fill, pad_fill = "#2E7D4F", "#FFFFFF", "#E0B84C"
        elif fp.package.startswith("DIP"):
            m_long, m_short = 0.45 * P, 0.4 * P
            fill, text_fill, pad_fill = "#232323", "#FFFFFF", "#CCCCCC"
        elif part.ctype == "potentiometer":
            m_long, m_short = 0.6 * P, 0.9 * P
            fill, text_fill, pad_fill = "#2B5FA8", "#FFFFFF", "#CCCCCC"
        else:
            m_long, m_short = 0.45 * P, 0.45 * P
            fill, text_fill, pad_fill = "#232323", "#FFFFFF", "#CCCCCC"
        mx, my = (m_long, m_short) if horizontal else (m_short, m_long)
        x0, x1 = min(xs) - mx, max(xs) + mx
        y0, y1 = min(ys) - my, max(ys) + my
        out.append(f'<rect x="{_f(x0)}" y="{_f(y0)}" width="{_f(x1 - x0)}" '
                   f'height="{_f(y1 - y0)}" rx="4" fill="{fill}" '
                   f'stroke="#111" stroke-width="1"/>')

        # Pin-1 end: the notch of a DIP, the USB socket of a Pico.
        ux, uy = (1.0, 0.0) if horizontal else (0.0, 1.0)
        if (bx - ax) * ux + (by - ay) * uy < 0:
            ux, uy = -ux, -uy
        half = ((x1 - x0) if horizontal else (y1 - y0)) / 2
        ex, ey = cx - ux * half, cy - uy * half
        if fp.package == "Pico":
            w, h = (1.3 * P, 2.0 * P) if horizontal else (2.0 * P, 1.3 * P)
            ex -= ux * w / 2 if horizontal else 0
            ey -= uy * h / 2 if not horizontal else 0
            out.append(f'<rect x="{_f(ex - w / 2)}" y="{_f(ey - h / 2)}" width="{_f(w)}" '
                       f'height="{_f(h)}" rx="3" fill="#C4C4C4" stroke="#777"/>')
        elif fp.package.startswith("DIP"):
            out.append(f'<circle cx="{_f(ex)}" cy="{_f(ey)}" r="{_f(0.35 * P)}" '
                       f'fill="{BOARD_FILL}"/>')

        for num, p in sorted(pts.items()):
            out.append(self._pad(p.hole, p.label, pad_fill))
            if fp.package.startswith("DIP"):
                px_, py_ = hole_xy(p.hole)
                dx, dy = (0, 1) if horizontal else (1, 0)
                if (cx - px_) * dx + (cy - py_) * dy < 0:
                    dx, dy = -dx, -dy
                out.append(f'<text x="{_f(px_ + dx * 9)}" y="{_f(py_ + dy * 9 + 3)}" '
                           f'text-anchor="middle" font-family="sans-serif" font-size="7" '
                           f'fill="#BBBBBB">{num}</text>')
            elif fp.package == "Pico":
                name = fp.name_of(num)
                label = name or (fp.labels.get(num) if fp.labels.get(num) == "GND" else "")
                if label:
                    px_, py_ = hole_xy(p.hole)
                    dx, dy = (0, 1) if horizontal else (1, 0)
                    if (cx - px_) * dx + (cy - py_) * dy < 0:
                        dx, dy = -dx, -dy
                    out.append(f'<text x="{_f(px_ + dx * 13)}" y="{_f(py_ + dy * 13 + 3)}" '
                               f'text-anchor="middle" font-family="sans-serif" '
                               f'font-size="7" fill="#FFFFFF">{_esc(label)}</text>')

        main = part.value or part.cid
        size = 14 if fp.package == "Pico" else 11
        if fp.package.startswith("SIP") and part.ctype != "potentiometer":
            # A TO-92 body is too small for text; the ID sits next to it.
            out.append(self._outside_label(part.cid, (cx, y0 - 5) if horizontal
                                           else (x0 - 5, cy),
                                           "middle" if horizontal else "end"))
        else:
            out.append(f'<text x="{_f(cx)}" y="{_f(cy + size / 3)}" text-anchor="middle" '
                       f'font-family="sans-serif" font-size="{size}" font-weight="bold" '
                       f'fill="{text_fill}">{_esc(main)}</text>')
            if fp.package == "Pico":
                out.append(f'<text x="{_f(cx)}" y="{_f(cy + size / 3 + 15)}" '
                           f'text-anchor="middle" font-family="sans-serif" font-size="9" '
                           f'fill="#DDEEDD">{_esc(part.cid)} · pin 1 in '
                           f'{pts[1].hole.name}</text>')
            elif part.value:
                out.append(self._outside_label(part.cid, (cx, y0 - 5), "middle"))
        out.append("</g>")
        return "\n".join(out)

    def _outside_label(self, text: str, at: tuple[float, float], anchor: str) -> str:
        """Queue a part ID for the top layer; a halo keeps it readable where
        it overlaps holes, leads or a chip."""
        self._labels.append(
            f'<text x="{_f(at[0])}" y="{_f(at[1])}" text-anchor="{anchor}" '
            f'font-family="sans-serif" font-size="10" font-weight="bold" fill="#333" '
            f'stroke="{BOARD_FILL}" stroke-width="3" stroke-linejoin="round" '
            f'paint-order="stroke">{_esc(text)}</text>')
        return ""

    def _two_legs(self, part: Part) -> str:
        a, b = part.pins[0], part.pins[1]
        (ax, ay), (bx, by) = hole_xy(a.hole), hole_xy(b.hole)
        d = math.hypot(bx - ax, by - ay)
        ux, uy = (bx - ax) / d, (by - ay) / d
        mx, my = (ax + bx) / 2, (ay + by) / 2
        angle = math.degrees(math.atan2(by - ay, bx - ax))
        t = part.ctype
        round_body = t in ("capacitor", "led", "switch")
        L = 0.84 * P if t == "capacitor" else 0.9 * P if t == "led" \
            else 1.1 * P if t == "switch" \
            else min(_BODY_LEN.get(t, 1.4) * P, max(d - 0.6 * P, 0.45 * P))
        if round_body:
            L = min(L, max(d - 0.3 * P, 0.5 * P))

        # Which end is the marked one (cathode / minus)?
        names = [p.ref.split(".", 1)[1] if p.ref else "" for p in (a, b)]
        mark = 0
        for i, n in enumerate(names):
            if n in ("cathode", "-"):
                mark = -1 if i == 0 else 1

        body = []
        h = {"resistor": 0.55, "inductor": 0.8, "capacitor_pol": 0.9, "diode": 0.45,
             "zener": 0.45, "crystal": 0.6, "fuse": 0.5}.get(t, 0.6) * P
        if t == "capacitor":
            body.append(f'<circle cx="0" cy="0" r="{_f(L / 2)}" fill="#F0B429" '
                        f'stroke="#B7832F" stroke-width="1"/>')
        elif t == "led":
            color = next((c for words, c in _LED_COLORS
                          if any(w in part.value.lower() for w in words)), "#E03131")
            body.append(f'<circle cx="0" cy="0" r="{_f(L / 2)}" fill="{color}" '
                        f'fill-opacity="0.9" stroke="#555" stroke-width="1"/>')
            if mark:
                x = mark * L * 0.32
                body.append(f'<line x1="{_f(x)}" y1="{_f(-L / 2.4)}" x2="{_f(x)}" '
                            f'y2="{_f(L / 2.4)}" stroke="#333" stroke-width="1.5"/>')
        elif t == "switch":
            body.append(f'<rect x="{_f(-L / 2)}" y="{_f(-L / 2)}" width="{_f(L)}" '
                        f'height="{_f(L)}" rx="2" fill="#3A3A3A" stroke="#111"/>'
                        f'<circle cx="0" cy="0" r="{_f(L * 0.28)}" fill="#151515" '
                        f'stroke="#777"/>')
        else:
            fill, stroke = {"resistor": ("#E8D3A8", "#B89B68"),
                            "inductor": ("#2F4A3A", "#1B2D22"),
                            "capacitor_pol": ("#2A4D8F", "#1B3363"),
                            "diode": ("#262626", "#111"), "zener": ("#262626", "#111"),
                            "crystal": ("#C9CCD1", "#8E9399"),
                            "fuse": ("#E7EEF3", "#999")}.get(t, ("#8A8A8A", "#555"))
            body.append(f'<rect x="{_f(-L / 2)}" y="{_f(-h / 2)}" width="{_f(L)}" '
                        f'height="{_f(h)}" rx="{_f(min(h / 2, 5))}" fill="{fill}" '
                        f'stroke="{stroke}" stroke-width="1"/>')
            if mark and t in ("diode", "zener", "capacitor_pol"):
                band = "#DDDDDD"
                x = mark * (L / 2 - 0.22 * L)
                body.append(f'<rect x="{_f(x - L * 0.07)}" y="{_f(-h / 2)}" '
                            f'width="{_f(L * 0.14)}" height="{_f(h)}" fill="{band}"/>')

        title = f"{part.cid} · {part.value or t} · {a.label.split('.', 1)[-1]} in " \
                f"{a.hole.name}, {b.label.split('.', 1)[-1]} in {b.hole.name}"
        nx, ny = -uy, ux
        if ny > 0 or (ny == 0 and nx > 0):
            nx, ny = -nx, -ny
        off = max(h if not round_body else L, 0.6 * P) / 2 + 7
        anchor = "end" if nx < -0.7 else "middle"
        label = self._outside_label(part.cid, (mx + nx * off, my + ny * off + 3.5), anchor)
        return (f'<g class="bbpart" data-part="{_esc(part.cid)}"><title>{_esc(title)}</title>'
                f'<line x1="{_f(ax)}" y1="{_f(ay)}" x2="{_f(bx)}" y2="{_f(by)}" '
                f'stroke="#8A8A8A" stroke-width="1.8"/>'
                f'<g transform="translate({_f(mx)},{_f(my)}) rotate({angle:.1f})">'
                + "".join(body) + "</g>"
                + self._pad(a.hole, a.label, "#8A8A8A", 2.6)
                + self._pad(b.hole, b.label, "#8A8A8A", 2.6)
                + label + "</g>")

    def _cluster(self, part: Part) -> str:
        """A part whose leads go straight into the board but are not two:
        a small box just above the leads, with a line to every hole."""
        pts = [hole_xy(p.hole) for p in part.pins]
        cx = sum(x for x, _ in pts) / len(pts)
        cy = min(y for _, y in pts) - 1.4 * P
        w = max(40.0, len(part.cid) * 8 + 16)
        out = [f'<g class="bbpart" data-part="{_esc(part.cid)}">'
               f'<title>{_esc(part.cid)} · {_esc(part.value or part.ctype)}</title>']
        for (x, y), p in zip(pts, part.pins):
            out.append(f'<line x1="{_f(cx)}" y1="{_f(cy)}" x2="{_f(x)}" y2="{_f(y)}" '
                       f'stroke="#8A8A8A" stroke-width="1.8"/>')
            out.append(self._pad(p.hole, p.label, "#8A8A8A", 2.6))
        out.append(f'<rect x="{_f(cx - w / 2)}" y="{_f(cy - 10)}" width="{_f(w)}" '
                   f'height="20" rx="4" fill="#555" stroke="#222"/>'
                   f'<text x="{_f(cx)}" y="{_f(cy + 4)}" text-anchor="middle" '
                   f'font-family="sans-serif" font-size="10" font-weight="bold" '
                   f'fill="#fff">{_esc(part.cid)}</text></g>')
        return "".join(out)

    def _offboard(self, parts: list[Part]) -> list[str]:
        """Parts beside the board (antennas, speakers, batteries …), in a row
        below it, with their leads running to the holes."""
        def avg_x(p: Part) -> float:
            xs = [hole_xy(q.hole)[0] for q in p.pins]
            return sum(xs) / len(xs) if xs else 0.0

        out = []
        top = BOARD_BOTTOM * P + OFFBOARD_GAP * P
        right_edge = -math.inf
        for part in sorted(parts, key=avg_x):
            text = part.cid + (f" · {part.value}" if part.value else "")
            w = max(60.0, len(text) * 6.4 + 20)
            x = max(avg_x(part) if part.pins else 0.0, right_edge + w / 2 + 12)
            right_edge = x + w / 2
            out.append(f'<g class="bbpart" data-part="{_esc(part.cid)}">'
                       f'<title>{_esc(text)} (off the board)</title>')
            for k, p in enumerate(part.pins):
                hx, hy = hole_xy(p.hole)
                sx = x + (k - (len(part.pins) - 1) / 2) * 12
                mid = (hy + top) / 2
                out.append(f'<path d="M{_f(hx)},{_f(hy)} C{_f(hx)},{_f(mid)} '
                           f'{_f(sx)},{_f(mid)} {_f(sx)},{_f(top)}" fill="none" '
                           f'stroke="#555" stroke-width="2.2" stroke-dasharray="6 4"/>')
                out.append(self._pad(p.hole, p.label, "#555", 2.8))
            out.append(f'<rect x="{_f(x - w / 2)}" y="{_f(top)}" width="{_f(w)}" '
                       f'height="26" rx="5" fill="#FFFFFF" stroke="#555" '
                       f'stroke-width="1.4"/>'
                       f'<text x="{_f(x)}" y="{_f(top + 17)}" text-anchor="middle" '
                       f'font-family="sans-serif" font-size="11" font-weight="bold" '
                       f'fill="#333">{_esc(text)}</text></g>')
            self._offboard_bottom = max(self._offboard_bottom, top + 26)
        return out

    # ── wires ────────────────────────────────────────────────────────────────

    def _wires(self) -> list[str]:
        out = []
        for i, w in enumerate(self.bb.wires, 1):
            nets = self._nets_at(w.a)
            color = w.color or (self._net_color(nets[0]) if len(nets) == 1
                                else "#E67E22")
            (ax, ay), (bx, by) = hole_xy(w.a), hole_xy(w.b)
            if ax == bx or math.hypot(bx - ax, by - ay) <= 1.5 * P:
                d = f"M{_f(ax)},{_f(ay)} L{_f(bx)},{_f(by)}"
            else:
                # Long jumpers bow a little, like real ones, so they do not
                # vanish behind parts on the same row.
                dist = math.hypot(bx - ax, by - ay)
                nx, ny = -(by - ay) / dist, (bx - ax) / dist
                if ny > 0:
                    nx, ny = -nx, -ny
                bow = min(0.12 * dist, 3 * P)
                qx, qy = (ax + bx) / 2 + nx * bow, (ay + by) / 2 + ny * bow
                d = f"M{_f(ax)},{_f(ay)} Q{_f(qx)},{_f(qy)} {_f(bx)},{_f(by)}"
            node = self._node(w.a)
            title = f"Wire {i}: {w.a.name} → {w.b.name}" + \
                    (f" · net {', '.join(nets)}" if nets else "")
            out.append(f'<g class="bbwire" data-node="{node}"><title>{_esc(title)}</title>'
                       f'<path class="wire" d="{d}" fill="none" stroke="{_esc(color)}" '
                       f'stroke-width="3.4" stroke-linecap="round" data-node="{node}"/>'
                       f'<circle cx="{_f(ax)}" cy="{_f(ay)}" r="2.8" fill="{_esc(color)}" '
                       f'stroke="#222" stroke-width="0.8"/>'
                       f'<circle cx="{_f(bx)}" cy="{_f(by)}" r="2.8" fill="{_esc(color)}" '
                       f'stroke="#222" stroke-width="0.8"/></g>')
        return out

    # ── assembly ─────────────────────────────────────────────────────────────

    def _body(self, editor: bool) -> str:
        self._labels = []
        on_board, off_board = [], []
        for part in self.bb.parts.values():
            (off_board if part.offboard else on_board).append(part)
        parts = []
        # Big parts first, so leads and small parts end up on top of them.
        for part in sorted(on_board, key=lambda p: -len(p.pins)):
            if part.fp is not None and part.fp.kind == RIGID:
                parts.append(self._rigid(part))
            elif len(part.pins) == 2:
                parts.append(self._two_legs(part))
            elif part.pins:
                parts.append(self._cluster(part))
        out = self._board(editor)
        out.append('<g id="bb_parts">' + "\n".join(parts) + "</g>")
        out.append('<g id="bb_offboard">' + "\n".join(self._offboard(off_board)) + "</g>")
        out.append('<g id="bb_wires">' + "\n".join(self._wires()) + "</g>")
        out.append('<g id="bb_labels">' + "".join(self._labels) + "</g>")
        if editor:
            marks = []
            for k, f in enumerate(self.bb.findings):
                for name in f["holes"]:
                    h = next((p.hole for part in self.bb.parts.values()
                              for p in part.pins if p.hole.name == name), None)
                    if h is None:
                        continue
                    x, y = hole_xy(h)
                    marks.append(f'<circle class="bbmark {f["kind"]}" cx="{_f(x)}" '
                                 f'cy="{_f(y)}" r="7" data-finding="{k}"/>')
            out.append('<g id="bb_marks">' + "".join(marks) + "</g>")
        title = self.project.circuit.get("title", self.project.name)
        out.append(f'<text x="{_f(self.cols * P / 2)}" y="{_f(-6.3 * P)}" '
                   f'text-anchor="middle" font-family="sans-serif" font-size="16" '
                   f'font-weight="bold" fill="#1C2733">{_esc(title)} — breadboard</text>')
        return "\n".join(out)

    def nodes(self) -> dict[str, dict[str, Any]]:
        """What the editor shows when the pointer rests on a node."""
        info: dict[str, dict[str, Any]] = {}
        for c in range(1, self.cols + 1):
            for row in ("j", "e") + RAILS:
                h = Hole(f"{row}{c}", c, row)
                entry = info.setdefault(self.bb.root(h), {"where": [], "pins": [],
                                                          "nets": []})
                d = self.bb.describe_node(h)
                if d not in entry["where"]:
                    entry["where"].append(d)
        for part in self.bb.parts.values():
            for p in part.pins:
                info[self.bb.root(p.hole)]["pins"].append(f"{p.label} ({p.hole.name})")
        for root, entry in info.items():
            pins = [p for part in self.bb.parts.values() for p in part.pins
                    if self.bb.root(p.hole) == root]
            entry["nets"] = self.bb.nets_in(pins)
        return {k: v for k, v in info.items() if v["pins"] or len(v["where"]) > 1}

    def to_dict(self) -> dict[str, Any]:
        body = self._body(editor=True)
        return {
            "exists": self.bb.exists,
            "svg": body,
            "viewBox": list(self.bounds()),
            "findings": self.bb.findings,
            "nodes": self.nodes(),
            "columns": self.cols,
        }

    def to_svg(self) -> str:
        body = self._body(editor=False)
        x, y, w, h = self.bounds()
        return (f'<svg xmlns="http://www.w3.org/2000/svg" '
                f'viewBox="{x:.1f} {y:.1f} {w:.1f} {h:.1f}" '
                f'width="{w:.0f}" height="{h:.0f}">\n'
                f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" '
                f'fill="#FFFFFF"/>\n{body}\n</svg>\n')
