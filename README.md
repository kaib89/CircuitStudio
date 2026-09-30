# CircuitStudio

A schematic editor built around a simple split of labour:
**the language model does the wiring, you do the layout.**

Large language models are good at describing what connects to what. They are
bad at arranging that into a drawing a human enjoys looking at. CircuitStudio
keeps those two jobs in separate files, so neither side overwrites the other.

![CircuitStudio](docs/screenshot.png)

## The idea

| File | Contains | Owned by |
|------|----------|----------|
| `<name>.circuit.json` | components, nets, notes, no-connects | the assistant |
| `<name>.layout.json` | positions, rotation, wires and their waypoints, note boxes | you |
| `<name>.breadboard.json` | optional: which lead goes into which breadboard hole | the assistant writes it, you rearrange it |

The assistant can rewrite the circuit at any time and your arrangement survives:
components are matched by ID, so anything already placed stays where you put it,
and only new parts get an automatic position (marked orange in the editor).
Conversely, dragging things around never touches the electrical description.

The editor watches the circuit file, so changes from the assistant show up live
in the browser.

## Quick start

Requires **Python 3.10 or newer**. No third-party packages.

```
start.bat
```

or

```
python -m circuitstudio [project] [--port 8730] [--no-browser]
```

The browser opens automatically. The console window stays open — closing it
stops the editor. If that project is already open in a running editor, the
existing one is shown instead of starting a second (two editors would keep
overwriting each other's layout); `open_editor` from the assistant behaves the
same way.

## Connecting an assistant (MCP)

CircuitStudio ships an MCP server so an assistant can create and edit circuits
directly. It works on the JSON files, so it does not care whether the editor is
currently running.

**Claude Desktop** — add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "circuitstudio": {
      "command": "python",
      "args": ["/absolute/path/to/CircuitStudio/circuitstudio_mcp.py"]
    }
  }
}
```

**VS Code** — add to `.vscode/mcp.json`:

```json
{
  "servers": {
    "circuitstudio": {
      "type": "stdio",
      "command": "python",
      "args": ["${workspaceFolder}/CircuitStudio/circuitstudio_mcp.py"]
    }
  }
}
```

Tools: `list_projects`, `list_component_types`, `get_circuit`, `write_circuit`,
`update_circuit`, `get_breadboard`, `write_breadboard`, `update_breadboard`,
`open_editor`, `review_project`.

`update_circuit` edits an existing netlist in place — add, replace or remove
parts, nets and notes, connect or disconnect pins, mark pins as no-connect — so
the assistant does not have to resend the whole circuit for a small change.
Removing a part also drops its pins from every net; connecting a pin that
already sits in another net moves it rather than creating a short.

`write_circuit` validates before it writes. Unknown component types, wrong pin
names, bad note anchors, duplicate IDs, duplicate net names and pins that sit in
two nets at once (a short) are rejected with a precise message and nothing is
saved — which gives the model a chance to correct itself instead
of leaving a broken file behind:

```
NOT written — please fix:
- Unknown component type 'widerstand' for 'R1'. Known types: ...
- Net 'GND': 'U1' has no pin 'MASSE'. Available: GND, TRG, OUT, RST, CTL, THR, DIS, VCC
```

On top of that it runs a rule check and reports what is questionable rather than
wrong — a pin nobody wired up, a net name typo that quietly split one node in
two (each half then connects a single pin), a part wired to nothing:

```
1 warning(s):
- R2.2 is not connected to anything. Wire it up, or list it in "nc" to mark it
  as deliberately open.
