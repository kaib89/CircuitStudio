"""Breadboard plans: footprints, the connectivity check, drawing, MCP tools."""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from circuitstudio import mcp_server  # noqa: E402
from circuitstudio.bb_scene import BreadboardScene  # noqa: E402
from circuitstudio.breadboard import (  # noqa: E402
    OPEN, SHORT, STRAY, STRUCTURE, UNPLACED, Breadboard, parse_hole)
from circuitstudio.document import Project  # noqa: E402

# NE555 with one resistor: VCC on the top + rail, GND on the bottom − rail.
TIMER = {
    "components": [
        {"id": "U1", "type": "ne555"},
        {"id": "R1", "type": "resistor", "value": "10k"},
        {"id": "VCC1", "type": "vcc"},
        {"id": "GND1", "type": "ground"},
    ],
    "nets": [
        {"name": "VCC", "pins": ["U1.VCC", "U1.RST", "R1.1", "VCC1.pin"]},
        {"name": "GND", "pins": ["U1.GND", "GND1.pin"]},
        {"name": "DIS", "pins": ["U1.DIS", "R1.2"]},
    ],
    "nc": ["U1.TRG", "U1.OUT", "U1.CTL", "U1.THR"],
}
# Pin 1 in e10: pins 1-4 run along e10..e13, pins 8-5 along f10..f13.
TIMER_PLAN = {
    "board": {"columns": 30},
    "parts": {
        "U1": {"anchor": "e10", "rotation": 0},
        "R1": {"legs": {"1": "T+20", "2": "g11"}},
    },
    "wires": [
        {"from": "j10", "to": "T+10"},      # pin 8 VCC
        {"from": "a10", "to": "B-10"},      # pin 1 GND
        {"from": "a13", "to": "T+13"},      # pin 4 RST
    ],
}

# Pico with a pull-up on GP2 and a capacitor from GP2 to GND.
PICO = {
    "components": [
        {"id": "U2", "type": "pico", "pins": {"left": ["GP2"], "right": ["3V3", "GND"]}},
        {"id": "R1", "type": "resistor"},
        {"id": "C1", "type": "capacitor"},
    ],
    "nets": [
        {"name": "SIG", "pins": ["U2.GP2", "R1.1", "C1.1"]},
        {"name": "3V3", "pins": ["U2.3V3", "R1.2"]},
        {"name": "GND", "pins": ["U2.GND", "C1.2"]},
    ],
}


def kinds(bb: Breadboard) -> list[str]:
    return [f["kind"] for f in bb.findings]


def plan(**changes) -> dict:
    p = json.loads(json.dumps(TIMER_PLAN))
    p.update(changes)
    return p


class HoleTests(unittest.TestCase):
    def test_names(self) -> None:
        self.assertEqual(parse_hole("E30", 63).name, "e30")
        self.assertTrue(parse_hole("t+5", 63).rail)
        self.assertEqual(parse_hole("B-63", 63).name, "B-63")
        for bad in ("k3", "e0", "e64", "T5", "", "30e"):
            self.assertIsNone(parse_hole(bad, 63), bad)


