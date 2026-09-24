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
import urllib.error
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


class GeometryCompatTests(unittest.TestCase):
    """Pin positions are what layouts are aligned to; they must not drift."""

    def tips(self, spec):
        c = build_component(spec)
        return {p.name: (round(p.dx * 50, 3), round(p.dy * 50, 3)) for p in c.pins}

    def test_board_pins_unchanged(self) -> None:
        t = self.tips({"id": "U1", "type": "esp32",
                       "pins": {"left": ["3V3", "EN"], "right": ["IO2"], "bottom": ["GND"]}})
        self.assertEqual(t, {"3V3": (-90, -20), "EN": (-90, 20), "IO2": (90, 0),
                             "GND": (0, 90)})

    def test_side_only_ic_pins_unchanged(self) -> None:
        t = self.tips({"id": "U1", "type": "ic",
                       "pins": {"left": ["A", "B", "C"], "right": ["D"]}})
        self.assertEqual(t, {"A": (-70, -40), "B": (-70, 0), "C": (-70, 40),
                             "D": (70, 0)})

    def test_ic_leads_have_constant_length(self) -> None:
        c = build_component({"id": "U1", "type": "ic", "pins": {
            "left": list("ABCDEFGH"), "right": list("IJKLMNOP"),
            "top": ["VCC"], "bottom": ["GND"]}})
        x0, y0, x1, y1 = c._body_local()
        self.assertEqual(c.pin("VCC").dy * 50, y0 - 30)
        self.assertEqual(c.pin("A").dx * 50, x0 - 30)


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


def _old_mst(pts):
    """The original O(n^3) Prim loop, kept as the reference."""
    n = len(pts)
    in_tree = [False] * n
    in_tree[0] = True
    edges = []
    for _ in range(n - 1):
        best_d, best_i, best_j = float("inf"), 0, 1
        for i in range(n):
            if not in_tree[i]:
                continue
            for j in range(n):
                if in_tree[j]:
                    continue
                d = abs(pts[i][0] - pts[j][0]) + abs(pts[i][1] - pts[j][1])
                if d < best_d:
                    best_d, best_i, best_j = d, i, j
        edges.append((best_i, best_j))
        in_tree[best_j] = True
    return edges


class RoutingTests(TmpProjects):
    def chain(self, n=8):
        """A row of resistors, each wired to the next, all sharing GND."""
        comps = [{"id": f"R{i}", "type": "resistor"} for i in range(n)]
        comps.append({"id": "GND1", "type": "ground"})
        nets = [{"name": f"n{i}", "pins": [f"R{i}.2", f"R{i + 1}.1"]}
                for i in range(n - 1)]
        nets.append({"name": "GND", "pins": ["R0.1", "GND1.pin"]})
        pos = {f"R{i}": {"x": i * 160, "y": (i % 2) * 120} for i in range(n)}
        pos["GND1"] = {"x": -100, "y": 200}
        return self.project(comps, nets, positions=pos)

    def test_mst_matches_original_including_ties(self) -> None:
        import random
        from circuitstudio.router import _mst_edges
        rnd = random.Random(1)
        for _ in range(300):
            # a coarse lattice makes equal distances (ties) very common
            pts = [(rnd.randint(0, 6) * 10.0, rnd.randint(0, 6) * 10.0)
                   for _ in range(rnd.randint(2, 12))]
            self.assertEqual(_mst_edges(pts), _old_mst(pts))

    def test_moving_a_part_keeps_unrelated_wires(self) -> None:
        p = self.chain()
        first = Scene(p)
        self.assertTrue(p.routes_dirty)
        before = {n["name"]: w for n, w in zip(first.nets, first.wires)}
        p.set_positions({"R7": {"x": 7 * 160, "y": 300}})
        second = Scene(p)
        after = {n["name"]: w for n, w in zip(second.nets, second.wires)}
        self.assertLessEqual(second.rerouted, 2)       # only n6 touches R7
        for name in ("n0", "n1", "n2", "n3", "GND"):
            self.assertEqual(before[name], after[name], name)

    def test_routes_survive_a_new_session(self) -> None:
        p = self.chain()
        wires = Scene(p).wires
        p.save_circuit()
        p.save_routes()
        again = Project(self.dir, "t").load()
        scene = Scene(again)
        self.assertEqual(scene.rerouted, 0)
        self.assertEqual(scene.wires, wires)

    def test_stored_wire_is_dropped_when_a_part_lands_on_it(self) -> None:
        p = self.chain(3)
        scene = Scene(p)
        seg = next(s for n, w in zip(scene.nets, scene.wires)
                   if n["name"] == "n0" for s in w
                   if abs(s[0][0] - s[1][0]) > 40)     # a long horizontal run
        mid = ((seg[0][0] + seg[1][0]) / 2, seg[0][1])
        comps = p.circuit["components"] + [{"id": "C9", "type": "capacitor"}]
        p.circuit["components"] = comps
        p.layout["positions"]["C9"] = {"x": mid[0], "y": mid[1], "rotation": 90}
        scene = Scene(p)
        body = scene.components[-1].body_bbox()
        for (x1, y1), (x2, y2) in scene.wires[0]:
            inside = (max(x1, x2) > body[0] and min(x1, x2) < body[2]
                      and max(y1, y2) > body[1] and min(y1, y2) < body[3])
            self.assertFalse(inside, "wire still runs through the new part")

    def test_clear_routes_routes_everything(self) -> None:
        p = self.chain()
        Scene(p)
        p.clear_routes()
        scene = Scene(p)
        self.assertEqual(scene.rerouted, sum(len(e) for e in scene.net_edges))


