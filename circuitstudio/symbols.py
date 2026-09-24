from __future__ import annotations
import re
from dataclasses import dataclass, field


@dataclass
class PinDef:
    name: str
    dx: float  # offset from component center, in grid units (1 unit = 50px)
    dy: float


@dataclass
class Component:
    comp_id: str
    comp_type: str
    value: str = ""
    rotation: int = 0  # 0, 90, 180, 270
    pins: list[PinDef] = field(default_factory=list)

    x: float = 0.0  # set by layout
    y: float = 0.0
    flip: bool = False  # mirrored horizontally (before rotation)

    # Local body bbox (xmin, ymin, xmax, ymax) — area where wires must NOT pass.
    # Defaults to a small box; component subclasses override to match their body.
    # Lead/pin areas are intentionally excluded so wires can reach pins.
    BODY: tuple[float, float, float, float] = (-22, -15, 22, 15)

    def pin(self, name: str) -> PinDef:
        for p in self.pins:
            if p.name == name:
                return p
        raise KeyError(f"Pin '{name}' not found on {self.comp_id}")

    def abs_pin_pos(self, name: str) -> tuple[float, float]:
        p = self.pin(name)
        dx = -p.dx if self.flip else p.dx
        dx, dy = _rotate(dx, p.dy, self.rotation)
        return (self.x + dx * 50, self.y + dy * 50)

    def _body_local(self) -> tuple[float, float, float, float]:
        """Override in subclasses whose body size depends on construction args."""
        return self.BODY

    def body_bbox(self) -> tuple[float, float, float, float]:
        """Axis-aligned body bbox in absolute coords, accounting for rotation."""
        return self._to_abs_bbox(self._body_local())

    def _extent_local(self) -> tuple[float, float, float, float]:
        """Everything the symbol draws (leads, labels), unrotated."""
        w, h = self.width, self.height
        return (-w / 2, -h / 2, w / 2, h / 2)

    def extent_bbox(self) -> tuple[float, float, float, float]:
        """Axis-aligned bbox of the whole drawn symbol, accounting for rotation
        and mirroring — `width`/`height` alone are only right at 0°/180°."""
        return self._to_abs_bbox(self._extent_local())

    def _to_abs_bbox(self, local: tuple[float, float, float, float]
                     ) -> tuple[float, float, float, float]:
        bx0, by0, bx1, by1 = local
        if self.flip:
            bx0, bx1 = -bx1, -bx0
        if self.rotation:
            corners = [(bx0, by0), (bx1, by0), (bx1, by1), (bx0, by1)]
            corners = [_rotate(x, y, self.rotation) for x, y in corners]
            xs = [c[0] for c in corners]
            ys = [c[1] for c in corners]
            bx0, by0, bx1, by1 = min(xs), min(ys), max(xs), max(ys)
        return (self.x + bx0, self.y + by0,
                self.x + bx1, self.y + by1)

    def svg_symbol(self) -> str:
        raise NotImplementedError

    @property
    def width(self) -> float:
        return 100.0

    @property
    def height(self) -> float:
        return 100.0


