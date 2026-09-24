"""Regression tests. Stdlib only: python -m unittest discover tests"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from circuitstudio import mcp_server  # noqa: E402
from circuitstudio.document import Project  # noqa: E402
from circuitstudio.registry import build_component  # noqa: E402
from circuitstudio.scene import Scene  # noqa: E402

_LINE = re.compile(r'<line x1="([-\d.]+)" y1="([-\d.]+)" x2="([-\d.]+)" y2="([-\d.]+)"')


def _stub_ends(svg: str) -> set[tuple[float, float]]:
    ends = set()
    for m in _LINE.finditer(svg):
        x1, y1, x2, y2 = map(float, m.groups())
        ends |= {(x1, y1), (x2, y2)}
    return ends


class TmpProjects(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())

    def tearDown(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)

    def project(self, components, nets=(), positions=None) -> Project:
        p = Project(self.dir, "t")
        p.circuit = {"title": "t", "components": list(components),
                     "nets": list(nets), "notes": []}
        p.layout["notes"] = {}
        p.layout["positions"] = positions or {}
        p.autoplace()
        return p


class SymbolTests(unittest.TestCase):
    def test_every_pin_tip_is_drawn(self) -> None:
        """Wires end at the pin coordinates, so a stub must reach them."""
        specs = [
            {"id": "U1", "type": "ic", "pins": {"left": ["A", "B"], "right": ["C"]}},
            {"id": "U1", "type": "ic", "pins": {"left": list("ABCDEFGH"),
                                                "right": list("IJKLMNOP"),
                                                "top": ["VCC"], "bottom": ["GND"]}},
            {"id": "U1", "type": "esp32", "pins": {"left": ["3V3", "EN"],
                                                   "right": ["GPIO2"], "bottom": ["GND"]}},
            {"id": "U1", "type": "pico", "pins": {"top": ["A", "B", "C"], "left": ["X"]}},
            {"id": "J1", "type": "connector", "n": 4},
            {"id": "R1", "type": "resistor"},
        ]
        for spec in specs:
            comp = build_component(spec)
            ends = _stub_ends(comp.svg_symbol())
            for p in comp.pins:
                gap = min(abs(p.dx * 50 - x) + abs(p.dy * 50 - y) for x, y in ends)
                self.assertLess(gap, 0.01, f"{spec['type']} pin {p.name}: gap {gap}px")


class SceneTests(TmpProjects):
    def test_bounds_follow_rotation(self) -> None:
        p = self.project([{"id": "J1", "type": "connector", "n": 12}],
                         positions={"J1": {"x": 0, "y": 0, "rotation": 90}})
        scene = Scene(p)
        x, _, w, _ = scene.bounds()
        bx0, _, bx1, _ = scene.components[0].body_bbox()
        self.assertLessEqual(x, bx0)
        self.assertGreaterEqual(x + w, bx1)

    def test_example_project_renders_cleanly(self) -> None:
        p = Project(ROOT / "projects", "esp32_led")
        p.load()
        scene = Scene(p)
        self.assertEqual(scene.errors, [])
        self.assertIn("<svg", scene.to_svg())


class LayoutBackupTests(TmpProjects):
    def test_backup_survives_later_saves(self) -> None:
        p = self.project([{"id": "R1", "type": "resistor"}])
        p.save_layout()                                  # layout.json exists now
        p = Project(self.dir, "t").load()                # a new editor session
        p.set_positions({"R1": {"x": 100, "y": 0}})
        p.save_layout()                                  # backs up the session start
        p.layout["view"] = {"x": 1, "y": 2, "w": 3, "h": 4}
        p.save_layout()                                  # a mere pan
        backup = json.loads(p.layout_backup_path.read_text(encoding="utf-8"))
        self.assertEqual(backup["positions"]["R1"]["x"], 0)


class ValidateTests(unittest.TestCase):
    def errors(self, components, nets):
        return mcp_server._validate({"components": components, "nets": nets})

    def test_valid_circuit(self) -> None:
        circuit = json.loads((ROOT / "projects" / "esp32_led.circuit.json")
                             .read_text(encoding="utf-8"))
        self.assertEqual(mcp_server._validate(circuit), [])

    def test_pin_in_two_nets(self) -> None:
        errs = self.errors([{"id": "R1", "type": "resistor"}],
                           [{"name": "a", "pins": ["R1.1"]},
                            {"name": "b", "pins": ["R1.1", "R1.2"]}])
        self.assertTrue(any("shorts" in e for e in errs), errs)

    def test_duplicate_net_name(self) -> None:
        errs = self.errors([{"id": "R1", "type": "resistor"}],
                           [{"name": "a", "pins": ["R1.1"]},
                            {"name": "a", "pins": ["R1.2"]}])
        self.assertTrue(any("Duplicate net name" in e for e in errs), errs)

    def test_dot_in_part_id(self) -> None:
        errs = self.errors([{"id": "U.1", "type": "resistor"}], [])
        self.assertTrue(any("must not contain" in e for e in errs), errs)

    def test_duplicate_pin_names(self) -> None:
        errs = self.errors([{"id": "U1", "type": "esp32",
                             "pins": {"left": ["GND"], "right": ["GND"]}}], [])
        self.assertTrue(any("more than once" in e for e in errs), errs)


class McpTests(TmpProjects):
    def setUp(self) -> None:
        super().setUp()
        self._orig = mcp_server.PROJECTS_DIR
        mcp_server.PROJECTS_DIR = self.dir

    def tearDown(self) -> None:
        mcp_server.PROJECTS_DIR = self._orig
        super().tearDown()

    def test_write_repairs_broken_circuit_file(self) -> None:
        (self.dir / "t.circuit.json").write_text("{ broken", encoding="utf-8")
        text = mcp_server.tool_write_circuit({"project": "t", "circuit": {
            "components": [{"id": "R1", "type": "resistor"}], "nets": []}})
        self.assertTrue(text.startswith("Written"), text)

    def test_transport_survives_non_object_message(self) -> None:
        msgs = "[1,2]\n" + json.dumps({"jsonrpc": "2.0", "id": 7, "method": "ping"}) + "\n"
        out = subprocess.run([sys.executable, str(ROOT / "circuitstudio_mcp.py")],
                             input=msgs, capture_output=True, text=True, timeout=30)
        replies = [json.loads(line) for line in out.stdout.splitlines()]
        self.assertEqual(replies[-1], {"jsonrpc": "2.0", "id": 7, "result": {}})


class ServerTests(TmpProjects):
    def test_autoarrange_keeps_locked_parts(self) -> None:
        from circuitstudio.server import AppState, Handler, _Server
        p = self.project([{"id": "R1", "type": "resistor"},
                          {"id": "R2", "type": "resistor"}],
                         positions={"R1": {"x": 900, "y": 900, "locked": True},
                                    "R2": {"x": 500, "y": 500}})
        p.save_circuit()
        p.save_layout()
        Handler.state = AppState(self.dir, "t")
        httpd = _Server(("127.0.0.1", 0), Handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            url = f"http://127.0.0.1:{httpd.server_address[1]}/api/autoarrange"
            req = urllib.request.Request(url, data=b"{}", method="POST",
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as res:
                comps = {c["id"]: c for c in json.load(res)["scene"]["components"]}
        finally:
            httpd.shutdown()
            httpd.server_close()
        self.assertEqual((comps["R1"]["x"], comps["R1"]["y"]), (900, 900))
        self.assertNotEqual((comps["R2"]["x"], comps["R2"]["y"]), (500, 500))


if __name__ == "__main__":
    unittest.main()