class WaypointTests(TmpProjects):
    def test_off_grid_waypoint_is_kept(self) -> None:
        """The editor snaps waypoints to pin lines, which may be off the grid."""
        p = self.project([{"id": "R1", "type": "resistor"}])
        p.set_waypoints("A.1|B.2", [[300, 105], [12.345, 7.0]])
        self.assertEqual(p.layout["wires"]["A.1|B.2"], [[300, 105], [12.3, 7.0]])
        p.set_waypoints("A.1|B.2", [])
        self.assertNotIn("A.1|B.2", p.layout["wires"])


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

    def base(self) -> None:
        mcp_server.tool_write_circuit({"project": "t", "circuit": {
            "components": [{"id": "R1", "type": "resistor"},
                           {"id": "R2", "type": "resistor"},
                           {"id": "GND1", "type": "ground"}],
            "nets": [{"name": "mid", "pins": ["R1.2", "R2.1"]},
                     {"name": "GND", "pins": ["R2.2", "GND1.pin"]}]}})

    def circuit(self) -> dict:
        return json.loads((self.dir / "t.circuit.json").read_text(encoding="utf-8"))

    def test_update_adds_connects_and_removes(self) -> None:
        self.base()
        text = mcp_server.tool_update_circuit({"project": "t", "changes": {
            "upsert_components": [{"id": "C1", "type": "capacitor", "value": "100nF"}],
            "connect": [{"net": "mid", "pins": ["C1.1"]},
                        {"net": "GND", "pins": ["C1.2"]}],
            "remove_components": ["R1"],
        }})
        self.assertIn("Written", text)
        self.assertIn("removed part R1 and its connections R1.2", text)
        c = self.circuit()
        self.assertEqual([x["id"] for x in c["components"]], ["R2", "GND1", "C1"])
        nets = {n["name"]: n["pins"] for n in c["nets"]}
        self.assertEqual(nets, {"mid": ["R2.1", "C1.1"],
                                "GND": ["R2.2", "GND1.pin", "C1.2"]})

    def test_connect_moves_a_pin_instead_of_shorting(self) -> None:
        self.base()
        mcp_server.tool_update_circuit({"project": "t", "changes": {
            "connect": [{"net": "GND", "pins": ["R2.1"]}]}})
        nets = {n["name"]: n["pins"] for n in self.circuit()["nets"]}
        self.assertEqual(nets["mid"], ["R1.2"])
        self.assertIn("R2.1", nets["GND"])

    def test_update_keeps_nc_consistent(self) -> None:
        self.base()
        mcp_server.tool_update_circuit({"project": "t", "changes": {
            "upsert_components": [{"id": "R3", "type": "resistor"}],
            "add_nc": ["R3.1", "R3.2", "R1.1"]}})
        self.assertEqual(self.circuit()["nc"], ["R3.1", "R3.2", "R1.1"])
        text = mcp_server.tool_update_circuit({"project": "t", "changes": {
            "remove_components": ["R3"],
            "connect": [{"net": "GND", "pins": ["R1.1"]}]}})
        self.assertIn("Written", text)       # no dangling nc refs to R3
        self.assertEqual(self.circuit()["nc"], [])

    def test_invalid_update_writes_nothing(self) -> None:
        self.base()
        before = self.circuit()
        text = mcp_server.tool_update_circuit({"project": "t", "changes": {
            "connect": [{"net": "mid", "pins": ["R9.1"]}]}})
        self.assertTrue(text.startswith("NOT written"), text)
        self.assertEqual(self.circuit(), before)

    def test_update_keeps_layout(self) -> None:
        self.base()
        p = Project(self.dir, "t").load()
        p.set_positions({"R1": {"x": 400, "y": 400}})
        p.save_layout()
        mcp_server.tool_update_circuit({"project": "t", "changes": {
            "upsert_components": [{"id": "R1", "type": "resistor", "value": "1k"}]}})
        layout = json.loads((self.dir / "t.layout.json").read_text(encoding="utf-8"))
        self.assertEqual(layout["positions"]["R1"]["x"], 400)

    def test_transport_survives_non_object_message(self) -> None:
        msgs = "[1,2]\n" + json.dumps({"jsonrpc": "2.0", "id": 7, "method": "ping"}) + "\n"
        out = subprocess.run([sys.executable, str(ROOT / "circuitstudio_mcp.py")],
                             input=msgs, capture_output=True, text=True, timeout=30)
        replies = [json.loads(line) for line in out.stdout.splitlines()]
        self.assertEqual(replies[-1], {"jsonrpc": "2.0", "id": 7, "result": {}})


