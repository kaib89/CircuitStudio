"""Parts library: built-in parts, parts the assistant adds, and their use."""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from circuitstudio import library, mcp_server  # noqa: E402
from circuitstudio.breadboard import Breadboard  # noqa: E402

# A made-up part, so the tests do not depend on what is built in.
GATE = {
    "name": "TESTGATE1",
    "aliases": ["TG-1N"],
    "type": "ic",
    "description": "Single inverter for the tests",
    "package": "DIP-6",
    "pins": {"left": ["A", "NC_2"], "right": ["Y", "NC_5"],
             "top": ["VCC"], "bottom": ["GND"]},
    "pinout": {"A": 1, "NC_2": 2, "GND": 3, "Y": 4, "NC_5": 5, "VCC": 6},
    "source": "https://example.com/testgate.pdf — Figure 1, page 2",
}


class UserLibrary(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())
        self._orig = library.USER_DIR
        library.USER_DIR = self.dir / "library"
        library._cache["key"] = None

    def tearDown(self) -> None:
        library.USER_DIR = self._orig
        library._cache["key"] = None
        shutil.rmtree(self.dir, ignore_errors=True)

    def add(self, entry=None, **kw) -> str:
        return mcp_server.tool_add_library_part(
            {"part": json.loads(json.dumps(entry or GATE)), **kw})


class BuiltinTests(unittest.TestCase):
    def test_every_builtin_part_is_consistent_and_sourced(self) -> None:
        entries = [e for e in library.catalog() if e["origin"] == "builtin"]
        self.assertGreater(len(entries), 20)
        for e in entries:
            self.assertEqual(library.validate(e), [], e["name"])
            self.assertIn("http", e["source"], e["name"])

    def test_names_and_aliases_are_unique(self) -> None:
        seen: dict[str, str] = {}
        for path in sorted(library.BUILTIN_DIR.glob("*.json")):
            e = json.loads(path.read_text(encoding="utf-8"))
            for n in [e["name"]] + e.get("aliases", []):
                self.assertNotIn(library.norm(n), seen, f"{n} in {path.name}")
                seen[library.norm(n)] = path.name


class AddPartTests(UserLibrary):
    def test_add_then_use_in_a_circuit_and_on_the_board(self) -> None:
        text = self.add()
        self.assertTrue(text.startswith("Added TESTGATE1"), text)
        self.assertTrue((self.dir / "library" / "TESTGATE1.json").exists())
        circuit = {"components": [{"id": "U1", "part": "tg-1n"},
                                  {"id": "R1", "type": "resistor"}],
                   "nets": [{"name": "in", "pins": ["U1.A", "R1.1"]}]}
        self.assertEqual(mcp_server._validate(circuit), [])
        # The breadboard gets the pinout from the library: pin 1 (A) in e10.
        bb = Breadboard({"parts": {"U1": {"anchor": "e10"},
                                   "R1": {"legs": {"1": "a10", "2": "a15"}}}}, circuit)
        self.assertEqual(bb.structure_errors, [])
        self.assertEqual([f["kind"] for f in bb.findings], [])
        self.assertIn("TESTGATE1", mcp_server.tool_list_library({"query": "inverter"}))

    def test_source_is_required(self) -> None:
        text = self.add({**GATE, "source": ""})
        self.assertTrue(text.startswith("NOT added"), text)
        self.assertIn("'source' is required", text)
        self.assertIn("How to add a part", text)      # the guide comes along

    def test_every_pin_needs_a_number(self) -> None:
        pinout = dict(GATE["pinout"])
        del pinout["NC_5"]
        text = self.add({**GATE, "pinout": pinout})
        self.assertIn("NOT added", text)
        self.assertIn("NC_5", text)

    def test_unnamed_package_pins_are_refused(self) -> None:
        text = self.add({**GATE, "package": "DIP-8"})
        self.assertIn("pins 7, 8", text)

    def test_duplicate_numbers_are_refused(self) -> None:
        text = self.add({**GATE, "pinout": {**GATE["pinout"], "NC_5": 4}})
        self.assertIn("both given pin number 4", text)

    def test_existing_part_needs_replace(self) -> None:
        self.add()
        self.assertIn("replace=true", self.add())
        self.assertTrue(self.add({**GATE, "description": "v2"},
                                 replace=True).startswith("Added"))
        self.assertEqual(library.find("TESTGATE1")["description"], "v2")

    def test_builtin_parts_cannot_be_shadowed(self) -> None:
        builtin = next(e for e in library.catalog() if e["origin"] == "builtin")
        text = self.add({**GATE, "name": builtin["name"]})
        self.assertIn("built-in part", text)

    def test_transistor_has_fixed_pin_names(self) -> None:
        q = {"name": "TESTQ1", "type": "npn", "package": "TO-92",
             "pinout": {"emitter": 1, "base": 2, "collector": 3},
             "source": "https://example.com/q.pdf — Fig. 3"}
        self.assertTrue(self.add(q).startswith("Added"))
        self.assertIn("fixed pin names", self.add({**q, "name": "TESTQ2",
                                                   "pins": {"left": ["B"]}}))


class UseTests(UserLibrary):
    def test_unknown_part_and_wrong_type(self) -> None:
        errs = mcp_server._validate({"components": [{"id": "U1", "part": "NOPE123"}],
                                     "nets": []})
        self.assertTrue(any("not in the library" in e for e in errs), errs)
        self.add()
        errs = mcp_server._validate({"components": [
            {"id": "U1", "type": "npn", "part": "TESTGATE1"}], "nets": []})
        self.assertTrue(any("is a 'ic'" in e for e in errs), errs)

    def test_explicit_fields_win(self) -> None:
        self.add()
        spec = library.expand({"id": "U1", "part": "TESTGATE1", "value": "mine"})
        self.assertEqual(spec["value"], "mine")
        self.assertEqual(spec["package"], "DIP-6")
        self.assertEqual(library.expand({"id": "U1", "part": "TESTGATE1"})["value"],
                         "TESTGATE1")


if __name__ == "__main__":
    unittest.main()