class CheckTests(unittest.TestCase):
    def test_correct_plan_is_clean(self) -> None:
        bb = Breadboard(TIMER_PLAN, TIMER)
        self.assertEqual(bb.findings, [])

    def test_dip_not_straddling_shorts_its_rows(self) -> None:
        # Pin 1 in row a: the second row lands in d — same half columns.
        bb = Breadboard(plan(parts={"U1": {"anchor": "a10"},
                                    "R1": TIMER_PLAN["parts"]["R1"]}, wires=[]), TIMER)
        self.assertEqual(bb.structure_errors, [])
        self.assertIn(SHORT, kinds(bb))
        short = next(f for f in bb.findings if f["kind"] == SHORT)
        self.assertIn("column 10 (a–e)", short["message"])

    def test_part_off_the_strip_is_a_structure_error(self) -> None:
        bb = Breadboard(plan(parts={"U1": {"anchor": "j10"}}), TIMER)
        self.assertTrue(any("land off" in e for e in bb.structure_errors),
                        bb.structure_errors)

    def test_two_leads_in_one_hole(self) -> None:
        p = plan()
        p["parts"]["R1"] = {"legs": {"1": "j10", "2": "g11"}}   # j10 has a wire
        bb = Breadboard(p, TIMER)
        self.assertTrue(any("used twice" in e for e in bb.structure_errors))

    def test_open_connection(self) -> None:
        p = plan()
        p["parts"]["R1"] = {"legs": {"1": "T+20", "2": "g12"}}  # THR, not DIS
        bb = Breadboard(p, TIMER)
        self.assertIn(OPEN, kinds(bb))
        self.assertIn(STRAY, kinds(bb))     # THR is no-connect but now live

    def test_unplaced_parts_and_leads(self) -> None:
        p = plan()
        p["parts"]["R1"] = {"legs": {"1": "T+20"}}
        bb = Breadboard(p, TIMER)
        self.assertTrue(any("R1: 2 (net DIS)" in f["message"] for f in bb.findings))
        del p["parts"]["R1"]
        self.assertIn("R1 is not on the breadboard yet",
                      [f["message"] for f in Breadboard(p, TIMER).findings])
        self.assertEqual(kinds(Breadboard(p, TIMER)), [UNPLACED])

    def test_net_symbols_are_not_parts(self) -> None:
        p = plan()
        p["parts"]["GND1"] = {"legs": {"pin": "a20"}}
        bb = Breadboard(p, TIMER)
        self.assertTrue(any("net symbol" in e for e in bb.structure_errors))

    def test_transistor_needs_a_pinout(self) -> None:
        circuit = {"components": [{"id": "Q1", "type": "npn"}], "nets": []}
        bb = Breadboard({"parts": {"Q1": {"anchor": "e5"}}}, circuit)
        self.assertTrue(any("datasheet" in e for e in bb.structure_errors))
        circuit["components"][0]["pinout"] = {"emitter": 1, "base": 2, "collector": 3}
        self.assertEqual(Breadboard({"parts": {"Q1": {"anchor": "e5"}}},
                                    circuit).structure_errors, [])

    def test_pico_ground_pins_are_joined_on_the_board(self) -> None:
        # U2.GND is pin 38 (column 3, f-j); C1 goes into pin 3's column
        # (column 3, a-e). No wire, but both are the Pico's GND plane.
        p = {"parts": {"U2": {"anchor": "c1"},
                       "R1": {"legs": {"1": "a4", "2": "i5"}},
                       "C1": {"legs": {"1": "b4", "2": "a3"}}}}
        bb = Breadboard(p, PICO)
        self.assertEqual(bb.findings, [])

    def test_unused_pico_pin_in_a_live_column(self) -> None:
        # Column 4 f-j holds pin 37 (3V3_EN), not 3V3 (pin 36, column 5).
        p = {"parts": {"U2": {"anchor": "c1"},
                       "R1": {"legs": {"1": "a4", "2": "i4"}},
                       "C1": {"legs": {"1": "b4", "2": "a3"}}}}
        found = Breadboard(p, PICO).findings
        self.assertIn(STRAY, [f["kind"] for f in found])
        self.assertTrue(any("3V3_EN" in f["message"] for f in found))
        self.assertIn(OPEN, [f["kind"] for f in found])

    def test_wires_through_rails(self) -> None:
        p = {"parts": {"U2": {"anchor": "c1"},
                       "R1": {"legs": {"1": "a4", "2": "T+20"}},
                       "C1": {"legs": {"1": "b4", "2": "B-20"}}},
             "wires": [{"from": "i5", "to": "T+5"}, {"from": "b3", "to": "B-3"}]}
        self.assertEqual(Breadboard(p, PICO).findings, [])
        p["board"] = {"split_rails": True, "columns": 30}   # T+5 and T+20 now apart
        self.assertIn(OPEN, kinds(Breadboard(p, PICO)))