```

## Auto-arrange

**Auto-arrange** throws away every position except the locked ones and starts
over. It only has to give you something sensible to drag from:

- Ground, supply and label symbols are placed last, each hung directly onto
  one of its net's pins, so it serves that pin (see *Circuit format* for how
  they are wired).
- Power rails do not pull parts together. Otherwise GND, which touches almost
  everything, would pile the whole sheet into one heap.
- Parts can carry a `group` (`"group": "Pitch oscillator"`). Each group is laid
  out as a compact block, and the blocks are then set side by side where their
  connecting wires come out shortest and straightest. For bigger circuits this
  makes the most difference. Without groups the whole circuit is laid out
  as one block.

## Getting the result back to the assistant

The automatic arrangement is not worth showing anyone — it exists so you have
something to drag around, nothing more. So the assistant does not get to see it.

When your layout is ready, press **Hand back ✓**. The browser renders the
schematic to a PNG, and `review_project` then returns that picture together with
the rule check. The assistant sees the drawing you actually made and can check
its own wiring against it.

The button turns green while the picture is current and amber again as soon as
you move something, so it is always clear whether the assistant is looking at
the latest state.

(The rasterising happens in the browser on purpose: Python cannot turn SVG into
a bitmap without a rendering library, and this app has no dependencies. The
browser is already here and is the thing that defines what the drawing looks
like, so the result is WYSIWYG by construction.)

## Circuit format

```json
{
  "title": "ESP32 with status LED and button",
  "components": [
    { "id": "U1", "type": "esp32", "value": "ESP32 DevKit",
      "pins": { "left": ["3V3", "EN"], "right": ["GPIO2", "GPIO4"], "bottom": ["GND"] } },
    { "id": "R1", "type": "resistor", "value": "220Ω" },
    { "id": "D1", "type": "led", "value": "red" },
    { "id": "GND1", "type": "ground" }
  ],
  "nets": [
    { "name": "led_drive", "pins": ["U1.GPIO2", "R1.1"] },
    { "name": "led_anode", "pins": ["R1.2", "D1.anode"] },
    { "name": "GND", "pins": ["U1.GND", "D1.cathode", "GND1.pin"] }
  ],
  "notes": [
    { "id": "N1", "text": "220Ω gives about 6 mA at 3.3 V.", "anchor": "R1" }
  ],
  "nc": ["U1.EN"]
}
```

This is the `esp32_led` project that ships with the repository — open it to see
the result.

Pins are referenced as `ComponentID.PinName`. Call `list_component_types` for
the exact pin names of every symbol.

`nc` lists pins that are meant to stay unconnected. They get the standard
no-connect cross in the drawing and stop being reported as a missing connection
— the difference between "left open on purpose" and "forgotten" is worth saying
out loud. For the reason behind it, anchor a note to the same pin.

Nets named `VCC`, `VDD`, `3V3`, `5V`… are drawn red; `GND`, `VSS`… black and
thicker.

### Net ties: GND, VCC and labels

`ground`, `vcc`, `vdd` and `label` are connections, not parts — the same
convention as KiCad or Eagle. Put several of them into one net and they count
as connected, but no wire is ever drawn between them. Instead every ordinary
pin of the net is wired to the **nearest** such symbol, based on where the
parts currently sit; drag a GND symbol next to other parts and they switch over
to it. A net with just one symbol is wired to it as before.

So instead of one long ground bus, give each part its own GND symbol, and use a
pair of labels (value = net name) instead of a signal wire across the sheet. A
net that has a label does not get its name printed along the wire as well — the
label already says it.

Symbols you have never moved (the orange ones) tidy themselves up: if one
serves no pin, or only pins far away, it is parked right next to the pin that
is furthest from its symbol. Symbols you placed by hand are never moved.

To override the nearest-symbol rule, select the symbol and **Alt+click** the
part that should use it; Alt+click with nothing selected hands the part back.
The rule check warns about a symbol that ends up serving no pin, a label whose
value is not its net's name, and a ground/supply symbol on a net whose name
says otherwise.

ICs, connectors and board symbols (`esp32`, `rpi`, `pico`, `arduino_uno`,
`arduino_nano`) take their pins from the circuit file:

```json
{ "id": "U1", "type": "esp32",
  "pins": { "left": ["3V3", "EN"], "right": ["GPIO2"], "bottom": ["GND"] } }