def _rotate(dx: float, dy: float, rot: int) -> tuple[float, float]:
    """Rotate a pin offset by rot degrees clockwise — matching SVG's rotate()."""
    for _ in range(rot // 90):
        dx, dy = -dy, dx
    return dx, dy


# ── Mirroring ────────────────────────────────────────────────────────────────

# Every piece of text in this module is produced by _label(), so its markup has
# exactly one shape — which is what makes the rewrite below safe.
_TEXT_RE = re.compile(
    r'<text x="(?P<x>-?[\d.]+)" y="(?P<y>-?[\d.]+)" text-anchor="(?P<anchor>[a-z]+)"'
    r'(?P<rest>[^>]*)>'
)
_ANCHOR_SWAP = {"start": "end", "end": "start", "middle": "middle"}


def mirror_symbol(svg: str) -> str:
    """Mirror a symbol horizontally without mirroring its text.

    The whole symbol is flipped with scale(-1,1); each <text> then gets an
    additional flip about its own anchor point, which cancels the outer one so
    the glyphs stay readable while their position is mirrored. Left/right text
    anchors are swapped as well, otherwise labels that used to sit inside a chip
    outline would end up outside it.
    """
    def fix(m: re.Match) -> str:
        x = float(m.group("x"))
        anchor = _ANCHOR_SWAP.get(m.group("anchor"), m.group("anchor"))
        rest = m.group("rest")
        existing = re.search(r'transform="([^"]*)"', rest)
        rest = re.sub(r'\s*transform="[^"]*"', "", rest)
        parts = [f"translate({x * 2:g},0)", "scale(-1,1)"]
        if existing:
            parts.append(existing.group(1))
        return (f'<text x="{m.group("x")}" y="{m.group("y")}" '
                f'text-anchor="{anchor}"{rest} transform="{" ".join(parts)}">')

    return f'<g transform="scale(-1,1)">\n{_TEXT_RE.sub(fix, svg)}\n</g>'


# ── SVG helpers ──────────────────────────────────────────────────────────────

def xml_escape(s) -> str:
    return (str(s).replace("&", "&amp;")
                  .replace("<", "&lt;")
                  .replace(">", "&gt;")
                  .replace('"', "&quot;"))


def _fmt(v: float) -> float | int:
    """Drop float noise such as 129.99999999999997 from computed coordinates."""
    v = round(v, 2)
    return int(v) if v == int(v) else v


def _line(x1, y1, x2, y2, stroke="#000000", sw=2) -> str:
    return f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{stroke}" stroke-width="{sw}" stroke-linecap="round"/>'


def _circle(cx, cy, r, fill="none", stroke="#000000", sw=2) -> str:
    return f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="{fill}" stroke="{stroke}" stroke-width="{sw}"/>'


def _rect(x, y, w, h, fill="none", stroke="#000000", sw=2) -> str:
    return f'<rect x="{x}" y="{y}" width="{w}" height="{h}" fill="{fill}" stroke="{stroke}" stroke-width="{sw}"/>'


def _path(d, fill="none", stroke="#000000", sw=2) -> str:
    return f'<path d="{d}" fill="{fill}" stroke="{stroke}" stroke-width="{sw}" stroke-linecap="round" stroke-linejoin="round"/>'


def _text_body(txt, x, size) -> str:
    """Inner text content; expand newlines into <tspan> elements."""
    txt = str(txt)
    if "\n" not in txt:
        return xml_escape(txt)
    lines = txt.split("\n")
    parts = []
    for i, line in enumerate(lines):
        dy = 0 if i == 0 else size + 1
        parts.append(f'<tspan x="{x}" dy="{dy}">{xml_escape(line)}</tspan>')
    return "".join(parts)


def _label(x, y, txt, rotation: int = 0, anchor: str = "middle",
           size: int = 10, color: str = "#333333") -> str:
    """Text that stays upright even when its parent group is rotated.

    Used for component IDs, values, and pin labels — anything that should remain
    readable regardless of component orientation."""
    if txt is None or txt == "":
        return ""
    body = _text_body(txt, x, size)
    # parent group is rotated by +rotation (CW); cancel with -rotation here
    counter = (-rotation) % 360
    rot_attr = f' transform="rotate({counter} {x} {y})"' if counter else ""
    return (f'<text x="{x}" y="{y}" text-anchor="{anchor}" font-family="sans-serif" '
            f'font-size="{size}" fill="{color}"{rot_attr}>{body}</text>')


def _corner_label(x, y, txt, rotation: int, size: int = 10,
                  color: str = "#333333") -> str:
    """Label hung off a corner of a chip outline, reading away from the body.

    Used when pins leave the top or bottom edge: a centred label would sit
    right on the middle pin's wire. The anchor is chosen from where the corner
    ends up on screen after rotation, so the text never runs back over the
    body. Mirroring swaps the anchor again (see mirror_symbol).
    """
    sx, _ = _rotate(x, y, rotation)
    anchor = "start" if sx >= 0 else "end"
    text = _label(_fmt(x), _fmt(y), txt, rotation=rotation, anchor=anchor,
                  size=size, color=color)
    # Out here a wire may still pass underneath; a white halo keeps the text
    # readable (symbols are drawn above the wires).
    return text.replace(
        ' font-family=',
        ' stroke="#FFFFFF" stroke-width="3" stroke-linejoin="round" '
        'paint-order="stroke" font-family=', 1)


# ── Two-terminal base ───────────────────────────────────────────────────────

class TwoTerminal(Component):
    """Horizontal by default: pin1 left, pin2 right, center at origin."""

    def __init__(self, comp_id: str, comp_type: str, value: str = "", rotation: int = 0):
        super().__init__(
            comp_id=comp_id,
            comp_type=comp_type,
            value=value,
            rotation=rotation,
            pins=[PinDef("1", -1, 0), PinDef("2", 1, 0)],
        )


# ── Resistor ────────────────────────────────────────────────────────────────

class Resistor(TwoTerminal):
    def __init__(self, comp_id: str, value: str = "", rotation: int = 0):
        super().__init__(comp_id, "resistor", value, rotation)

    def svg_symbol(self) -> str:
        r = self.rotation
        return "\n".join([
            _line(-50, 0, -20, 0),
            _path("M-20,0 L-15,-8 L-5,8 L5,-8 L15,8 L20,0"),
            _line(20, 0, 50, 0),
            _label(0, -16, self.comp_id, rotation=r),
            _label(0, 24, self.value, rotation=r),
        ])


# ── Capacitor ───────────────────────────────────────────────────────────────

class Capacitor(TwoTerminal):
    BODY = (-10, -20, 10, 20)

    def __init__(self, comp_id: str, value: str = "", rotation: int = 0):
        super().__init__(comp_id, "capacitor", value, rotation)

    def svg_symbol(self) -> str:
        r = self.rotation
        return "\n".join([
            _line(-50, 0, -6, 0),
            _line(-6, -18, -6, 18),
            _line(6, -18, 6, 18),
            _line(6, 0, 50, 0),
            _label(0, -24, self.comp_id, rotation=r),
            _label(0, 32, self.value, rotation=r),
        ])


# ── Polarised Capacitor ──────────────────────────────────────────────────────

class CapacitorPol(Component):
    BODY = (-10, -20, 20, 20)

    def __init__(self, comp_id: str, value: str = "", rotation: int = 0):
        super().__init__(
            comp_id=comp_id,
            comp_type="capacitor_pol",
            value=value,
            rotation=rotation,
            pins=[PinDef("+", -1, 0), PinDef("-", 1, 0)],
        )

    def svg_symbol(self) -> str:
        r = self.rotation
        return "\n".join([
            _line(-50, 0, -6, 0),
            _line(-6, -18, -6, 18),
            _path("M6,-18 Q18,0 6,18", fill="none"),
            _line(6, 0, 50, 0),
            _label(-18, -22, "+", rotation=r, size=11),
            _label(18, -22, "−", rotation=r, size=11),
            _label(0, -30, self.comp_id, rotation=r),
            _label(0, 34, self.value, rotation=r),
        ])


# ── Inductor ─────────────────────────────────────────────────────────────────

class Inductor(TwoTerminal):
    def __init__(self, comp_id: str, value: str = "", rotation: int = 0):
        super().__init__(comp_id, "inductor", value, rotation)

    def svg_symbol(self) -> str:
        r = self.rotation
        return "\n".join([
            _line(-50, 0, -20, 0),
            _path("M-20,0 Q-15,-14 -10,0 Q-5,-14 0,0 Q5,-14 10,0 Q15,-14 20,0"),
            _line(20, 0, 50, 0),
            _label(0, -18, self.comp_id, rotation=r),
            _label(0, 24, self.value, rotation=r),
        ])


# ── Diode ────────────────────────────────────────────────────────────────────

class Diode(Component):
    BODY = (-18, -16, 18, 16)

    def __init__(self, comp_id: str, value: str = "", rotation: int = 0,
                 comp_type: str = "diode"):
        super().__init__(
            comp_id=comp_id,
            comp_type=comp_type,
            value=value,
            rotation=rotation,
            pins=[PinDef("anode", -1, 0), PinDef("cathode", 1, 0)],
        )

    def svg_symbol(self) -> str:
        r = self.rotation
        return "\n".join([
            _line(-50, 0, -15, 0),
            _path("M-15,-14 L-15,14 L15,0 Z", fill="#000000"),
            _line(15, -14, 15, 14),
            _line(15, 0, 50, 0),
            _label(0, -20, self.comp_id, rotation=r),
            _label(0, 28, self.value, rotation=r),
        ])


class LED(Diode):
    def __init__(self, comp_id: str, value: str = "", rotation: int = 0):
        super().__init__(comp_id, value, rotation, comp_type="led")

    def svg_symbol(self) -> str:
        base = super().svg_symbol()
        arrows = (
            _path("M18,-10 L28,-20 M24,-20 L28,-20 L28,-16", sw=1.5) + "\n" +
            _path("M22,-4 L32,-14 M28,-14 L32,-14 L32,-10", sw=1.5)
        )
        return base + "\n" + arrows


class Zener(Diode):
    def __init__(self, comp_id: str, value: str = "", rotation: int = 0):
        super().__init__(comp_id, value, rotation, comp_type="zener")

    def svg_symbol(self) -> str:
        r = self.rotation
        return "\n".join([
            _line(-50, 0, -15, 0),
            _path("M-15,-14 L-15,14 L15,0 Z", fill="#000000"),
            _path("M15,-14 L20,-14 M15,14 L10,14 M15,-14 L15,14"),
            _line(15, 0, 50, 0),
            _label(0, -22, self.comp_id, rotation=r),
            _label(0, 28, self.value, rotation=r),
        ])


# ── BJT ──────────────────────────────────────────────────────────────────────

class NPN(Component):
    BODY = (-25, -25, 25, 25)

    def __init__(self, comp_id: str, value: str = "", rotation: int = 0):
        super().__init__(
            comp_id=comp_id, comp_type="npn", value=value, rotation=rotation,
            pins=[PinDef("base", -1, 0), PinDef("collector", 0, -1), PinDef("emitter", 0, 1)],
        )

    def svg_symbol(self) -> str:
        r = self.rotation
        return "\n".join([
            _circle(0, 0, 22),
            _line(-50, 0, -22, 0),
            _line(-22, -15, -22, 15),
            _line(-22, -10, 0, -30),
            _line(0, -30, 0, -50),
            _line(-22, 10, 0, 30),
            _line(0, 30, 0, 50),
            _path("M-5,25 L0,30 L5,22", fill="#000000", sw=1.5),
            _label(28, -4, self.comp_id, rotation=r, anchor="start"),
            _label(28, 10, self.value, rotation=r, anchor="start"),
        ])


class PNP(Component):
    BODY = (-25, -25, 25, 25)

    def __init__(self, comp_id: str, value: str = "", rotation: int = 0):
        super().__init__(
            comp_id=comp_id, comp_type="pnp", value=value, rotation=rotation,
            pins=[PinDef("base", -1, 0), PinDef("collector", 0, 1), PinDef("emitter", 0, -1)],
        )

    def svg_symbol(self) -> str:
        r = self.rotation
        return "\n".join([
            _circle(0, 0, 22),
            _line(-50, 0, -22, 0),
            _line(-22, -15, -22, 15),
            _line(-22, -10, 0, -30),
            _line(0, -30, 0, -50),
            _line(-22, 10, 0, 30),
            _line(0, 30, 0, 50),
            _path("M4,-26 L0,-30 L-4,-22", fill="#000000", sw=1.5),
            _label(28, -4, self.comp_id, rotation=r, anchor="start"),
            _label(28, 10, self.value, rotation=r, anchor="start"),
        ])


# ── MOSFET ───────────────────────────────────────────────────────────────────

class NMOS(Component):
    BODY = (-25, -25, 25, 25)

    def __init__(self, comp_id: str, value: str = "", rotation: int = 0):
        super().__init__(
            comp_id=comp_id, comp_type="nmos", value=value, rotation=rotation,
            pins=[PinDef("gate", -1, 0), PinDef("drain", 0, -1), PinDef("source", 0, 1)],
        )

    def svg_symbol(self) -> str:
        r = self.rotation
        return "\n".join([
            _circle(0, 0, 22),
            _line(-50, 0, -18, 0),
            _line(-18, -18, -18, 18),
            _line(-14, -18, -14, 18),
            _line(-14, -12, 0, -12),
            _line(-14, 0, 0, 0),
            _line(-14, 12, 0, 12),
            _line(0, -12, 0, -50),
            _line(0, 12, 0, 50),
            _line(0, 0, 0, -12),
            _line(0, 0, 0, 12),
            _path("M-14,0 L-8,0", fill="none", sw=1.5),
            _path("M-8,3 L-8,-3 L-4,0 Z", fill="#000000", sw=1),
            _label(28, -4, self.comp_id, rotation=r, anchor="start"),
            _label(28, 10, self.value, rotation=r, anchor="start"),
        ])


class PMOS(Component):
    BODY = (-25, -25, 25, 25)

    def __init__(self, comp_id: str, value: str = "", rotation: int = 0):
        super().__init__(
            comp_id=comp_id, comp_type="pmos", value=value, rotation=rotation,
            pins=[PinDef("gate", -1, 0), PinDef("drain", 0, 1), PinDef("source", 0, -1)],
        )

    def svg_symbol(self) -> str:
        r = self.rotation
        return "\n".join([
            _circle(0, 0, 22),
            _line(-50, 0, -18, 0),
            _line(-18, -18, -18, 18),
            _line(-14, -18, -14, 18),
            _line(-14, -12, 0, -12),
            _line(-14, 0, 0, 0),
            _line(-14, 12, 0, 12),
            _line(0, -12, 0, -50),
            _line(0, 12, 0, 50),
            _path("M-4,0 L-10,0", fill="none", sw=1.5),
            _path("M-4,3 L-4,-3 L-8,0 Z", fill="#000000", sw=1),
            _label(28, -4, self.comp_id, rotation=r, anchor="start"),
            _label(28, 10, self.value, rotation=r, anchor="start"),
        ])


# ── Op-Amp ───────────────────────────────────────────────────────────────────

class OpAmp(Component):
    BODY = (-42, -52, 42, 52)

    def __init__(self, comp_id: str, value: str = "", rotation: int = 0):
        super().__init__(
            comp_id=comp_id, comp_type="opamp", value=value, rotation=rotation,
            pins=[
                PinDef("in_p", -1.2, -0.5),
                PinDef("in_n", -1.2, 0.5),
                PinDef("out",   1.2, 0),
                PinDef("vcc",   0,  -1),
                PinDef("vee",   0,   1),
            ],
        )

    def svg_symbol(self) -> str:
        r = self.rotation
        return "\n".join([
            _path("M-40,-50 L-40,50 L40,0 Z"),
            _line(-60, -25, -40, -25),
            _line(-60, 25, -40, 25),
            _line(40, 0, 60, 0),
            _line(0, -50, 0, -25),
            _line(0, 50, 0, 25),
            _label(-30, -18, "+", rotation=r, size=12),
            _label(-30, 30, "−", rotation=r, size=12),
            _label(0, -56, self.comp_id, rotation=r),
            _label(0, 62, self.value, rotation=r),
        ])

    @property
    def width(self) -> float:
        return 120.0

    @property
    def height(self) -> float:
        return 130.0


# ── NE555 ────────────────────────────────────────────────────────────────────

class NE555(Component):
    BODY = (-50, -80, 50, 80)
    PIN_ORDER = ["GND", "TRG", "OUT", "RST", "CTL", "THR", "DIS", "VCC"]

    def __init__(self, comp_id: str, value: str = "NE555", rotation: int = 0):
        # offsets aligned with symbol's 40px pin-line spacing
        pins = [
            PinDef("GND", -1.5, 1.2),
            PinDef("TRG", -1.5, 0.4),
            PinDef("OUT", -1.5, -0.4),
            PinDef("RST", -1.5, -1.2),
            PinDef("CTL",  1.5, 1.2),
            PinDef("THR",  1.5, 0.4),
            PinDef("DIS",  1.5, -0.4),
            PinDef("VCC",  1.5, -1.2),
        ]
        super().__init__(comp_id=comp_id, comp_type="ne555", value=value,
                         rotation=rotation, pins=pins)

    def svg_symbol(self) -> str:
        r = self.rotation
        W, H = 100, 160
        x0, y0 = -W // 2, -H // 2
        parts = [_rect(x0, y0, W, H)]
        left_pins = ["GND", "TRG", "OUT", "RST"]
        right_pins = ["CTL", "THR", "DIS", "VCC"]
        for i, name in enumerate(left_pins):
            yp = y0 + H - 20 - i * 40
            parts.append(_line(x0 - 25, yp, x0, yp))
            parts.append(_label(x0 + 6, yp + 4, name, rotation=r, anchor="start", size=9))
        for i, name in enumerate(right_pins):
            yp = y0 + H - 20 - i * 40
            parts.append(_line(x0 + W, yp, x0 + W + 25, yp))
            parts.append(_label(x0 + W - 6, yp + 4, name, rotation=r, anchor="end", size=9))
        parts.append(_label(0, y0 - 12, self.comp_id, rotation=r, size=11))
        parts.append(_label(0, y0 + H + 18, self.value, rotation=r, size=10))
        return "\n".join(parts)

    @property
    def width(self) -> float:
        return 150.0

    @property
    def height(self) -> float:
        return 200.0


# ── Generic IC ────────────────────────────────────────────────────────────────

class IC(Component):
    LEAD = 30  # px from the body edge to the pin tip

    def __init__(self, comp_id: str, value: str = "",
                 left: list[str] | None = None,
                 right: list[str] | None = None,
                 top: list[str] | None = None,
                 bottom: list[str] | None = None,
                 rotation: int = 0):
        left = left or []
        right = right or []
        top = top or []
        bottom = bottom or []

        n_lr = max(len(left), len(right), 1)
        n_tb = max(len(top), len(bottom), 1)
        self._n_lr = n_lr
        self._n_tb = n_tb
        # Pin tips in px: body edge plus a fixed lead. With pins on the left
        # and right only (by far the common case) this is 70 px, the same as
        # the older formula, so existing layouts keep their alignment.
        W, H = self._ic_dims()
        self._tip_x = W / 2 + self.LEAD
        self._tip_y = H / 2 + self.LEAD
        ux, uy = self._tip_x / 50, self._tip_y / 50

        pins: list[PinDef] = []
        for i, name in enumerate(left):
            dy = (i - (len(left) - 1) / 2) * 0.8
            pins.append(PinDef(name, -ux, dy))
        for i, name in enumerate(right):
            dy = (i - (len(right) - 1) / 2) * 0.8
            pins.append(PinDef(name, ux, dy))
        for i, name in enumerate(top):
            dx = (i - (len(top) - 1) / 2) * 0.8
            pins.append(PinDef(name, dx, -uy))
        for i, name in enumerate(bottom):
            dx = (i - (len(bottom) - 1) / 2) * 0.8
            pins.append(PinDef(name, dx, uy))

        super().__init__(comp_id=comp_id, comp_type="ic", value=value,
                         rotation=rotation, pins=pins)
        self._left = left
        self._right = right
        self._top = top
        self._bottom = bottom

    def _ic_dims(self) -> tuple[int, int]:
        W = max(self._n_tb * 40 + 20, 80)
        # Tight on height: the pin span + 20px padding, but never less than
        # the minimum width, so a single top/bottom pin keeps a 70 px tip.
        H = max(self._n_lr * 40, 80)
        return W, H

    def _body_local(self) -> tuple[float, float, float, float]:
        W, H = self._ic_dims()
        return (-W // 2, -H // 2, W // 2, H // 2)

    def svg_symbol(self) -> str:
        r = self.rotation
        W, H = self._ic_dims()
        x0, y0 = -W // 2, -H // 2
        parts = [_rect(x0, y0, W, H)]
        # Pin lines drawn at the same positions used by the PinDef offsets
        # (centered, 40px spacing), so wires terminate exactly on the symbol.
        tx, ty = _fmt(self._tip_x), _fmt(self._tip_y)
        for i, name in enumerate(self._left):
            yp = (i - (len(self._left) - 1) / 2) * 40
            parts += [
                _line(-tx, yp, x0, yp),
                _label(x0 + 4, yp + 4, name, rotation=r, anchor="start", size=9),
            ]
        for i, name in enumerate(self._right):
            yp = (i - (len(self._right) - 1) / 2) * 40
            parts += [
                _line(x0 + W, yp, tx, yp),
                _label(x0 + W - 4, yp + 4, name, rotation=r, anchor="end", size=9),
            ]
        for i, name in enumerate(self._top):
            xp = (i - (len(self._top) - 1) / 2) * 40
            parts += [
                _line(xp, -ty, xp, y0),
                # label INSIDE box, just below top edge — same convention as
                # left/right labels (which are inside) so wires don't draw
                # across the text
                _label(xp, y0 + 12, name, rotation=r, size=9),
            ]
        for i, name in enumerate(self._bottom):
            xp = (i - (len(self._bottom) - 1) / 2) * 40
            parts += [
                _line(xp, y0 + H, xp, ty),
                _label(xp, y0 + H - 6, name, rotation=r, size=9),
            ]
        # comp_id above the box, value below it — moved out to the corners
        # when pins leave that edge, so no wire runs through the text
        if self._top:
            parts.append(_corner_label(x0 - 4, y0 - 6, self.comp_id, r, size=11))
        else:
            parts.append(_label(0, y0 - 12, self.comp_id, rotation=r, size=11))
        if self.value:
            if self._bottom:
                parts.append(_corner_label(x0 + W + 4, y0 + H + 14, self.value, r,
                                           color="#666666"))
            else:
                parts.append(_label(0, y0 + H + 18, self.value, rotation=r,
                                    size=10, color="#666666"))
        return "\n".join(parts)

    @property
    def width(self) -> float:
        return max(self._n_tb * 40 + 70, 130, 2 * self._tip_x + 10)

    @property
    def height(self) -> float:
        return max(self._n_lr * 40 + 90, 130, 2 * self._tip_y + 40)


# ── Sources ───────────────────────────────────────────────────────────────────

class SourceDC(Component):
    BODY = (-26, -26, 26, 26)

    def __init__(self, comp_id: str, value: str = "", rotation: int = 0):
        super().__init__(comp_id=comp_id, comp_type="source_dc", value=value,
                         rotation=rotation,
                         pins=[PinDef("+", 0, -1), PinDef("-", 0, 1)])

    def svg_symbol(self) -> str:
        r = self.rotation
        return "\n".join([
            _circle(0, 0, 24),
            _line(0, -50, 0, -24),
            _line(0, 24, 0, 50),
            _label(0, -2, "+", rotation=r, size=14),
            _label(0, 16, "−", rotation=r, size=14),
            _label(32, -8, self.comp_id, rotation=r, anchor="start"),
            _label(32, 8, self.value, rotation=r, anchor="start"),
        ])

    @property
    def width(self) -> float:
        return 100.0


class SourceAC(Component):
    BODY = (-26, -26, 26, 26)

    def __init__(self, comp_id: str, value: str = "", rotation: int = 0):
        super().__init__(comp_id=comp_id, comp_type="source_ac", value=value,
                         rotation=rotation,
                         pins=[PinDef("1", 0, -1), PinDef("2", 0, 1)])

    def svg_symbol(self) -> str:
        r = self.rotation
        return "\n".join([
            _circle(0, 0, 24),
            _line(0, -50, 0, -24),
            _line(0, 24, 0, 50),
            _path("M-10,0 Q-5,-10 0,0 Q5,10 10,0"),
            _label(32, -8, self.comp_id, rotation=r, anchor="start"),
            _label(32, 8, self.value, rotation=r, anchor="start"),
        ])

    @property
    def width(self) -> float:
        return 100.0


class Battery(Component):
    BODY = (-18, -12, 18, 10)

    def __init__(self, comp_id: str, value: str = "", rotation: int = 0):
        super().__init__(comp_id=comp_id, comp_type="battery", value=value,
                         rotation=rotation,
                         pins=[PinDef("+", 0, -1), PinDef("-", 0, 1)])

    def svg_symbol(self) -> str:
        r = self.rotation
        return "\n".join([
            _line(0, -50, 0, -10),
            _line(-16, -10, 16, -10),
            _line(-8, -4, 8, -4),
            _line(-16, 2, 16, 2),
            _line(-8, 8, 8, 8),
            _line(0, 8, 0, 50),
            _label(22, -6, self.comp_id, rotation=r, anchor="start"),
            _label(22, 8, self.value, rotation=r, anchor="start"),
        ])

    @property
    def width(self) -> float:
        return 100.0


# ── Switch ───────────────────────────────────────────────────────────────────

class Switch(TwoTerminal):
    def __init__(self, comp_id: str, value: str = "", rotation: int = 0):
        super().__init__(comp_id, "switch", value, rotation)

    def svg_symbol(self) -> str:
        r = self.rotation
        return "\n".join([
            _line(-50, 0, -20, 0),
            _circle(-20, 0, 3, fill="#000"),
            _circle(20, 0, 3, fill="#000"),
            _line(-20, 0, 18, -14),
            _line(20, 0, 50, 0),
            _label(0, -22, self.comp_id, rotation=r),
            _label(0, 24, self.value, rotation=r),
        ])


# ── Potentiometer ─────────────────────────────────────────────────────────────

class Potentiometer(Component):
    def __init__(self, comp_id: str, value: str = "", rotation: int = 0):
        super().__init__(comp_id=comp_id, comp_type="potentiometer", value=value,
                         rotation=rotation,
                         pins=[PinDef("1", -1, 0), PinDef("2", 1, 0), PinDef("wiper", 0, -1)])

    def svg_symbol(self) -> str:
        r = self.rotation
        return "\n".join([
            _line(-50, 0, -20, 0),
            _path("M-20,0 L-15,-8 L-5,8 L5,-8 L15,8 L20,0"),
            _line(20, 0, 50, 0),
            _line(0, -50, 0, -12),
            _path("M-5,-12 L0,0 L5,-12", fill="#000000"),
            _label(28, -8, self.comp_id, rotation=r, anchor="start"),
            _label(28, 8, self.value, rotation=r, anchor="start"),
        ])


# ── Crystal ───────────────────────────────────────────────────────────────────

class Crystal(TwoTerminal):
    def __init__(self, comp_id: str, value: str = "", rotation: int = 0):
        super().__init__(comp_id, "crystal", value, rotation)

    def svg_symbol(self) -> str:
        r = self.rotation
        return "\n".join([
            _line(-50, 0, -14, 0),
            _line(-14, -14, -14, 14),
            _rect(-10, -10, 20, 20),
            _line(14, -14, 14, 14),
            _line(14, 0, 50, 0),
            _label(0, -24, self.comp_id, rotation=r),
            _label(0, 28, self.value, rotation=r),
        ])


# ── Speaker ───────────────────────────────────────────────────────────────────

class Speaker(Component):
    def __init__(self, comp_id: str, value: str = "", rotation: int = 0):
        super().__init__(comp_id=comp_id, comp_type="speaker", value=value,
                         rotation=rotation,
                         pins=[PinDef("+", -1, 0), PinDef("-", 0, 0.8)])

    def svg_symbol(self) -> str:
        r = self.rotation
        return "\n".join([
            _line(-50, 0, -20, 0),
            _rect(-20, -12, 10, 24),
            _path("M-10,-12 L16,-28 L16,28 L-10,12 Z"),
            _line(-50, 40, -10, 40),
            _line(-10, 12, -10, 40),
            _label(24, -8, self.comp_id, rotation=r, anchor="start"),
            _label(24, 8, self.value, rotation=r, anchor="start"),
        ])


# ── Fuse ──────────────────────────────────────────────────────────────────────

class Fuse(TwoTerminal):
    def __init__(self, comp_id: str, value: str = "", rotation: int = 0):
        super().__init__(comp_id, "fuse", value, rotation)

    def svg_symbol(self) -> str:
        r = self.rotation
        return "\n".join([
            _line(-50, 0, -20, 0),
            _rect(-20, -10, 40, 20),
            _path("M-16,0 Q-8,-10 0,0 Q8,10 16,0", sw=1.5),
            _line(20, 0, 50, 0),
            _label(0, -18, self.comp_id, rotation=r),
            _label(0, 26, self.value, rotation=r),
        ])


# ── Power symbols ─────────────────────────────────────────────────────────────

class Ground(Component):
    """Power-rail terminator. comp_id is intentionally not shown — usually noise
    when many Grounds exist. Override by overriding svg_symbol if needed."""

    BODY = (-22, -2, 22, 18)

    def __init__(self, comp_id: str, value: str = "", rotation: int = 0):
        super().__init__(comp_id=comp_id, comp_type="ground", value=value,
                         rotation=rotation, pins=[PinDef("pin", 0, -0.5)])

    def svg_symbol(self) -> str:
        return "\n".join([
            _line(0, -25, 0, 0),
            _line(-20, 0, 20, 0),
            _line(-13, 7, 13, 7),
            _line(-6, 14, 6, 14),
        ])

    @property
    def width(self) -> float:
        return 50.0

    @property
    def height(self) -> float:
        return 50.0


class VCC(Component):
    BODY = (-14, -22, 14, 2)

    def __init__(self, comp_id: str, value: str = "VCC", rotation: int = 0):
        super().__init__(comp_id=comp_id, comp_type="vcc", value=value,
                         rotation=rotation, pins=[PinDef("pin", 0, 0.5)])

    def svg_symbol(self) -> str:
        r = self.rotation
        return "\n".join([
            _line(0, 25, 0, 0),
            _path("M-14,0 L0,-20 L14,0", fill="#000000"),
            _label(0, -26, self.value, rotation=r, color="#CC0000"),
        ])

    @property
    def width(self) -> float:
        return 60.0

    @property
    def height(self) -> float:
        return 60.0


class VDD(VCC):
    def __init__(self, comp_id: str, value: str = "VDD", rotation: int = 0):
        super().__init__(comp_id, value, rotation)
        self.comp_type = "vdd"


# ── Connector ────────────────────────────────────────────────────────────────

class Connector(Component):
    def __init__(self, comp_id: str, value: str = "", n: int = 2,
                 side: str = "right", rotation: int = 0):
        self._n = n
        self._side = side
        pins = []
        for i in range(n):
            dy = (i - (n - 1) / 2) * 0.8
            dx = 0.8 if side == "right" else -0.8
            pins.append(PinDef(str(i + 1), dx, dy))
        super().__init__(comp_id=comp_id, comp_type="connector", value=value,
                         rotation=rotation, pins=pins)

    def _body_local(self) -> tuple[float, float, float, float]:
        H = self._n * 40
        return (-15, -H // 2, 15, H // 2)

    def svg_symbol(self) -> str:
        r = self.rotation
        n = self._n
        H = n * 40
        W = 30
        x0, y0 = -W // 2, -H // 2
        parts = [_rect(x0, y0, W, H)]
        for i in range(n):
            yp = y0 + (i + 0.5) * 40
            xp = x0 + W if self._side == "right" else x0
            parts += [
                _circle(xp, yp, 4, fill="#666666", stroke="#666666"),
                _line(xp, yp, xp + (25 if self._side == "right" else -25), yp),
                _label(xp + (8 if self._side == "right" else -8), yp + 4,
                       str(i + 1), rotation=r,
                       anchor="start" if self._side == "right" else "end", size=9),
            ]
        parts.append(_label(0, y0 - 14, self.comp_id, rotation=r, size=11))
        if self.value:
            parts.append(_label(0, y0 + H + 16, self.value, rotation=r))
        return "\n".join(parts)

    @property
    def width(self) -> float:
        return 80.0

    @property
    def height(self) -> float:
        return self._n * 40 + 40.0


# ── Net Label ────────────────────────────────────────────────────────────────

class Label(Component):
    """Off-page connector / net label. Renders as a filled tag with text inside.
    The pin is at the tip (origin); the tag extends to the right by default."""

    def __init__(self, comp_id: str, value: str = "", rotation: int = 0):
        name = value or comp_id
        super().__init__(comp_id=comp_id, comp_type="label", value=name,
                         rotation=rotation, pins=[PinDef("pin", 0, 0)])

    def _body_local(self) -> tuple[float, float, float, float]:
        name = self.value or self.comp_id
        W = max(len(name) * 7 + 16, 44)
        # Tip is at (0,0); body extends right to W+8
        return (5, -12, W + 8, 12)

    def svg_symbol(self) -> str:
        r = self.rotation
        name = self.value or self.comp_id
        W = max(len(name) * 7 + 16, 44)
        return "\n".join([
            _path(f"M0,0 L8,-10 L{W + 8},-10 L{W + 8},10 L8,10 Z",
                  fill="#f7f7f0", stroke="#333333", sw=1.5),
            _label((W + 16) // 2, 4, name, rotation=r, color="#000000"),
        ])

    def _extent_local(self) -> tuple[float, float, float, float]:
        # The tag hangs off to the right of its tip, not around the centre.
        name = self.value or self.comp_id
        W = max(len(name) * 7 + 16, 44)
        return (-4, -14, W + 12, 14)

    @property
    def width(self) -> float:
        return max(len(self.value) * 7 + 32, 70)

    @property
    def height(self) -> float:
        return 28.0


# ── Board Symbols ─────────────────────────────────────────────────────────────

class _Board(Component):
    """Base for microcontroller board symbols."""

    BOARD_PINS: dict[str, tuple[str, str]] = {}
    BOARD_NAME: str = "Board"

    def __init__(self, comp_id: str, value: str = "",
                 pins: dict[str, list[str]] | None = None,
                 rotation: int = 0):
        self._pin_spec = pins or {}
        pin_defs: list[PinDef] = []

        left = pins.get("left", []) if pins else []
        right = pins.get("right", []) if pins else []
        top = pins.get("top", []) if pins else []
        bottom = pins.get("bottom", []) if pins else []

        n_lr = max(len(left), len(right), 1)
        n_tb = max(len(top), len(bottom), 1)
        self._left = left
        self._right = right
        self._top = top
        self._bottom = bottom

        # Body at least 100 px, pin tips a fixed 40 px lead further out.
        # That reproduces the tip positions of the older unit-based formula
        # exactly, so existing layouts are unaffected.
        self._W = max(n_tb * 40, 100)
        self._H = max(n_lr * 40, 100)
        self._tip_x = self._W / 2 + 40
        self._tip_y = self._H / 2 + 40
        ux, uy = self._tip_x / 50, self._tip_y / 50

        for i, name in enumerate(left):
            dy = (i - (len(left) - 1) / 2) * 0.8
            pin_defs.append(PinDef(name, -ux, dy))
        for i, name in enumerate(right):
            dy = (i - (len(right) - 1) / 2) * 0.8
            pin_defs.append(PinDef(name, ux, dy))
        for i, name in enumerate(top):
            dx = (i - (len(top) - 1) / 2) * 0.8
            pin_defs.append(PinDef(name, dx, -uy))
        for i, name in enumerate(bottom):
            dx = (i - (len(bottom) - 1) / 2) * 0.8
            pin_defs.append(PinDef(name, dx, uy))


        super().__init__(comp_id=comp_id, comp_type=self.BOARD_NAME.lower(),
                         value=value or self.BOARD_NAME,
                         rotation=rotation, pins=pin_defs)

    def _body_local(self) -> tuple[float, float, float, float]:
        return (-self._W // 2, -self._H // 2, self._W // 2, self._H // 2)

    def svg_symbol(self) -> str:
        r = self.rotation
        W, H = self._W, self._H
        x0, y0 = -W // 2, -H // 2
        parts = [_rect(x0, y0, W, H, fill="#f0f0f0")]

        tx, ty = _fmt(self._tip_x), _fmt(self._tip_y)

        def draw_side(names: list[str], side: str) -> None:
            n = len(names)
            for i, name in enumerate(names):
                if side == "left":
                    yp = (i - (n - 1) / 2) * 40
                    parts.append(_line(-tx, yp, x0, yp))
                    parts.append(_label(x0 + 4, yp + 4, name, rotation=r, anchor="start", size=8))
                elif side == "right":
                    yp = (i - (n - 1) / 2) * 40
                    parts.append(_line(x0 + W, yp, tx, yp))
                    parts.append(_label(x0 + W - 4, yp + 4, name, rotation=r, anchor="end", size=8))
                elif side == "top":
                    xp = (i - (n - 1) / 2) * 40
                    parts.append(_line(xp, -ty, xp, y0))
                    parts.append(_label(xp, y0 + 12, name, rotation=r, size=8))
                elif side == "bottom":
                    xp = (i - (n - 1) / 2) * 40
                    parts.append(_line(xp, y0 + H, xp, ty))
                    parts.append(_label(xp, y0 + H - 6, name, rotation=r, size=8))

        draw_side(self._left, "left")
        draw_side(self._right, "right")
        draw_side(self._top, "top")
        draw_side(self._bottom, "bottom")

        if self._top:
            parts.append(_corner_label(x0 - 4, y0 - 6, self.comp_id, r, size=11))
        else:
            parts.append(_label(0, y0 - 12, self.comp_id, rotation=r, size=11))
        if self._bottom:
            parts.append(_corner_label(x0 + W + 4, y0 + H + 14, self.value, r,
                                       color="#666666"))
        else:
            parts.append(_label(0, y0 + H + 18, self.value, rotation=r,
                                size=10, color="#666666"))
        return "\n".join(parts)

    @property
    def width(self) -> float:
        return max(self._W + 70.0, 2 * self._tip_x + 10)

    @property
    def height(self) -> float:
        return max(self._H + 90.0, 2 * self._tip_y + 40)


class RPi(_Board):
    BOARD_NAME = "RPi"


class ESP32(_Board):
    BOARD_NAME = "ESP32"


class ArduinoUno(_Board):
    BOARD_NAME = "Arduino Uno"


class ArduinoNano(_Board):
    BOARD_NAME = "Arduino Nano"


class Pico(_Board):
    BOARD_NAME = "Pico"