class ValidationTests(unittest.TestCase):
    def errors(self, spec) -> list[str]:
        return mcp_server._validate({"components": [spec], "nets": []})

    def test_ic_package_and_pinout(self) -> None:
        ic = {"id": "U1", "type": "ic", "pins": {"left": ["A"], "right": ["Y"],
                                                "top": ["VCC"], "bottom": ["GND"]}}
        self.assertEqual(self.errors(ic), [])
        self.assertTrue(any("go together" in e
                            for e in self.errors({**ic, "package": "DIP-8"})))
        good = {**ic, "package": "DIP-8", "pinout": {"A": 1, "Y": 2, "GND": 4, "VCC": 8}}
        self.assertEqual(self.errors(good), [])
        dup = {**good, "pinout": {"A": 1, "Y": 1, "GND": 4, "VCC": 8}}
        self.assertTrue(any("both given pin number 1" in e for e in self.errors(dup)))
        big = {**good, "pinout": {"A": 1, "Y": 2, "GND": 4, "VCC": 9}}
        self.assertTrue(any("between 1 and 8" in e for e in self.errors(big)))
        self.assertTrue(any("unknown package" in e
                            for e in self.errors({**good, "package": "QFN-8"})))

    def test_leaded_parts_take_no_package(self) -> None:
        errs = self.errors({"id": "R1", "type": "resistor", "package": "DIP-8"})
        self.assertTrue(any("lead by lead" in e for e in errs), errs)

    def test_unknown_pico_pin(self) -> None:
        spec = {"id": "U2", "type": "pico", "pins": {"left": ["LED"]}, "pinout": {}}
        self.assertTrue(any("not a Pico header pin" in e for e in self.errors(spec)))
        spec["pinout"] = {"LED": 25}
        self.assertEqual(self.errors(spec), [])


class SceneTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())

    def tearDown(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)

    def project(self, circuit, bb_plan) -> Project:
        (self.dir / "t.circuit.json").write_text(json.dumps(circuit), encoding="utf-8")
        if bb_plan is not None:
            (self.dir / "t.breadboard.json").write_text(json.dumps(bb_plan),
                                                        encoding="utf-8")
        return Project(self.dir, "t").load()

    def test_svg_carries_parts_and_nodes(self) -> None:
        p = self.project(TIMER, TIMER_PLAN)
        scene = BreadboardScene(p)
        svg = scene.to_svg()
        self.assertTrue(svg.startswith("<svg"))
        self.assertIn('data-part="U1"', svg)
        self.assertIn('data-part="R1"', svg)
        self.assertNotIn("bbmark", svg)          # editor aids stay out of exports
        d = scene.to_dict()
        self.assertTrue(d["exists"])
        vcc = [n for n in d["nodes"].values() if n["nets"] == ["VCC"]]
        self.assertTrue(vcc and any("top + rail" in w for w in vcc[0]["where"]))

    def test_live_reload_bumps_the_version(self) -> None:
        p = self.project(TIMER, None)
        self.assertIsNone(p.breadboard)
        v = p.version
        path = self.dir / "t.breadboard.json"
        path.write_text(json.dumps(TIMER_PLAN), encoding="utf-8")
        os.utime(path, (time.time() + 5, time.time() + 5))
        self.assertTrue(p.reload_breadboard_if_changed())
        self.assertGreater(p.version, v)
        self.assertFalse(p.reload_breadboard_if_changed())

    def test_broken_file_is_reported(self) -> None:
        (self.dir / "t.breadboard.json").write_text("{ nope", encoding="utf-8")
        p = self.project(TIMER, None)
        self.assertIsNone(p.breadboard)
        self.assertIn("not valid JSON", p.breadboard_error)


class McpBreadboardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())
        self._orig = mcp_server.PROJECTS_DIR
        mcp_server.PROJECTS_DIR = self.dir
        mcp_server.tool_write_circuit({"project": "t", "circuit": json.loads(
            json.dumps(TIMER))})

    def tearDown(self) -> None:
        mcp_server.PROJECTS_DIR = self._orig
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_write_read_and_review(self) -> None:
        self.assertIn("no breadboard plan",
                      mcp_server.tool_get_breadboard({"project": "t"}))
        text = mcp_server.tool_write_breadboard({"project": "t",
                                                 "breadboard": TIMER_PLAN})
        self.assertTrue(text.startswith("Written"), text)
        self.assertIn("matches the netlist", text)
        stored = json.loads(mcp_server.tool_get_breadboard({"project": "t"}))
        self.assertEqual(stored, TIMER_PLAN)
        review = "\n".join(b["text"] for b in mcp_server.tool_review_project(
            {"project": "t"}) if b["type"] == "text")
        self.assertIn("Breadboard:", review)

    def test_broken_plan_is_not_written(self) -> None:
        text = mcp_server.tool_write_breadboard({"project": "t", "breadboard": {
            "parts": {"U1": {"anchor": "x1"}}}})
        self.assertTrue(text.startswith("NOT written"), text)
        self.assertFalse((self.dir / "t.breadboard.json").exists())

    def test_connection_problems_are_written_and_reported(self) -> None:
        bad = plan(parts={"U1": {"anchor": "a10"}, "R1": TIMER_PLAN["parts"]["R1"]},
                   wires=[])
        text = mcp_server.tool_write_breadboard({"project": "t", "breadboard": bad})
        self.assertTrue(text.startswith("Written"), text)
        self.assertIn("Short between", text)

    def test_circuit_change_rechecks_the_plan(self) -> None:
        mcp_server.tool_write_breadboard({"project": "t", "breadboard": TIMER_PLAN})
        text = mcp_server.tool_update_circuit({"project": "t", "changes": {
            "connect": [{"net": "DIS", "pins": ["U1.THR"]}],
            "remove_nc": ["U1.THR"]}})
        self.assertIn("Breadboard:", text)
        self.assertIn("Open: net 'DIS'", text)


