"""Physical packages: which pin of a real part ends up in which breadboard hole.

A schematic symbol knows pin *names* only. To put a part on a breadboard we
also need its physical pin *numbers* and how those pins sit relative to each
other. The numbers are an electrical fact of the part — a 74HCU04 has GND on
pin 7 no matter how anyone arranges it — so they live in circuit.json, next to
the part, and are written by the assistant:

    {"id": "U1", "type": "ic", "value": "74HCU04",
     "package": "DIP-14", "pinout": {"1A": 1, "1Y": 2, ..., "VCC": 14}}

Two kinds of footprint exist:

- rigid: DIP/SIP packages and the Pico. Their pins keep a fixed spacing, so
  the breadboard plan gives one anchor hole (pin 1) and a rotation.
- legs:  resistors, capacitors, LEDs, … whose leads can be bent to any
  spacing. The plan names a hole for every lead.

Offsets are in breadboard pitches (0.1"), x to the right, y downwards, with
pin 1 at the origin and the part unrotated.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .library import expand
from .symbols import Component
from .ties import TIE_TYPES

RIGID = "rigid"
LEGS = "legs"

# Row distance of a DIP package (0.3") and of the Pico's two headers (0.7").
DIP_ROW_GAP = 3
PICO_ROW_GAP = 7

# DIP-14 is 0.3" wide; boards are wider DIPs: DIP-30-600 (0.6"), DIP-38-900.
_PACKAGE = re.compile(r"^(DIP|SIP)-?(\d+)(?:-(\d{3,4}))?$", re.IGNORECASE)
_PACKAGE_ALIASES = {"TO-92": "SIP-3", "TO92": "SIP-3",
                    "TO-220": "SIP-3", "TO220": "SIP-3"}

# Types that can carry a DIP/SIP package, with the default the part gets when
# the circuit names none. None means: the assistant has to say.
_RIGID_TYPES: dict[str, tuple[str | None, dict[str, int] | None]] = {
    "ic": (None, None),
    # Standard NE555 pinout.
    "ne555": ("DIP-8", {"GND": 1, "TRG": 2, "OUT": 3, "RST": 4,
                        "CTL": 5, "THR": 6, "DIS": 7, "VCC": 8}),
    # The industry-standard single op-amp pinout (741, TL071, NE5534, …).
    "opamp": ("DIP-8", {"in_n": 2, "in_p": 3, "vee": 4, "out": 6, "vcc": 7}),
    # Transistors: EBC, CBE, GDS, … differ from part to part. A guessed
    # default would put the part in backwards without anyone noticing.
    "npn": ("SIP-3", None),
    "pnp": ("SIP-3", None),
    "nmos": ("SIP-3", None),
    "pmos": ("SIP-3", None),
    # Inline pots and trimmers have the wiper in the middle.
    "potentiometer": ("SIP-3", {"1": 1, "wiper": 2, "2": 3}),
}

# Board symbols with caller-defined pins. With a package (usually from the
# library) they sit on the breadboard like a wide chip; without one they
# stay beside it and are wired lead by lead.
BOARD_TYPES = frozenset({"esp32", "arduino_nano", "arduino_uno", "rpi", "board"})

# ── Raspberry Pi Pico ─────────────────────────────────────────────────────────
# Physical header pins of the Pico / Pico W / Pico 2 / Pico 2 W (identical on
# all four). Pins 30 and 33-40 and the shared layout of the wireless variants
# were checked against raspberrypi.com/documentation/microcontrollers/
# pico-series.html; the GP pins follow the official pinout diagram.
PICO_PINS: dict[int, str] = {
    1: "GP0", 2: "GP1", 3: "GND", 4: "GP2", 5: "GP3", 6: "GP4", 7: "GP5",
    8: "GND", 9: "GP6", 10: "GP7", 11: "GP8", 12: "GP9", 13: "GND",
    14: "GP10", 15: "GP11", 16: "GP12", 17: "GP13", 18: "GND", 19: "GP14",
    20: "GP15", 21: "GP16", 22: "GP17", 23: "GND", 24: "GP18", 25: "GP19",
    26: "GP20", 27: "GP21", 28: "GND", 29: "GP22", 30: "RUN", 31: "GP26",
    32: "GP27", 33: "AGND", 34: "GP28", 35: "ADC_VREF", 36: "3V3(OUT)",
    37: "3V3_EN", 38: "GND", 39: "VSYS", 40: "VBUS",
}
# All GND pins are one copper plane. AGND is deliberately left out: the
# documentation calls it the analogue ground reference, and whether it is
# joined to GND on the board was not verified.
PICO_GND = (38, 3, 8, 13, 18, 23, 28)

_PICO_ALIASES: dict[str, int] = {
    "3V3": 36, "3V3OUT": 36, "33V": 36, "3V3EN": 37, "VSYS": 39, "VBUS": 40,
    "RUN": 30, "ADCVREF": 35, "AGND": 33, "ADC0": 31, "ADC1": 32, "ADC2": 34,
}
for _num, _name in PICO_PINS.items():
    if _name.startswith("GP"):
        _PICO_ALIASES[_name] = _num
        _PICO_ALIASES["GPIO" + _name[2:]] = _num

_GND_NAME = re.compile(r"^GND\d*$")


def _norm(name: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", name.upper())


@dataclass
class Footprint:
    kind: str                                   # RIGID or LEGS
    package: str = ""                           # "DIP-14", "SIP-3", "Pico"
    count: int = 0                              # physical pins (rigid only)
    numbers: dict[str, int] = field(default_factory=dict)   # name -> pin no.
    offsets: dict[int, tuple[int, int]] = field(default_factory=dict)
    ties: list[frozenset[int]] = field(default_factory=list)  # joined on the part
    board: bool = False                                       # drawn as a board
    labels: dict[int, str] = field(default_factory=dict)      # printed pin names

    def name_of(self, number: int) -> str | None:
        """Schematic pin name on a physical pin, if the circuit uses it."""
        for name, n in self.numbers.items():
            if n == number:
                return name
        return None

    def describe(self, number: int) -> str:
        label = self.labels.get(number)
        name = self.name_of(number)
        text = name or label
        return f"pin {number} ({text})" if text else f"pin {number}"


def parse_package(text: str) -> tuple[str, int, int] | None:
    """'DIP-14' -> ('DIP', 14, 3); 'DIP-30-600' -> ('DIP', 30, 6), the last
    number being the row distance in pitches; 'TO-92' -> ('SIP', 3, 0)."""
    text = _PACKAGE_ALIASES.get(text.strip().upper(), text.strip())
    m = _PACKAGE.match(text)
    if not m:
        return None
    kind, n, mil = m.group(1).upper(), int(m.group(2)), m.group(3)
    if n < 1 or n > 64 or (kind == "DIP" and (n < 4 or n % 2)):
        return None
    if kind == "SIP":
        return None if mil else (kind, n, 0)
    if mil is None:
        return kind, n, DIP_ROW_GAP
    mil = int(mil)
    if mil % 100 or not 300 <= mil <= 1000:
        return None
    return kind, n, mil // 100


def package_name(kind: str, n: int, gap: int) -> str:
    if kind == "SIP":
        return f"SIP-{n}"
    return f"DIP-{n}" if gap == DIP_ROW_GAP else f"DIP-{n}-{gap * 100}"


def _offsets(kind: str, n: int, gap: int = DIP_ROW_GAP) -> dict[int, tuple[int, int]]:
    if kind == "SIP":
        return {i: (i - 1, 0) for i in range(1, n + 1)}
    # DIP, seen from above with the notch (a board: its USB end) on the left:
    # pin 1 bottom left, counting to the right along the bottom row and back
    # along the top.
    half = n // 2
    out = {i: (i - 1, 0) for i in range(1, half + 1)}
    out.update({half + j: (half - j, -gap) for j in range(1, half + 1)})
    return out


def is_physical(ctype: str) -> bool:
    return ctype not in TIE_TYPES


def is_rigid_type(ctype: str) -> bool:
    return ctype in _RIGID_TYPES or ctype == "pico"


def takes_package(ctype: str) -> bool:
    return is_rigid_type(ctype) or ctype in BOARD_TYPES


def check_spec(spec: dict[str, Any], comp: Component) -> list[str]:
    """Validate the optional 'package'/'pinout' of one circuit part.

    Only what is written is checked. A missing pinout is not an error here —
    it only matters once the part is put on a breadboard.
    """
    cid = comp.comp_id
    from_library = bool(spec.get("part"))    # supplies whatever is left out
    ctype = str(expand(spec).get("type", "")).lower()
    has_pkg = "package" in spec
    has_map = "pinout" in spec
    if not has_pkg and not has_map:
        return []
    if not is_physical(ctype):
        return [f"Part '{cid}': '{ctype}' is a net symbol, it has no package or pinout"]
    if not takes_package(ctype):
        return [f"Part '{cid}': a {ctype} is placed lead by lead on the breadboard, "
                f"so 'package' and 'pinout' do not apply"]
    if ctype == "pico" and has_pkg:
        return [f"Part '{cid}': the Pico's package is built in — leave 'package' off"]
    _, errors = footprint(spec, comp)
    if (ctype == "ic" or ctype in BOARD_TYPES) and has_pkg != has_map \
            and not from_library:
        errors.insert(0, f"Part '{cid}': 'package' and 'pinout' go together — "
                         f"give both")
    return errors


def footprint(spec: dict[str, Any], comp: Component) -> tuple[Footprint | None, list[str]]:
    """The footprint of one part, or None plus the reason it has none."""
    spec = expand(spec)
    cid = comp.comp_id
    ctype = str(spec.get("type", "")).lower()
    names = [p.name for p in comp.pins]

    if not is_physical(ctype):
        return None, [f"'{cid}' is a net symbol ({ctype}), not a part — it does not "
                      f"go on the breadboard"]
    if ctype == "pico":
        return _pico(spec, comp)
    if ctype in BOARD_TYPES:
        if not spec.get("package"):
            return Footprint(kind=LEGS), []
        default_pkg, default_map = None, None
    elif not is_rigid_type(ctype):
        return Footprint(kind=LEGS), []
    else:
        default_pkg, default_map = _RIGID_TYPES[ctype]
    pkg_text = str(spec.get("package") or default_pkg or "")
    if not pkg_text:
        return None, [f"'{cid}' needs 'package' (e.g. \"DIP-14\") and 'pinout' "
                      f"(pin name -> pin number) in the circuit before it can go "
                      f"on the breadboard"]
    parsed = parse_package(pkg_text)
    if parsed is None:
        return None, [f"Part '{cid}': unknown package '{pkg_text}'. Use DIP-<n> "
                      f"(even n; a board: DIP-<n>-<row distance in mil>, e.g. "
                      f"DIP-30-600), SIP-<n>, or TO-92/TO-220 for three pins in a row"]
    kind, count, gap = parsed

    raw = spec.get("pinout")
    if raw is None:
        if default_map is None:
            what = ("from the datasheet — EBC, CBE, … differ between parts"
                    if ctype in ("npn", "pnp", "nmos", "pmos")
                    else "(pin name -> pin number)")
            return None, [f"'{cid}' needs a 'pinout' {what} before it can go on "
                          f"the breadboard"]
        raw = default_map
    numbers, errors = _read_pinout(cid, raw, names, count)
    ties, tie_errors = _read_ties(cid, spec.get("ties"), numbers)
    if errors or tie_errors:
        return None, errors + tie_errors
    board = ctype in BOARD_TYPES
    return Footprint(kind=RIGID, package=package_name(kind, count, gap), count=count,
                     numbers=numbers, offsets=_offsets(kind, count, gap), ties=ties,
                     board=board,
                     labels={n: name for name, n in numbers.items()} if board else {}), []


def _read_ties(cid: str, raw: Any, numbers: dict[str, int]) -> tuple[list[frozenset[int]], list[str]]:
    """Pins joined on the part itself, e.g. a board's GND pins."""
    if not raw:
        return [], []
    if not isinstance(raw, list) or not all(isinstance(g, list) for g in raw):
        return [], [f"Part '{cid}': 'ties' must be a list of pin-name lists, e.g. "
                    f"[[\"GND\", \"GND_2\"]]"]
    out, errors = [], []
    for group in raw:
        missing = [str(n) for n in group if str(n) not in numbers]
        if missing:
            errors.append(f"Part '{cid}': 'ties' names unknown pin(s) {', '.join(missing)}")
            continue
        if len(group) > 1:
            out.append(frozenset(numbers[str(n)] for n in group))
    return out, errors


