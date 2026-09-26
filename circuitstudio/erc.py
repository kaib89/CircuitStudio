"""Electrical rule check.

Type and pin names are already validated when a circuit is written; this looks
at the level above that — whether the netlist makes sense as a circuit. These
are the mistakes a language model actually makes: a pin nobody wired up, a net
name typo that quietly splits one node into two, the same pin listed in two
nets.

Everything here is a *warning*. A schematic with open pins is perfectly valid,
which is why `nc` exists: listing a pin there says "left open on purpose" and
draws the standard no-connect cross on it.
"""
from __future__ import annotations

from typing import Any

from .document import is_ground_net, is_supply_net
from .symbols import Component

WARNING = "warning"


def _finding(message: str, ref: str = "") -> dict[str, str]:
    return {"level": WARNING, "message": message, "ref": ref}


def nc_pins(circuit: dict[str, Any]) -> set[str]:
    """Pin references the author marked as intentionally unconnected."""
    return {str(p) for p in circuit.get("nc", []) or [] if str(p)}


def pin_net_counts(circuit: dict[str, Any]) -> dict[str, int]:
    """How many nets each pin reference appears in (duplicates within one net
    are not counted — those are harmless)."""
    counts: dict[str, int] = {}
    for net in circuit.get("nets", []) or []:
        if not isinstance(net, dict):
            continue
        for ref in {str(p) for p in net.get("pins", []) or []}:
            counts[ref] = counts.get(ref, 0) + 1
    return counts


def open_pins(circuit: dict[str, Any],
              components: list[Component]) -> list[str]:
    """Pins that are in no net and not declared as no-connect."""
    wired = set(pin_net_counts(circuit))
    declared = nc_pins(circuit)
    return [f"{c.comp_id}.{p.name}"
            for c in components for p in c.pins
            if f"{c.comp_id}.{p.name}" not in wired
            and f"{c.comp_id}.{p.name}" not in declared]


def check(circuit: dict[str, Any],
          components: list[Component]) -> list[dict[str, str]]:
    """Run every rule and return the findings, most useful ones first."""
    out: list[dict[str, str]] = []
    counts = pin_net_counts(circuit)
    declared = nc_pins(circuit)
    known = {c.comp_id: c for c in components}

    for ref in open_pins(circuit, components):
        out.append(_finding(
            f"{ref} is not connected to anything. Wire it up, or list it in "
            f'"nc" to mark it as deliberately open.', ref))

    for ref in sorted(r for r, n in counts.items() if n > 1):
        out.append(_finding(
            f"{ref} appears in {counts[ref]} nets — electrically that is one "
            f"node. If the nets are meant to be separate, this is a mistake; "
            f"if not, merge them so the schematic says so.", ref))

    for ref in sorted(declared & set(counts)):
        out.append(_finding(
            f'{ref} is listed in "nc" but also wired into a net. '
            f"Remove it from one of the two.", ref))

    for ref in sorted(r for r in declared if _unknown(r, known)):
        out.append(_finding(f'"nc" refers to {ref}, which does not exist.', ref))

    seen_names: dict[str, int] = {}
    for net in circuit.get("nets", []) or []:
        if not isinstance(net, dict):
            continue
        name = str(net.get("name", ""))
        pins = [str(p) for p in net.get("pins", []) or []]
        seen_names[name] = seen_names.get(name, 0) + 1
        if len(set(pins)) < 2:
            out.append(_finding(
                f"Net '{name}' connects fewer than two pins, so it does not "
                f"connect anything.", name))
    for name, n in sorted(seen_names.items()):
        if n > 1:
            out.append(_finding(
                f"Net name '{name}' is used {n} times. Separate nets with the "
                f"same name look like one net in the drawing.", name))

    out += _tie_findings(circuit, known)

    connected = {r.split(".", 1)[0] for r in counts}
    for c in components:
        if c.comp_id not in connected:
            out.append(_finding(
                f"{c.comp_id} is not connected to any net.", c.comp_id))

    return out


def dangling_tie(comp_id: str) -> dict[str, str]:
    """A GND/VCC/label symbol that ended up with no pin to serve — it is
    the nearest symbol of its net for none of the net's pins."""
    finding = _finding(
        f"{comp_id} is not the nearest symbol for any pin of its net, so it "
        f"hangs in the air. Drag it next to the pins it should serve, or have "
        f"it removed if it is one too many.", comp_id)
    finding["kind"] = "placement"   # the human's to fix, not the assistant's
    return finding


def _tie_findings(circuit: dict[str, Any],
                  known: dict[str, Component]) -> list[dict[str, str]]:
    """GND/VCC/label symbols whose net contradicts what they claim."""
    out: list[dict[str, str]] = []
    for net in circuit.get("nets", []) or []:
        if not isinstance(net, dict):
            continue
        name = str(net.get("name", ""))
        for cid in dict.fromkeys(str(p).split(".", 1)[0]
                                 for p in net.get("pins", []) or []):
            comp = known.get(cid)
            if comp is None:
                continue
            if comp.comp_type == "label" and comp.value.strip() != name.strip():
                out.append(_finding(
                    f"Label {cid} reads '{comp.value}' but sits on net '{name}'. "
                    f"Set its value to the net name, or it will be misread.", cid))
            elif comp.comp_type == "ground" and not is_ground_net(name):
                out.append(_finding(
                    f"Ground symbol {cid} is on net '{name}', which is not a "
                    f"ground net. Use a label there instead.", cid))
            elif comp.comp_type in ("vcc", "vdd") and not is_supply_net(name):
                out.append(_finding(
                    f"Supply symbol {cid} is on net '{name}', which is not a "
                    f"supply net. Use a label there instead.", cid))
    return out


def _unknown(ref: str, known: dict[str, Component]) -> bool:
    cid, _, pin = str(ref).partition(".")
    comp = known.get(cid)
    if comp is None:
        return True
    if not pin:
        return True
    try:
        comp.pin(pin)
    except KeyError:
        return True
    return False


def format_report(findings: list[dict[str, str]]) -> str:
    if not findings:
        return "No warnings — every pin is either wired or declared as no-connect."
    lines = [f"{len(findings)} warning(s):"]
    lines += [f"- {f['message']}" for f in findings]
    return "\n".join(lines)
