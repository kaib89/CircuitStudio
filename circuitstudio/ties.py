"""Net ties: GND/VCC/label symbols that *are* a connection rather than a part.

Two GND symbols in the same net are connected by definition, so no wire is
drawn between them. Instead each ordinary pin of the net is wired to the
nearest such symbol — the same convention as KiCad or Eagle. Both the drawing
(scene.py) and the placement (document.py) use this one rule, so a symbol is
always placed where it will actually be wired.
"""
from __future__ import annotations

from typing import Callable

TIE_TYPES = frozenset({"ground", "vcc", "vdd", "label"})

Point = tuple[float, float]


def assign(refs: list[str], pts: list[Point], type_of: Callable[[str], str],
           overrides: dict[str, str] | None = None) -> dict[int, list[int]] | None:
    """Group one net's pins by the symbol they are wired to.

    Returns {symbol index: [symbol index, pin index, ...]}, or None when the
    net has fewer than two symbols and is simply wired as one tree. A pin goes
    to the symbol named in `overrides` (a human's choice) if that symbol is on
    this net, else to the nearest one by Manhattan distance.
    """
    ties = [k for k, ref in enumerate(refs) if type_of(ref) in TIE_TYPES]
    if len(ties) < 2:
        return None
    tie_by_comp = {refs[k].split(".", 1)[0]: k for k in ties}
    overrides = overrides or {}
    groups: dict[int, list[int]] = {t: [t] for t in ties}
    for k, ref in enumerate(refs):
        if k in groups:
            continue
        target = tie_by_comp.get(str(overrides.get(ref, "")))
        if target is None:
            target = min(ties, key=lambda t: distance(pts[t], pts[k]))
        groups[target].append(k)
    return groups


def distance(a: Point, b: Point) -> float:
    return abs(a[0] - b[0]) + abs(a[1] - b[1])