class EditorTests(unittest.TestCase):
    """The editor's side: saving a changed plan, and what it is told."""

    def setUp(self) -> None:
        from circuitstudio.server import AppState, Handler, _Server
        self.dir = Path(tempfile.mkdtemp())
        (self.dir / "t.circuit.json").write_text(json.dumps(TIMER), encoding="utf-8")
        (self.dir / "t.breadboard.json").write_text(json.dumps(TIMER_PLAN),
                                                    encoding="utf-8")
        Handler.state = AppState(self.dir, "t")
        self.httpd = _Server(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def post(self, path: str, body: dict) -> dict:
        req = urllib.request.Request(self.base + path, method="POST",
                                     data=json.dumps(body).encode("utf-8"),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as res:
            return json.load(res)

    def test_saving_marks_the_plan_as_the_humans(self) -> None:
        plan = json.loads(json.dumps(TIMER_PLAN))
        plan["parts"]["R1"]["legs"]["2"] = "g12"
        payload = self.post("/api/breadboard", {"plan": plan})
        stored = json.loads((self.dir / "t.breadboard.json").read_text(encoding="utf-8"))
        self.assertEqual(stored["parts"]["R1"]["legs"]["2"], "g12")
        self.assertIn("edited_by_human", stored)
        # The first save of a session keeps the assistant's version.
        backup = json.loads((self.dir / "t.breadboard.bak.json").read_text(encoding="utf-8"))
        self.assertEqual(backup, TIMER_PLAN)
        kinds = [f["kind"] for f in payload["breadboard"]["findings"]]
        self.assertIn(OPEN, kinds)

    def test_payload_tells_the_editor_what_it_needs(self) -> None:
        bb = self.post("/api/layout", {})["breadboard"]
        self.assertEqual(bb["parts"]["U1"]["kind"], "rigid")
        self.assertEqual(bb["parts"]["U1"]["anchor"], "e10")
        self.assertEqual(bb["parts"]["R1"]["kind"], "legs")
        self.assertEqual(bb["used"]["e10"], {"part": "U1", "pin": "GND"})
        self.assertEqual(bb["used"]["T+10"], {"wire": 0, "end": "to"})
        self.assertEqual(bb["unplaced"], [])
        self.assertIn("rowOrder", bb["geometry"])
        # Unplugging R1 puts it on the tray, ready to be dragged back.
        plan = json.loads(json.dumps(TIMER_PLAN))
        del plan["parts"]["R1"]
        bb = self.post("/api/breadboard", {"plan": plan})["breadboard"]
        self.assertEqual([u["id"] for u in bb["unplaced"]], ["R1"])
        self.assertEqual(bb["unplaced"][0]["pins"], ["1", "2"])

    def test_breadboard_hand_back(self) -> None:
        payload = self.post("/api/review", {"what": "breadboard"})
        self.assertTrue(payload["bbReview"]["reviewed"])
        self.assertTrue(payload["bbReview"]["current"])
        self.assertFalse(payload["review"]["reviewed"])   # the schematic is separate
        self.assertTrue((self.dir / "t.breadboard.svg").exists())
        plan = json.loads(json.dumps(TIMER_PLAN))
        plan["wires"].append({"from": "j20", "to": "j25"})
        payload = self.post("/api/breadboard", {"plan": plan})
        self.assertFalse(payload["bbReview"]["current"])


class OwnershipTests(unittest.TestCase):
    """Once the human has arranged the board, the assistant patches it."""

    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())
        self._orig = mcp_server.PROJECTS_DIR
        mcp_server.PROJECTS_DIR = self.dir
        mcp_server.tool_write_circuit({"project": "t", "circuit": json.loads(
            json.dumps(TIMER))})
        mcp_server.tool_write_breadboard({"project": "t", "breadboard": TIMER_PLAN})
        edited = json.loads(json.dumps(TIMER_PLAN))
        edited["edited_by_human"] = "2026-09-30T18:00:00+02:00"
        (self.dir / "t.breadboard.json").write_text(json.dumps(edited), encoding="utf-8")

    def tearDown(self) -> None:
        mcp_server.PROJECTS_DIR = self._orig
        shutil.rmtree(self.dir, ignore_errors=True)

    def stored(self) -> dict:
        return json.loads((self.dir / "t.breadboard.json").read_text(encoding="utf-8"))

    def test_write_refuses_to_overwrite_the_humans_plan(self) -> None:
        text = mcp_server.tool_write_breadboard({"project": "t", "breadboard": {
            "parts": {}}})
        self.assertTrue(text.startswith("NOT written"), text)
        self.assertIn("update_breadboard", text)
        self.assertEqual(self.stored()["parts"], TIMER_PLAN["parts"])

    def test_replace_overwrites_and_keeps_a_backup(self) -> None:
        text = mcp_server.tool_write_breadboard({"project": "t", "replace": True,
                                                 "breadboard": {"parts": {}}})
        self.assertTrue(text.startswith("Written"), text)
        self.assertEqual(self.stored()["parts"], {})
        self.assertNotIn("edited_by_human", self.stored())
        backup = json.loads((self.dir / "t.breadboard.bak.json").read_text(encoding="utf-8"))
        self.assertEqual(backup["parts"], TIMER_PLAN["parts"])

    def test_update_patches_and_keeps_the_rest(self) -> None:
        text = mcp_server.tool_update_breadboard({"project": "t", "changes": {
            "place": {"R1": {"legs": {"1": "T+22", "2": "g11"}}},
            "remove_wires": [{"from": "T+13", "to": "a13"}],
            "add_wires": [{"from": "b13", "to": "T+14"}]}})
        self.assertIn("moved R1", text)
        self.assertIn("removed wire", text)
        self.assertIn("matches the netlist", text)
        plan = self.stored()
        self.assertEqual(plan["parts"]["R1"]["legs"]["1"], "T+22")
        self.assertEqual(plan["parts"]["U1"], TIMER_PLAN["parts"]["U1"])
        self.assertIn("edited_by_human", plan)
        self.assertEqual(len(plan["wires"]), 3)

    def test_update_that_breaks_the_plan_is_not_written(self) -> None:
        text = mcp_server.tool_update_breadboard({"project": "t", "changes": {
            "place": {"R1": {"legs": {"1": "j10", "2": "g11"}}}}})   # j10: a wire
        self.assertTrue(text.startswith("NOT written"), text)
        self.assertEqual(self.stored()["parts"]["R1"], TIMER_PLAN["parts"]["R1"])

    def test_remove_wires_at_a_hole_and_unplug(self) -> None:
        text = mcp_server.tool_update_breadboard({"project": "t", "changes": {
            "remove_wires": ["T+10"], "remove_parts": ["R1", "R9"]}})
        self.assertIn("unplugged R1", text)
        self.assertIn("R9: was not on the board", text)
        self.assertEqual(len(self.stored()["wires"]), 2)
        self.assertIn("R1 is not on the breadboard yet", text)

    def test_review_says_who_owns_the_plan(self) -> None:
        text = "\n".join(b["text"] for b in mcp_server.tool_review_project(
            {"project": "t"}) if b["type"] == "text")
        self.assertIn("update_breadboard, not write_breadboard", text)


if __name__ == "__main__":
    unittest.main()