```

## Breadboard view

Next to the schematic, the editor has a **Breadboard** view: the assistant
writes a plugging plan with `write_breadboard`, and the editor draws it — and
checks it against the netlist. A breadboard is electrically simple (every half
column is one node, every rail another), so the connections the plan *really*
makes can be worked out and compared with the schematic:

- **Short** — two nets meet, e.g. a lead in the GND column of a chip.
- **Open** — the schematic joins pins the board leaves apart.
- **Stray pin** — an unused pin (or one marked no-connect) sits in a live
  column, e.g. the Pico's 3V3_EN next to 3V3.
- **Not plugged in** — parts or leads the plan does not place yet.

A plan that is broken in itself — an unknown hole, two leads in one hole, a
DIP that does not straddle the channel — is not written at all. Problems are
ringed on the board, listed at the bottom right and counted on the
**Breadboard** button. Point at any hole, lead or wire and everything
electrically connected to it lights up, with its net and pins in the status
bar. **Export SVG** writes `<name>.breadboard.svg` while this view is shown.

### Rearranging the board

| Action | How |
|--------|-----|
| Move a part | drag it — it snaps to the holes; the preview is green where it fits and red where a hole is taken or a chip would miss the channel |
| Re-plug one lead | drag the lead (bendable parts, off-board leads) |
| Turn around | `R` — a chip's notch to the other side, a polarised part's leads swapped |
| New wire | drag from a free hole to another |
| Move a wire end | drag the end |
| Remove | select, `Del` — a wire is gone, a part goes back to the tray |
| Place a missing part | drag it from the tray at the top left onto a hole |
| Undo / redo | `Ctrl+Z` / `Ctrl+Y` |
| Pan / zoom | drag on empty space, space or middle-drag / mouse wheel |
| Finish | **Hand back ✓** — a picture of the board for the assistant, separate from the schematic's |

The check runs after every change, so a short shows up the moment it is
plugged. Once you have changed the plan it is yours: `write_breadboard` then
refuses to replace it, and the assistant works on it with `update_breadboard`
(place or move parts, unplug them, add or remove wires) instead. Only if you
agree to start over does it pass `replace=true`. The plan as it was before the
first change of a session, and before any replacement, is kept in
`<name>.breadboard.bak.json`.

```json
{
  "board": { "columns": 63, "split_rails": false },
  "parts": {
    "U1":   { "anchor": "e30", "rotation": 0 },
    "R1":   { "legs": { "1": "d30", "2": "d31" } },
    "ANT1": { "offboard": true, "legs": { "1": "a33" } }
  },
  "wires": [ { "from": "j30", "to": "T+30", "color": "red" } ]
}
```

Holes are named as printed on the board: `a1`…`j63`, and the rails `T+`, `T-`,
`B-`, `B+` plus a column. Parts with fixed pin spacing (DIP/SIP chips, the
555, op-amps, transistors, pots, the Pico) are placed by the hole of pin 1
and a rotation; a DIP with pin 1 in row e and rotation 0 straddles the channel
with its pins running right along e and back along f. Everything else is placed
lead by lead. Parts that live off the board (antennas, speakers) are
`offboard` and drawn below it with their leads. GND/VCC/label symbols are not
parts — their nets reach the rails through wires.

For that, a part needs its physical pin numbers. They are a property of the
part, so they belong in the circuit (the assistant's file), not the layout:

```json
{ "id": "U1", "type": "ic", "value": "74HCU04", "package": "DIP-14",
  "pinout": { "1A": 1, "1Y": 2, "2A": 3, "2Y": 4, "3A": 5, "3Y": 6, "GND": 7,
              "4Y": 8, "4A": 9, "5Y": 10, "5A": 11, "6Y": 12, "6A": 13, "VCC": 14 } }
