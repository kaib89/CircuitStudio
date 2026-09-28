"""Maps JSON component specs to symbol instances.

The circuit document is pure data (written by an LLM), so every component type
needs a declarative constructor path. Positions and rotation are NOT handled
here — they belong to the layout document.
"""
from __future__ import annotations
from typing import Any

from .symbols import (
    Component,
    Resistor, Capacitor, CapacitorPol, Inductor,
    Diode, LED, Zener, NPN, PNP, NMOS, PMOS,
    OpAmp, NE555, IC, SourceDC, SourceAC, Battery,
    Switch, Potentiometer, Crystal, Speaker, Fuse,
    Ground, VCC, VDD, Connector, Label,
    RPi, ESP32, ArduinoUno, ArduinoNano, Pico,
)

# Types constructed as Cls(comp_id, value)
_SIMPLE: dict[str, type[Component]] = {
    "resistor": Resistor,
    "capacitor": Capacitor,
    "capacitor_pol": CapacitorPol,
    "inductor": Inductor,
    "diode": Diode,
    "led": LED,
    "zener": Zener,
    "npn": NPN,
    "pnp": PNP,
    "nmos": NMOS,
    "pmos": PMOS,
    "opamp": OpAmp,
    "ne555": NE555,
    "source_dc": SourceDC,
    "source_ac": SourceAC,
    "battery": Battery,
    "switch": Switch,
    "potentiometer": Potentiometer,
    "crystal": Crystal,
    "speaker": Speaker,
    "fuse": Fuse,
    "ground": Ground,
    "vcc": VCC,
    "vdd": VDD,
    "label": Label,
}

# Types taking a pins={"left": [...], ...} spec
_BOARDS: dict[str, type[Component]] = {
    "rpi": RPi,
    "esp32": ESP32,
    "arduino_uno": ArduinoUno,
    "arduino_nano": ArduinoNano,
    "pico": Pico,
}

KNOWN_TYPES: list[str] = sorted(
    list(_SIMPLE) + list(_BOARDS) + ["ic", "connector"]
)

# Types whose pins are defined by the caller rather than fixed by the symbol.
CONFIGURABLE_TYPES: frozenset[str] = frozenset(list(_BOARDS) + ["ic", "connector"])


class UnknownComponentType(ValueError):
    pass


def build_component(spec: dict[str, Any]) -> Component:
    """Instantiate one component from its JSON spec.

    Required keys: `id`, `type`. Optional: `value`, plus type-specific keys
    (`pins` for ic/boards, `n`/`side` for connector).
    """
    ctype = str(spec.get("type", "")).lower().strip()
    cid = str(spec.get("id", "")).strip()
    if not cid:
        raise ValueError("Component spec is missing 'id'")
    value = spec.get("value", "")

    if ctype in _SIMPLE:
        cls = _SIMPLE[ctype]
        if value == "":
            # Let classes with meaningful defaults (NE555, VCC, VDD) keep them.
            return cls(cid)  # type: ignore[call-arg]
        return cls(cid, value)  # type: ignore[call-arg]

    if ctype == "ic":
        pins = spec.get("pins") or {}
        return IC(
            cid, value,
            left=list(pins.get("left", [])),
            right=list(pins.get("right", [])),
            top=list(pins.get("top", [])),
            bottom=list(pins.get("bottom", [])),
        )

    if ctype == "connector":
        return Connector(
            cid, value,
            n=int(spec.get("n", 2)),
            side=str(spec.get("side", "right")),
        )

    if ctype in _BOARDS:
        pins = spec.get("pins") or {}
        return _BOARDS[ctype](cid, value, pins=pins)  # type: ignore[call-arg]

    raise UnknownComponentType(
        f"Unknown component type '{ctype}' for '{cid}'. "
        f"Known types: {', '.join(KNOWN_TYPES)}"
    )
