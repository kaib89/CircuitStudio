"""Breadboard auto-placement: the result must always pass the check."""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from circuitstudio import mcp_server  # noqa: E402
from circuitstudio.bb_place import autoplace  # noqa: E402
from circuitstudio.breadboard import Breadboard  # noqa: E402
from test_breadboard import PICO, TIMER, TIMER_PLAN  # noqa: E402

# Two chips from the library, passives between them, a decoupling cap and an
# off-board speaker — enough to exercise every step.
MIXED = {
    "components": [
        {"id": "U1", "part": "74HC00"},
        {"id": "U2", "part": "LM358"},
        {"id": "R1", "type": "resistor"}, {"id": "R2", "type": "resistor"},
        {"id": "R3", "type": "resistor"}, {"id": "C1", "type": "capacitor"},
        {"id": "C2", "type": "capacitor"}, {"id": "LS1", "type": "speaker"},
        {"id": "GND1", "type": "ground"}, {"id": "VCC1", "type": "vcc", "value": "5V"},
    ],
    "nets": [
        {"name": "5V", "pins": ["U1.VCC", "U2.V+", "C1.1", "R3.1", "VCC1.pin"]},
        {"name": "GND", "pins": ["U1.GND", "U2.V-", "C1.2", "C2.2", "LS1.-",
                                 "U1.1B", "U1.2B", "U1.3A", "U1.3B", "U1.4A", "U1.4B",
                                 "U2.IN2+", "GND1.pin"]},
        {"name": "osc", "pins": ["U1.1A", "R1.1", "C2.1"]},
        {"name": "fb", "pins": ["U1.1Y", "R1.2", "U1.2A"]},
        {"name": "sig", "pins": ["U1.2Y", "R2.1"]},
        {"name": "inv", "pins": ["R2.2", "U2.IN1-", "R3.2"]},
        {"name": "out", "pins": ["U2.OUT1", "U2.IN1+", "LS1.+"]},
        {"name": "fb2", "pins": ["U2.IN2-", "U2.OUT2"]},
    ],
    "nc": ["U1.3Y", "U1.4Y"],
}


def check(plan, circuit):
    return [f["kind"] for f in Breadboard(plan, circuit).findings]


class AutoplaceTests(unittest.TestCase):
    def test_small_circuit_goes_on_a_half_board(self) -> None:
        plan, log, failed = autoplace(TIMER, None, "all")
        self.assertEqual(failed, [])
        self.assertEqual(check(plan, TIMER), [])
        self.assertEqual(plan["board"]["columns"], 30)
        self.assertEqual(set(plan["parts"]), {"U1", "R1"})

    def test_mixed_circuit_is_clean_and_tidy(self) -> None:
        plan, log, failed = autoplace(MIXED, None, "all")
        self.assertEqual(failed, [])
        self.assertEqual(check(plan, MIXED), [])
        self.assertTrue(plan["parts"]["LS1"]["offboard"])
        bb = Breadboard(plan, MIXED)
        spans: dict[str, list[tuple[int, int]]] = {}
        for part in bb.parts.values():
            if part.anchor is not None or part.offboard or len(part.pins) != 2:
                continue
            a, b = (p.hole for p in part.pins)
            # A lead never crosses the board to the rail on the other side.
            for x, y in ((a, b), (b, a)):
                if x.rail and not y.rail:
                    self.assertEqual(x.row[0] == "T", y.row in "fghij", part.cid)
            # Two parts never lie on top of each other in one row.
            if a.row == b.row:
                lo, hi = sorted((a.col, b.col))
                for s, e in spans.get(a.row, []):
                    self.assertTrue(hi < s or lo > e, f"{part.cid} overlaps in row {a.row}")
                spans.setdefault(a.row, []).append((lo, hi))

    def test_pico_leaves_the_holes_under_it_alone(self) -> None:
        plan, _, failed = autoplace(PICO, None, "all")
        self.assertEqual(failed, [])
        self.assertEqual(Breadboard(plan, PICO).findings, [])

    def test_rest_keeps_what_is_placed(self) -> None:
        plan = json.loads(json.dumps(TIMER_PLAN))
        del plan["parts"]["R1"]
        plan["edited_by_human"] = "2026-09-30T18:00:00+02:00"
        new, log, failed = autoplace(TIMER, plan, "rest")
        self.assertEqual(new["parts"]["U1"], TIMER_PLAN["parts"]["U1"])
        self.assertIn("R1", new["parts"])
        self.assertEqual(new["wires"][:3], TIMER_PLAN["wires"])
        self.assertEqual(new["edited_by_human"], plan["edited_by_human"])
        self.assertEqual(check(new, TIMER), [])

    def test_parts_without_pinout_are_reported_not_guessed(self) -> None:
        circuit = json.loads(json.dumps(MIXED))
        circuit["components"].append({"id": "Q1", "type": "npn"})
        plan, _, failed = autoplace(circuit, None, "all")
        self.assertNotIn("Q1", plan["parts"])
        self.assertTrue(any(f.startswith("Q1:") and "datasheet" in f for f in failed))