```

The 555 (DIP-8), single op-amps (standard DIP-8 pinout) and pots come with a
built-in pinout; transistors need one from the datasheet, because EBC and CBE
both exist. The Pico's 40 header pins are built in: name the schematic pins
`GP0`…`GP28`, `GND`, `3V3`, `VSYS`, `VBUS`, `RUN`, `3V3_EN`, `ADC_VREF`,
`AGND`, and all its GND pins count as one. AGND is kept separate — whether it
is joined to GND on the board was not verified.

When the circuit changes, `write_circuit`/`update_circuit` re-check an existing
breadboard plan and report what the change broke; `review_project` includes the
breadboard check as well.

## Editor

| Action | How |
|--------|-----|
| Move | drag a part |
| Select several | drag a frame on empty space, or shift-click |
| Pan / zoom | space or middle-drag / mouse wheel |
| Rotate, mirror, lock | `R` (`Shift+R` the other way), `M`, `L` — several selected parts turn as one group; locked parts also survive **Auto-arrange** |
| Nudge | arrow keys (shift = 5 steps) |
| Undo / redo | `Ctrl+Z` / `Ctrl+Y` or `Ctrl+Shift+Z` |
| Align / distribute | toolbar, relative to the first selected part |
| Guide a wire | click it to add a waypoint, drag the waypoint, double-click to remove |
| Redraw all wires | **Reroute** — forgets the stored wires and routes everything afresh |
| Trace a net | hover a wire — the whole net stays lit and its pins are listed |
| Spot loose ends | pins in no net get a dashed red ring (**Open pins**) |
| Pick a GND/label | select the symbol, **Alt+click** a part to wire it there; Alt+click alone resets |
| Hide net names | untick **Net names** — also leaves them out of the exported SVG |
| Finish | **Hand back ✓** — renders a picture the assistant can review |
| Export | **Export SVG**, written next to the project |

Dragging snaps to the grid, but pin alignment wins over the grid (for parts
and for wire waypoints alike): when a pin
lines up with a pin of another part, a guide appears and it snaps exactly.
That matters because pin pitches differ between symbols — without it, some pins
could never be aligned and wires would zigzag for no reason.

Wires are routed automatically and then stored in the layout. Moving a part
only reroutes the wires attached to it (and any stored wire it now lands on);
everything else stays exactly where it was — which keeps the drawing calm and
large schematics fast.

Junction dots are placed where three or more conductors of the same net meet.
Wires of different nets that merely cross deliberately get no dot.

## Notes

Notes are written by the assistant and live in the circuit file, so explanations
stay with the schematic instead of getting lost in a chat log. They can be
anchored to a component (`"anchor": "R1"`) or to a single pin
(`"anchor": "U1.GPIO25"`); the connection is drawn as a dashed diagonal line,
which can never be mistaken for a wire since wires are solid and orthogonal.

You control position, width and visibility. Hiding a note also removes it from
the exported SVG.

## Project layout

```
circuitstudio/
  symbols.py     component symbols, pins, mirroring
  registry.py    type name -> symbol
  document.py    the two JSON documents, merging, auto-placement
  router.py      orthogonal routing (A*) and junction detection
  erc.py         rule check: open pins, split nodes, dead nets
  footprints.py  physical packages and pin numbers (DIP, SIP, Pico)
  breadboard.py  breadboard plan: parsing and the check against the netlist
  bb_scene.py    breadboard drawing for the editor and the SVG export
  scene.py       geometry; feeds both the editor and the SVG export
  server.py      local HTTP server (127.0.0.1 only)
  mcp_server.py  MCP server for assistants
  web/           the editor UI
projects/        your circuits
```

The editor and the exported SVG are generated from the same geometry, so what
you arrange is exactly what you get.

## Tests

```
python -m unittest discover tests
```

Standard library only, like the rest of the project.

## Credits

The symbol library and the orthogonal router started life in a predecessor
project and were carried over largely unchanged.

## License

Apache License 2.0 — see [LICENSE](LICENSE).