def _read_pinout(cid: str, raw: Any, names: list[str],
                 count: int) -> tuple[dict[str, int], list[str]]:
    if not isinstance(raw, dict):
        return {}, [f"Part '{cid}': 'pinout' must be an object "
                    f"{{\"pin name\": pin number}}"]
    errors: list[str] = []
    numbers: dict[str, int] = {}
    used: dict[int, str] = {}
    for name, num in raw.items():
        name = str(name)
        if name not in names:
            errors.append(f"Part '{cid}': pinout names pin '{name}', which the part "
                          f"does not have. Available: {', '.join(names)}")
            continue
        if isinstance(num, bool) or not isinstance(num, int) or not 1 <= num <= count:
            errors.append(f"Part '{cid}': pin '{name}' -> {num!r} is not a pin "
                          f"number between 1 and {count}")
            continue
        if num in used:
            errors.append(f"Part '{cid}': pins '{used[num]}' and '{name}' are both "
                          f"given pin number {num}")
            continue
        used[num] = name
        numbers[name] = num
    missing = [n for n in names if n not in raw]
    if missing and not errors:
        errors.append(f"Part '{cid}': pinout has no number for {', '.join(missing)}")
    return numbers, errors


def _pico(spec: dict[str, Any], comp: Component) -> tuple[Footprint | None, list[str]]:
    cid = comp.comp_id
    names = [p.name for p in comp.pins]
    explicit = spec.get("pinout") or {}
    if not isinstance(explicit, dict):
        return None, [f"Part '{cid}': 'pinout' must be an object "
                      f"{{\"pin name\": pin number}}"]
    numbers: dict[str, int] = {}
    errors: list[str] = []
    spare_gnd = list(PICO_GND)
    for name in names:
        num = explicit.get(name)
        if num is None:
            key = _norm(name)
            if _GND_NAME.match(key):
                # Several GND pins in the schematic get distinct header pins;
                # they are joined on the board anyway.
                num = spare_gnd.pop(0) if spare_gnd else PICO_GND[0]
            else:
                num = _PICO_ALIASES.get(key)
        if isinstance(num, bool) or not isinstance(num, int) or not 1 <= num <= 40:
            errors.append(f"Part '{cid}': pin '{name}' is not a Pico header pin. "
                          f"Rename it (GP0…GP28, GND, 3V3, VSYS, VBUS, RUN, "
                          f"3V3_EN, ADC_VREF, AGND) or map it with "
                          f"\"pinout\": {{\"{name}\": <1-40>}}")
            continue
        numbers[name] = num
    if errors:
        return None, errors
    taken: dict[int, str] = {}
    for name, num in numbers.items():
        if num in taken:
            errors.append(f"Part '{cid}': '{taken[num]}' and '{name}' both sit on "
                          f"Pico pin {num} ({PICO_PINS[num]})")
        taken[num] = name
    if errors:
        return None, errors
    offsets = {i: (i - 1, 0) for i in range(1, 21)}
    offsets.update({i: (40 - i, -PICO_ROW_GAP) for i in range(21, 41)})
    return Footprint(kind=RIGID, package="Pico", count=40, numbers=numbers,
                     offsets=offsets, ties=[frozenset(PICO_GND)],
                     labels=dict(PICO_PINS), board=True), []