def board_led(part: str, pin: str, gnd: str = "GND") -> dict:
    return {"components": [{"id": "U1", "part": part},
                           {"id": "R1", "type": "resistor"},
                           {"id": "D1", "type": "led"}],
            "nets": [{"name": "led", "pins": [f"U1.{pin}", "R1.1"]},
                     {"name": "a", "pins": ["R1.2", "D1.anode"]},
                     {"name": "GND", "pins": [f"U1.{gnd}", "D1.cathode"]}]}


class BoardTests(unittest.TestCase):
    def test_boards_straddle_the_channel(self) -> None:
        # Row of pin 1 follows from the row distance: 0.6" Nano sits in d/h,
        # 0.9" D1 mini and 1.0" ESP32 in b and above.
        for part, pin, row in (("Arduino-Nano", "D13", "d"), ("D1-mini", "D4", "b"),
                               ("ESP32-DevKitC", "IO2", "b")):
            circuit = board_led(part, pin)
            self.assertEqual(mcp_server._validate(circuit), [], part)
            plan, _, failed = autoplace(circuit, None, "all")
            self.assertEqual(failed, [], part)
            self.assertEqual(check(plan, circuit), [], part)
            self.assertEqual(plan["parts"]["U1"]["anchor"][0], row, part)

    def test_board_pins_joined_on_the_board(self) -> None:
        # The Nano's second GND pin is the same net as the first: using
        # either one is fine, and no wire between them is needed.
        circuit = board_led("Arduino-Nano", "D13", gnd="GND_2")
        circuit["nets"][2]["pins"].append("U1.GND")
        plan = {"parts": {"U1": {"anchor": "d2"},
                          "R1": {"legs": {"1": "c2", "2": "c18"}},
                          "D1": {"legs": {"anode": "b18", "cathode": "b15"}}}}
        self.assertEqual(check(plan, circuit), [])     # D1 into GND's column 15

    def test_nothing_goes_under_a_board(self) -> None:
        circuit = board_led("Arduino-Nano", "D13")
        plan = {"parts": {"U1": {"anchor": "d2"},
                          "R1": {"legs": {"1": "c2", "2": "f20"}}}}
        errors = Breadboard(plan, circuit).structure_errors
        self.assertEqual(errors, [])
        plan["parts"]["R1"]["legs"]["2"] = "f5"          # under the Nano
        self.assertTrue(any("covered by U1" in e
                            for e in Breadboard(plan, circuit).structure_errors))


class AutoplaceToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())
        self._orig = mcp_server.PROJECTS_DIR
        mcp_server.PROJECTS_DIR = self.dir
        mcp_server.tool_write_circuit({"project": "t", "circuit": json.loads(
            json.dumps(TIMER))})

    def tearDown(self) -> None:
        mcp_server.PROJECTS_DIR = self._orig
        shutil.rmtree(self.dir, ignore_errors=True)

    def stored(self) -> dict:
        return json.loads((self.dir / "t.breadboard.json").read_text(encoding="utf-8"))

    def test_creates_a_plan(self) -> None:
        text = mcp_server.tool_autoplace_breadboard({"project": "t"})
        self.assertIn("matches the netlist", text)
        self.assertEqual(set(self.stored()["parts"]), {"U1", "R1"})

    def test_start_over_respects_the_humans_plan(self) -> None:
        human = json.loads(json.dumps(TIMER_PLAN))
        human["edited_by_human"] = "2026-09-30T18:00:00+02:00"
        (self.dir / "t.breadboard.json").write_text(json.dumps(human), encoding="utf-8")
        text = mcp_server.tool_autoplace_breadboard({"project": "t", "mode": "all"})
        self.assertTrue(text.startswith("NOT changed"), text)
        self.assertEqual(self.stored(), human)
        text = mcp_server.tool_autoplace_breadboard({"project": "t", "mode": "rest"})
        self.assertTrue(text.startswith("Written"), text)
        self.assertEqual(self.stored()["parts"], human["parts"])   # nothing was missing
        text = mcp_server.tool_autoplace_breadboard({"project": "t", "mode": "all",
                                                     "replace": True})
        self.assertTrue(text.startswith("Written"), text)


class AutoplaceEditorTests(unittest.TestCase):
    def test_button_creates_a_plan_owned_by_the_human(self) -> None:
        from circuitstudio.server import AppState, Handler, _Server
        d = Path(tempfile.mkdtemp())
        try:
            (d / "t.circuit.json").write_text(json.dumps(TIMER), encoding="utf-8")
            Handler.state = AppState(d, "t")
            httpd = _Server(("127.0.0.1", 0), Handler)
            threading.Thread(target=httpd.serve_forever, daemon=True).start()
            try:
                req = urllib.request.Request(
                    f"http://127.0.0.1:{httpd.server_address[1]}/api/breadboard/autoplace",
                    data=b'{"mode": "all"}', method="POST",
                    headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=30) as res:
                    payload = json.load(res)
            finally:
                httpd.shutdown()
                httpd.server_close()
            self.assertTrue(payload["breadboard"]["exists"])
            self.assertEqual(payload["autoplace"]["failed"], [])
            stored = json.loads((d / "t.breadboard.json").read_text(encoding="utf-8"))
            self.assertIn("edited_by_human", stored)
            self.assertEqual(payload["breadboard"]["findings"], [])
        finally:
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