class SingleInstanceTests(TmpProjects):
    def start(self, name):
        from circuitstudio.server import AppState, Handler, _pick_port, _Server
        for n in (name, "other"):
            q = self.project([{"id": "R1", "type": "resistor"}])
            q.name = n
            q.save_circuit()
        state = AppState(self.dir, name)
        port = _pick_port()
        state.port = port

        class H(Handler):      # one handler class per server: state is per class
            pass
        H.state = state
        httpd = _Server(("127.0.0.1", port), H)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        return f"http://127.0.0.1:{port}/"

    def test_running_editor_is_found_and_reused(self) -> None:
        from circuitstudio.server import find_running_editor
        url = self.start("t")
        self.assertEqual(find_running_editor(self.dir, "t"), url)
        self.assertIsNone(find_running_editor(self.dir, "other"))
        self.assertIsNone(find_running_editor(self.dir / "elsewhere", "t"))

        opened = []
        orig_dir, orig_open = mcp_server.PROJECTS_DIR, mcp_server._open_browser
        mcp_server.PROJECTS_DIR, mcp_server._open_browser = self.dir, opened.append
        try:
            text = mcp_server.tool_open_editor({"project": "t"})
        finally:
            mcp_server.PROJECTS_DIR, mcp_server._open_browser = orig_dir, orig_open
        self.assertIn("already running", text)
        self.assertEqual(opened, [url])

        out = subprocess.run([sys.executable, "-m", "circuitstudio", "t", "--no-browser",
                              "--projects-dir", str(self.dir)],
                             cwd=ROOT, capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0)
        self.assertIn("already open", out.stdout)

    def test_switching_to_a_project_open_elsewhere_is_refused(self) -> None:
        self.start("t")
        other = self.start("other")
        req = urllib.request.Request(other + "api/open", data=b'{"name": "t"}',
                                     method="POST",
                                     headers={"Content-Type": "application/json"})
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=30)
        self.assertEqual(ctx.exception.code, 409)


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