def summary() -> list[str]:
    """Human-readable overview for list_component_types."""
    lines = [
        "Breadboard packages (optional in the circuit; needed once a part goes "
        "on a breadboard plan):",
        "- ic: \"package\": \"DIP-<n>\" or \"SIP-<n>\" plus \"pinout\": "
        "{pin name: physical pin number} covering every pin — from the datasheet.",
        "- ne555: DIP-8 built in (GND 1, TRG 2, OUT 3, RST 4, CTL 5, THR 6, "
        "DIS 7, VCC 8).",
        "- opamp: DIP-8 with the standard single op-amp pinout built in "
        "(in_n 2, in_p 3, vee 4, out 6, vcc 7); override with \"pinout\" if "
        "the part differs.",
        "- npn, pnp, nmos, pmos: TO-92 (three in a row); \"pinout\" is required, "
        "e.g. {\"emitter\": 1, \"base\": 2, \"collector\": 3} — take it from the "
        "datasheet, EBC/CBE differ.",
        "- potentiometer: three in a row, wiper in the middle (1, wiper, 2).",
        "- pico: the 40 header pins are built in; name the schematic pins "
        "GP0…GP28, GND, 3V3, VSYS, VBUS, RUN, 3V3_EN, ADC_VREF, AGND. All GND "
        "pins are joined on the board.",
        "- esp32, arduino_nano, arduino_uno, board: with a wide package "
        "\"DIP-<pins>-<row distance in mil>\" (e.g. DIP-30-600) they sit on the "
        "breadboard across the channel — easiest from the library "
        "(Arduino-Nano, D1-mini, ESP32-DevKitC-V4); without one they stay "
        "beside it and only their leads go into holes. \"ties\": [[\"GND\", "
        "\"GND_2\"]] marks pins that are one net on the board itself.",
        "- everything else (resistor, capacitor, led, diode, …) is placed lead "
        "by lead and needs no package.",
    ]
    return lines
