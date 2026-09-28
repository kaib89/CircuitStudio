"""Putting functional groups next to each other like building blocks.

The circuit document may tag parts with a `group` ("Pitch oscillator",
"Power supply"...). Each group is first laid out on its own (see
Project.autoplace); this module then decides where the finished blocks go.

Blocks are placed one at a time, the best-connected first. Every next block is
tried on each side of every block already down, at a few alignments — flush,
centred, and "this pin exactly level with its partner" — and the spot whose
wires to the placed blocks are shortest and straightest wins.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import NamedTuple

Box = tuple[float, float, float, float]


class Pin(NamedTuple):
    net: str
    x: float
    y: float
    dx: int     # the way the pin points out of its part (unit vector)
    dy: int

GAP = 100.0          # free space between blocks: room for wires and symbols
INNER_GAP = 40.0     # free space between the parts inside one block
BEND = 30.0          # a wire with a corner costs as much as 30 px of straight wire
BEHIND = 200.0       # partner lies behind the pin: the wire has to go round the part
RAIL_WEIGHT = 0.15   # power rails barely count: symbols connect them anyway
SPREAD_WEIGHT = 0.5  # per px the sheet's outline grows


@dataclass
class Block:
    box: Box            # extent of everything drawn, relative to the block origin
    pins: list[Pin]     # relative to the block origin


def _moved(box: Box, ox: float, oy: float) -> Box:
    return (box[0] + ox, box[1] + oy, box[2] + ox, box[3] + oy)


def _clear(box: Box, others: list[Box], gap: float) -> bool:
    x0, y0, x1, y1 = box[0] - gap + 1, box[1] - gap + 1, box[2] + gap - 1, box[3] + gap - 1
    return not any(x0 < o[2] and o[0] < x1 and y0 < o[3] and o[1] < y1 for o in others)


def _union(boxes: list[Box]) -> Box | None:
    if not boxes:
        return None
    return (min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes))


def arrange(blocks: list[Block], fixed_boxes: list[Box], fixed_pins: list[Pin],
            rails: set[str], gap: float = GAP) -> list[tuple[float, float]]:
    """Offset for every block. `fixed_*` describe parts that already sit
    somewhere (locked or placed earlier) — they are obstacles and partners.

    The same routine lays out the parts inside a block: each part is then a
    block of its own, with `gap` = INNER_GAP."""
    placed_boxes = list(fixed_boxes)
    placed_pins: dict[str, list[Pin]] = {}
    for pin in fixed_pins:
        placed_pins.setdefault(pin.net, []).append(pin)

    # Hubs first: the block that shares the most signal nets with the others.
    signal_nets = [{p.net for p in b.pins if p.net not in rails} for b in blocks]
    shared = [len({n for n in nets
                   if any(n in other for j, other in enumerate(signal_nets) if j != i)})
              for i, nets in enumerate(signal_nets)]

    offsets: list[tuple[float, float]] = [(0.0, 0.0)] * len(blocks)
    todo = set(range(len(blocks)))
    while todo:
        def priority(i: int) -> tuple[int, int, int, int]:
            linked = len({n for n in signal_nets[i] if n in placed_pins})
            return (linked, shared[i], len(blocks[i].pins), -i)

        i = max(todo, key=priority)
        todo.discard(i)
        block = blocks[i]
        if placed_boxes:
            off = _best_offset(block, placed_boxes, placed_pins, rails, gap)
        else:
            off = (0.0, 0.0)
        offsets[i] = off
        placed_boxes.append(_moved(block.box, *off))
        for pin in block.pins:
            placed_pins.setdefault(pin.net, []).append(
                pin._replace(x=pin.x + off[0], y=pin.y + off[1]))
    return offsets


def _wire_cost(p: Pin, q: Pin) -> float:
    """Rough price of the wire p-q: length, plus a corner, plus a detour for
    each end whose partner lies behind it."""
    vx, vy = q.x - p.x, q.y - p.y
    cost = abs(vx) + abs(vy)
    if abs(vx) > 1 and abs(vy) > 1:
        cost += BEND
    if p.dx * vx + p.dy * vy < -1:
        cost += BEHIND
    if -(q.dx * vx + q.dy * vy) < -1:
        cost += BEHIND
    return cost


def _best_offset(block: Block, placed_boxes: list[Box],
                 placed_pins: dict[str, list[Pin]],
                 rails: set[str], gap: float) -> tuple[float, float]:
    bx0, by0, bx1, by1 = block.box
    outline = _union(placed_boxes)
    assert outline is not None
    half_perimeter = (outline[2] - outline[0]) + (outline[3] - outline[1])

    # Offsets that put one of our pins exactly level with a partner pin.
    level_x: set[float] = set()
    level_y: set[float] = set()
    for p in block.pins:
        if p.net in rails:
            continue
        for q in placed_pins.get(p.net, ()):
            level_x.add(round(q.x - p.x, 1))
            level_y.add(round(q.y - p.y, 1))

    cands: set[tuple[float, float]] = set()
    for r in placed_boxes:
        # Beside r: flush top, centred, flush bottom, or pin-level.
        ys = {r[1] - by0, (r[1] + r[3]) / 2 - (by0 + by1) / 2, r[3] - by1}
        ys |= {oy for oy in level_y if oy + by0 < r[3] and oy + by1 > r[1]}
        for oy in ys:
            cands.add((r[2] + gap - bx0, oy))
            cands.add((r[0] - gap - bx1, oy))
        # Above / below r.
        xs = {r[0] - bx0, (r[0] + r[2]) / 2 - (bx0 + bx1) / 2, r[2] - bx1}
        xs |= {ox for ox in level_x if ox + bx0 < r[2] and ox + bx1 > r[0]}
        for ox in xs:
            cands.add((ox, r[3] + gap - by0))
            cands.add((ox, r[1] - gap - by1))

    def cost(off: tuple[float, float]) -> float:
        ox, oy = off
        total = 0.0
        for p in block.pins:
            partners = placed_pins.get(p.net)
            if not partners:
                continue
            best = min(_wire_cost(p._replace(x=p.x + ox, y=p.y + oy), q)
                       for q in partners)
            total += best * (RAIL_WEIGHT if p.net in rails else 1.0)
        grown = _union([outline, _moved(block.box, ox, oy)])
        assert grown is not None
        total += SPREAD_WEIGHT * ((grown[2] - grown[0]) + (grown[3] - grown[1])
                                  - half_perimeter)
        return total

    valid = [c for c in cands if _clear(_moved(block.box, *c), placed_boxes, gap)]
    if not valid:
        # Everything around is taken: start a new column on the right.
        return (outline[2] + gap - bx0, outline[1] - by0)
    return min(sorted(valid), key=cost)
