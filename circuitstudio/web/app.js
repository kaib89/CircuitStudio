'use strict';

const NS = 'http://www.w3.org/2000/svg';

const canvas = document.getElementById('canvas');
const gridRect = document.getElementById('gridRect');
const gridPattern = document.getElementById('gridPattern');
const wiresG = document.getElementById('wires');
const wireHitsG = document.getElementById('wireHits');
const juncG = document.getElementById('junctions');
const labelsG = document.getElementById('netLabels');
const wpG = document.getElementById('waypoints');
const compsG = document.getElementById('components');
const notesG = document.getElementById('notes');
const chkNotes = document.getElementById('chkNotes');
const netPinsG = document.getElementById('netPins');
const guidesG = document.getElementById('guides');
const bandEl = document.getElementById('band');
const gridRectEl = document.getElementById('gridRect');
const chkGrid = document.getElementById('chkGrid');
const errorsBox = document.getElementById('errors');
const statusEl = document.getElementById('status');
const projectSelect = document.getElementById('projectSelect');
const gridSelect = document.getElementById('gridSelect');
const btnAlignX = document.getElementById('btnAlignX');
const btnAlignY = document.getElementById('btnAlignY');
const btnDistX = document.getElementById('btnDistX');
const btnDistY = document.getElementById('btnDistY');

let scene = null;
let view = { x: 0, y: 0, w: 1000, h: 700 };
let grid = 20;
let lastVersion = -1;
let selected = new Set();
let drag = null;
let wpDrag = null;
let wireClick = null;
let pan = null;
let band = null;
let noteDrag = null;
let spaceDown = false;
let viewSaveTimer = null;
let lastWpClick = null;
let hoverNet = null;
const undoStack = [];
const redoStack = [];
const HISTORY_MAX = 50;
const DBLCLICK_MS = 700;
const SNAP_PX = 9;          // pin-alignment catch radius, in screen pixels

// ── helpers ────────────────────────────────────────────────────────────────

function escapeHtml(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;')
                  .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function snap(v) { return Math.round(v / grid) * grid; }

// The SVG uses the default preserveAspectRatio (xMidYMid meet), so the viewBox
// is scaled uniformly and centred. Doing the mapping explicitly (instead of via
// getScreenCTM) keeps drag maths stable even while the view is changing.
function viewMetrics() {
  const rect = canvas.getBoundingClientRect();
  const upp = Math.max(view.w / rect.width, view.h / rect.height); // user units / px
  return {
    rect, upp,
    offX: (rect.width - view.w / upp) / 2,
    offY: (rect.height - view.h / upp) / 2,
  };
}

function toUser(evt) {
  const m = viewMetrics();
  return {
    x: view.x + (evt.clientX - m.rect.left - m.offX) * m.upp,
    y: view.y + (evt.clientY - m.rect.top - m.offY) * m.upp,
  };
}

async function api(path, body) {
  const opts = body === undefined
    ? {}
    : { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body) };
  const res = await fetch(path, opts);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || res.statusText);
  return data;
}

function setStatus(msg) {
  statusEl.textContent = msg;
  statusEl.title = msg;   // the line is clipped, so keep the full text reachable
}

// ── alignment guides ─────────────────────────────────────────────────

function showGuides(gx, gy) {
  let html = '';
  if (gx !== null) {
    html += `<line class="guide" x1="${gx}" y1="${view.y}" ` +
            `x2="${gx}" y2="${view.y + view.h}"/>`;
  }
  if (gy !== null) {
    html += `<line class="guide" x1="${view.x}" y1="${gy}" ` +
            `x2="${view.x + view.w}" y2="${gy}"/>`;
  }
  guidesG.innerHTML = html;
}

function clearGuides() { guidesG.innerHTML = ''; }

function showBand() {
  if (!band) { bandEl.style.display = 'none'; return; }
  bandEl.style.display = '';
  bandEl.setAttribute('x', Math.min(band.x0, band.x1));
  bandEl.setAttribute('y', Math.min(band.y0, band.y1));
  bandEl.setAttribute('width', Math.abs(band.x1 - band.x0));
  bandEl.setAttribute('height', Math.abs(band.y1 - band.y0));
}

/** Nudge the drag so a moved pin lines up exactly with a stationary pin.
 *  Returns the correction plus the guide lines to draw. */
function pinSnap(dx, dy) {
  let corrX = 0, corrY = 0, gx = null, gy = null;
  let bestX = Infinity, bestY = Infinity;

  for (const [id, o] of Object.entries(drag.orig)) {
    const offsets = drag.pinOffsets[id];
    if (!offsets) continue;
    const bx = snap(o.x + dx);
    const by = snap(o.y + dy);
    for (const off of offsets) {
      const px = bx + off.dx;
      const py = by + off.dy;
      for (const sx of drag.staticXs) {
        const d = sx - px;
        if (Math.abs(d) <= drag.tol && Math.abs(d) < Math.abs(bestX)) {
          bestX = d; gx = sx;
        }
      }
      for (const sy of drag.staticYs) {
        const d = sy - py;
        if (Math.abs(d) <= drag.tol && Math.abs(d) < Math.abs(bestY)) {
          bestY = d; gy = sy;
        }
      }
    }
  }
  if (Number.isFinite(bestX)) corrX = bestX; else gx = null;
  if (Number.isFinite(bestY)) corrY = bestY; else gy = null;
  return { corrX, corrY, gx, gy };
}

// ── view ───────────────────────────────────────────────────────────────────

function applyView() {
  canvas.setAttribute('viewBox', `${view.x} ${view.y} ${view.w} ${view.h}`);
  gridRect.setAttribute('x', view.x);
  gridRect.setAttribute('y', view.y);
  gridRect.setAttribute('width', view.w);
  gridRect.setAttribute('height', view.h);
}

function scheduleViewSave() {
  clearTimeout(viewSaveTimer);
  viewSaveTimer = setTimeout(() => {
    // Fire and forget: applying the response would re-render the canvas, and if
    // that lands mid-drag the component visibly jumps back to its stored spot.
    api('/api/layout', { view: view }).catch(() => {});
  }, 900);
}

function fitView() {
  if (!scene) return;
  const [x, y, w, h] = scene.viewBox;
  const rect = canvas.getBoundingClientRect();
  const aspect = rect.width / Math.max(rect.height, 1);
  let vw = w, vh = h;
  if (w / h > aspect) vh = w / aspect; else vw = h * aspect;
  view = { x: x - (vw - w) / 2, y: y - (vh - h) / 2, w: vw, h: vh };
  applyView();
  scheduleViewSave();
}

// ── rendering ──────────────────────────────────────────────────────────────

function defaultStatus() {
  if (!scene) return '';
  const autoCount = scene.components.filter(c => c.auto).length;
  return `${scene.components.length} parts · ${scene.wires.length} nets` +
         (autoCount ? ` · ${autoCount} not arranged yet` : '') +
         (selected.size ? ` · ${selected.size} selected` : '');
}

/** Fade everything except one net, mark its pins, and list them in the status
 *  bar — the quickest way to check whether the wiring is really what you asked
 *  the model for. */
function setNetFocus(name) {
  if (hoverNet === name) return;
  hoverNet = name;

  if (!name) {
    canvas.classList.remove('netfocus');
    netPinsG.innerHTML = '';
    for (const l of wiresG.querySelectorAll('line.hl')) l.classList.remove('hl');
    for (const g of compsG.querySelectorAll('g.comp.netmember')) {
      g.classList.remove('netmember');
    }
    setStatus(defaultStatus());
    return;
  }

  const wire = scene.wires.find(w => w.net === name);
  if (!wire) return;

  const members = new Set();
  const marks = [];
  for (const ref of wire.pins || []) {
    // Split on the first dot only — pin names such as "3.3V" contain dots.
    const s = String(ref);
    const dot = s.indexOf('.');
    const cid = dot < 0 ? s : s.slice(0, dot);
    const pinName = dot < 0 ? '' : s.slice(dot + 1);
    members.add(cid);
    const comp = scene.components.find(c => c.id === cid);
    const pin = comp && comp.pins.find(p => p.name === pinName);
    if (pin) marks.push(`<circle cx="${pin.x}" cy="${pin.y}" r="5"/>`);
  }

  canvas.classList.add('netfocus');
  netPinsG.innerHTML = marks.join('');
  for (const l of wiresG.querySelectorAll('line')) {
    l.classList.toggle('hl', l.dataset.net === name);
  }
  for (const g of compsG.querySelectorAll('g.comp')) {
    g.classList.toggle('netmember', members.has(g.dataset.id));
  }

  const pins = wire.pins || [];
  const shown = pins.slice(0, 8).join(', ');
  setStatus(`Net "${name}" · ${pins.length} pins: ${shown}` +
            (pins.length > 8 ? ` … (+${pins.length - 8} more)` : ''));
}

async function postNote(patch) {
  try {
    applyPayload(await api('/api/note', patch));
  } catch (err) { setStatus('Error: ' + err.message); }
}

/** Notes are drawn from the same geometry the export uses; only the little
 *  handles are editor-only. */
function renderNotes() {
  if (!scene) return;
  if (!chkNotes.checked) { notesG.innerHTML = ''; return; }
  let html = '';
  for (const n of scene.notes || []) {
    const id = escapeHtml(n.id);
    if (n.hidden) {
      html += `<g class="note-ghost" data-id="${id}">` +
              `<circle cx="${n.x + 10}" cy="${n.y + 10}" r="9"/>` +
              `<text x="${n.x + 10}" y="${n.y + 14}" text-anchor="middle" ` +
              `font-family="sans-serif" font-size="11" fill="#8a6d1e">i</text>` +
              `<title>Show note</title></g>`;
      continue;
    }
    let body = '';
    if (n.leader) {
      const [[sx, sy], [ax, ay]] = n.leader;
      body += `<line x1="${sx}" y1="${sy}" x2="${ax}" y2="${ay}" ` +
              `stroke="#B08A3E" stroke-width="1.2" stroke-dasharray="5 4"/>` +
              `<circle cx="${ax}" cy="${ay}" r="3" fill="#B08A3E"/>`;
    }
    body += `<rect x="${n.x}" y="${n.y}" width="${n.w}" height="${n.h}" rx="5" ` +
            `fill="#FFF9E3" stroke="#C8A951" stroke-width="1.2"/>`;
    const tx = n.x + 8;
    const spans = n.lines.map((l, i) =>
      `<tspan x="${tx}" dy="${i === 0 ? 0 : 15}">${escapeHtml(l)}</tspan>`).join('');
    body += `<text x="${tx}" y="${n.y + 8 + 11}" font-family="sans-serif" ` +
            `font-size="11" fill="#4A3F1E">${spans}</text>`;
    // editor-only handles
    body += `<circle class="note-toggle" cx="${n.x + n.w - 9}" cy="${n.y + 9}" r="6">` +
            `<title>Hide note</title></circle>` +
            `<line class="note-toggle-glyph" x1="${n.x + n.w - 12}" ` +
            `y1="${n.y + 9}" x2="${n.x + n.w - 6}" y2="${n.y + 9}" ` +
            `stroke="#8a6d1e" stroke-width="1.4"/>`;
    body += `<rect class="note-resize" x="${n.x + n.w - 5}" ` +
            `y="${n.y + n.h - 16}" width="5" height="16" rx="2">` +
            `<title>Resize width</title></rect>`;
    html += `<g class="note" data-id="${id}">${body}</g>`;
  }
  notesG.innerHTML = html;
}

function render() {
  if (!scene) return;

  // The DOM is rebuilt below, so any highlight classes are gone; forget the
  // focus state too, otherwise the next hover would be treated as unchanged.
  hoverNet = null;
  canvas.classList.remove('netfocus');
  netPinsG.innerHTML = '';

  gridPattern.setAttribute('width', grid);
  gridPattern.setAttribute('height', grid);

  let html = '';
  let hits = '';
  let handles = '';
  for (const w of scene.wires) {
    const netAttr = escapeHtml(w.net);
    for (const e of w.edges) {
      const ekey = escapeHtml(e.key);
      e.legs.forEach((leg, li) => {
        for (const [[x1, y1], [x2, y2]] of leg) {
          html += `<line data-net="${netAttr}" x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}" ` +
                  `stroke="${w.color}" stroke-width="${w.width}" stroke-linecap="round"/>`;
          hits += `<line class="wirehit" data-edge="${ekey}" data-leg="${li}" ` +
                  `data-net="${netAttr}" ` +
                  `x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}"><title>` +
                  `${netAttr} — click to add a waypoint</title></line>`;
        }
      });
      e.waypoints.forEach((p, i) => {
        handles += `<circle class="wp" data-edge="${ekey}" data-index="${i}" ` +
                   `cx="${p[0]}" cy="${p[1]}" r="5"><title>` +
                   `Drag to steer the wire, double-click to remove</title></circle>`;
      });
    }
  }
  wiresG.innerHTML = html;
  wireHitsG.innerHTML = hits;
  wpG.innerHTML = handles;

  juncG.innerHTML = scene.junctions
    .map(([x, y]) => `<circle cx="${x}" cy="${y}" r="4" fill="#000"/>`).join('');

  labelsG.innerHTML = scene.netLabels.map(l =>
    `<rect x="${l.bg.x}" y="${l.bg.y}" width="${l.bg.w}" height="${l.bg.h}" ` +
    `fill="#fff" fill-opacity="0.92"/>` +
    `<text x="${l.x}" y="${l.y}" text-anchor="middle" font-family="sans-serif" ` +
    `font-size="9" font-style="italic" fill="${l.color}">${escapeHtml(l.text)}</text>`
  ).join('');

  compsG.textContent = '';
  for (const c of scene.components) {
    const g = document.createElementNS(NS, 'g');
    g.setAttribute('class', 'comp' + (c.auto ? ' auto' : '') +
                            (c.locked ? ' locked' : '') +
                            (selected.has(c.id) ? ' selected' : ''));
    g.dataset.id = c.id;
    g.setAttribute('transform', `translate(${c.x},${c.y}) rotate(${c.rotation})`);
    const [hx, hy, hw, hh] = c.hit;
    g.innerHTML =
      `<rect class="hit" x="${hx}" y="${hy}" width="${hw}" height="${hh}"/>` + c.svg;
    compsG.appendChild(g);
  }

  if (scene.errors && scene.errors.length) {
    errorsBox.hidden = false;
    errorsBox.textContent = scene.errors.join('\n');
  } else {
    errorsBox.hidden = true;
  }

  setStatus(defaultStatus());

  renderNotes();

  btnAlignX.disabled = btnAlignY.disabled = selectedComponents().length < 2;
  btnDistX.disabled = btnDistY.disabled = selectedComponents().length < 3;
}

function applyPayload(payload) {
  scene = payload.scene;
  lastVersion = payload.version;
  grid = scene.grid || 20;
  gridSelect.value = String(grid);
  chkGrid.checked = payload.showGrid !== false;
  gridRectEl.style.display = chkGrid.checked ? '' : 'none';
  document.title = `${scene.title} — CircuitStudio`;

  const names = payload.projects.length ? payload.projects : [payload.project];
  projectSelect.innerHTML = names
    .map(n => `<option value="${escapeHtml(n)}">${escapeHtml(n)}</option>`).join('');
  projectSelect.value = payload.project;

  const ids = new Set(scene.components.map(c => c.id));
  selected = new Set([...selected].filter(id => ids.has(id)));

  render();
  return payload;
}

// ── loading ────────────────────────────────────────────────────────────────

async function loadState(resetView) {
  const payload = await api('/api/state');
  applyPayload(payload);
  if (resetView) {
    if (payload.view && payload.view.w > 0) {
      view = payload.view;
      applyView();
    } else {
      fitView();
    }
  }
}

// ── interaction ────────────────────────────────────────────────────────────

function compAt(evt) {
  const el = evt.target.closest ? evt.target.closest('g.comp') : null;
  return el && compsG.contains(el) ? el : null;
}

function findEdge(key) {
  for (const w of scene.wires) {
    for (const e of w.edges) if (e.key === key) return e;
  }
  return null;
}

async function postWaypoints(edge, points) {
  try {
    applyPayload(await api('/api/waypoints', { edge, points }));
  } catch (err) { setStatus('Error: ' + err.message); }
}

// ── undo ──────────────────────────────────────────────────────────────────

function snapshot() {
  if (!scene) return null;
  const positions = {};
  for (const c of scene.components) {
    positions[c.id] = { x: c.x, y: c.y, rotation: c.rotation, flip: c.flip,
                        locked: c.locked };
  }
  const wires = {};
  for (const w of scene.wires) {
    for (const e of w.edges) {
      if (e.waypoints.length) wires[e.key] = e.waypoints.map(p => [p[0], p[1]]);
    }
  }
  return { positions, wires };
}

/** Remember the state *before* a change so Ctrl+Z can get back to it. */
function pushHistory() {
  const s = snapshot();
  if (!s) return;
  undoStack.push(s);
  if (undoStack.length > HISTORY_MAX) undoStack.shift();
  redoStack.length = 0;   // a new change starts a new branch of history
}

/** Step through history: restore one stack's top, park the present on the other. */
async function travel(from, to, verb) {
  const s = from.pop();
  if (!s) { setStatus(`Nothing to ${verb}.`); return; }
  const now = snapshot();
  try {
    applyPayload(await api('/api/restore', s));
    if (now) to.push(now);
    setStatus(`${verb === 'undo' ? 'Undone' : 'Redone'} — ` +
              `${undoStack.length} undo / ${redoStack.length} redo step(s) left.`);
  } catch (err) {
    from.push(s);
    setStatus('Error: ' + err.message);
  }
}

const undo = () => travel(undoStack, redoStack, 'undo');
const redo = () => travel(redoStack, undoStack, 'redo');

/** Rotate or mirror the selection as one rigid group.
 *  A single part turns in place; several parts also swing around their
 *  common centre, so wiring that was lined up stays lined up. The centre is
 *  snapped to the grid, which keeps grid-placed parts on the grid. */
async function transformSelection(kind) {
  const comps = selectedComponents();
  if (!comps.length) return;
  let cx = 0, cy = 0;
  if (comps.length > 1) {
    const xs = comps.map(c => c.x), ys = comps.map(c => c.y);
    cx = snap((Math.min(...xs) + Math.max(...xs)) / 2);
    cy = snap((Math.min(...ys) + Math.max(...ys)) / 2);
  }
  const updates = {};
  for (const c of comps) {
    const dx = c.x - cx, dy = c.y - cy;
    let x = c.x, y = c.y, rotation = c.rotation, flip = c.flip;
    if (kind === 'cw') {             // SVG y points down: clockwise is (-dy, dx)
      if (comps.length > 1) { x = cx - dy; y = cy + dx; }
      rotation = (c.rotation + 90) % 360;
    } else if (kind === 'ccw') {
      if (comps.length > 1) { x = cx + dy; y = cy - dx; }
      rotation = (c.rotation + 270) % 360;
    } else {                         // mirror left-right on screen
      if (comps.length > 1) x = cx - dx;
      // Flip is applied before rotation, so a screen mirror of a rotated
      // part is: toggle the flip and run the rotation the other way.
      flip = !c.flip;
      rotation = (360 - c.rotation) % 360;
    }
    updates[c.id] = { x, y, rotation, flip };
  }
  await applyPositions(updates);
}

canvas.addEventListener('pointerdown', evt => {
  if (evt.button !== 0 && evt.button !== 1) return;
  setNetFocus(null);
  const target = evt.button === 0 ? evt.target : null;

  // ── notes ───────────────────────────────────────────────────────────────
  if (target && !spaceDown) {
    const ghost = target.closest && target.closest('g.note-ghost');
    if (ghost) {
      postNote({ id: ghost.dataset.id, hidden: false });
      return;
    }
    if (target.classList && target.classList.contains('note-toggle')) {
      const g = target.closest('g.note');
      if (g) { postNote({ id: g.dataset.id, hidden: true }); return; }
    }
    const noteEl = target.closest && target.closest('g.note');
    if (noteEl) {
      const n = (scene.notes || []).find(k => k.id === noteEl.dataset.id);
      if (n) {
        noteDrag = {
          id: n.id, sx: evt.clientX, sy: evt.clientY, upp: viewMetrics().upp,
          x: n.x, y: n.y, w: n.w, moved: false,
          mode: target.classList.contains('note-resize') ? 'resize' : 'move',
          el: noteEl,
        };
        try { canvas.setPointerCapture(evt.pointerId); } catch (e) { /* ignore */ }
        return;
      }
    }
  }

  const wpEl = target && target.classList && target.classList.contains('wp')
    ? target : null;
  const wireEl = target && target.classList && target.classList.contains('wirehit')
    ? target : null;
  const compEl = wpEl || wireEl ? null : (evt.button === 0 ? compAt(evt) : null);

  if (wpEl) {
    const edge = findEdge(wpEl.dataset.edge);
    const index = parseInt(wpEl.dataset.index, 10);
    // Detect the double-click here rather than via the dblclick event: the
    // canvas captures the pointer, which retargets click/dblclick to the canvas.
    const now = Date.now();
    if (lastWpClick && lastWpClick.key === wpEl.dataset.edge &&
        lastWpClick.index === index && now - lastWpClick.t < DBLCLICK_MS) {
      lastWpClick = null;
      if (edge) {
        pushHistory();
        const points = edge.waypoints
          .filter((_, i) => i !== index).map(p => [p[0], p[1]]);
        postWaypoints(wpEl.dataset.edge, points);
      }
      return;
    }
    lastWpClick = { key: wpEl.dataset.edge, index, t: now };
    if (edge) {
      wpDrag = {
        key: wpEl.dataset.edge,
        index,
        points: edge.waypoints.map(p => [p[0], p[1]]),
        sx: evt.clientX, sy: evt.clientY, upp: viewMetrics().upp,
        el: wpEl, moved: false,
      };
      wpEl.classList.add('dragging');
    }
  } else if (wireEl) {
    // Decide on pointerup: a click adds a waypoint, a drag would be a pan.
    wireClick = {
      key: wireEl.dataset.edge,
      leg: parseInt(wireEl.dataset.leg, 10),
      sx: evt.clientX, sy: evt.clientY,
    };
  } else if (compEl) {
    const id = compEl.dataset.id;
    if (evt.shiftKey) {
      selected.has(id) ? selected.delete(id) : selected.add(id);
    } else if (!selected.has(id)) {
      selected = new Set([id]);
    }
    render();

    const orig = {};
    for (const c of scene.components) {
      if (selected.has(c.id) && !c.locked) {
        orig[c.id] = { x: c.x, y: c.y, rotation: c.rotation, flip: c.flip };
      }
    }
    // Freeze the scale at drag start so the cursor and the symbol stay locked.
    drag = { sx: evt.clientX, sy: evt.clientY, upp: viewMetrics().upp,
             orig, moved: false, last: {},
             pinOffsets: {}, staticXs: [], staticYs: [] };
    // Grid snapping alone can leave a pin up to half a grid step off target,
    // so the catch radius has to be wider than that or alignment could never
    // be reached. Also scales with zoom so it feels the same when zoomed out.
    drag.tol = Math.max(SNAP_PX * drag.upp, grid * 0.75);

    // Pins that move with the drag vs. pins that stay put.
    const staticXs = new Set();
    const staticYs = new Set();
    for (const c of scene.components) {
      if (selected.has(c.id)) {
        drag.pinOffsets[c.id] = c.pins.map(p => ({ dx: p.x - c.x, dy: p.y - c.y }));
      } else {
        for (const p of c.pins) { staticXs.add(p.x); staticYs.add(p.y); }
      }
    }
    drag.staticXs = [...staticXs];
    drag.staticYs = [...staticYs];
  } else {
    if (evt.button === 1 || spaceDown) {
      evt.preventDefault();   // stop the middle-click autoscroll cursor
      pan = { sx: evt.clientX, sy: evt.clientY, upp: viewMetrics().upp,
              view: { ...view } };
      canvas.classList.add('panning');
    } else {
      if (!evt.shiftKey) { selected = new Set(); render(); }
      const p = toUser(evt);
      band = { x0: p.x, y0: p.y, x1: p.x, y1: p.y };
      showBand();
    }
  }
  try { canvas.setPointerCapture(evt.pointerId); } catch (e) { /* no active pointer */ }
});

canvas.addEventListener('pointermove', evt => {
  if (noteDrag) {
    const dx = (evt.clientX - noteDrag.sx) * noteDrag.upp;
    const dy = (evt.clientY - noteDrag.sy) * noteDrag.upp;
    if (Math.abs(dx) > 1 || Math.abs(dy) > 1) noteDrag.moved = true;
    if (noteDrag.mode === 'resize') {
      noteDrag.newW = Math.max(90, snap(noteDrag.w + dx));
      noteDrag.el.querySelector('rect').setAttribute('width', noteDrag.newW);
    } else {
      noteDrag.newX = snap(noteDrag.x + dx);
      noteDrag.newY = snap(noteDrag.y + dy);
      noteDrag.el.setAttribute('transform',
        `translate(${noteDrag.newX - noteDrag.x},${noteDrag.newY - noteDrag.y})`);
    }
    return;
  }
  if (wpDrag) {
    const dx = (evt.clientX - wpDrag.sx) * wpDrag.upp;
    const dy = (evt.clientY - wpDrag.sy) * wpDrag.upp;
    if (Math.abs(dx) > 1 || Math.abs(dy) > 1) wpDrag.moved = true;
    const o = wpDrag.points[wpDrag.index];
    const nx = snap(o[0] + dx);
    const ny = snap(o[1] + dy);
    wpDrag.el.setAttribute('cx', nx);
    wpDrag.el.setAttribute('cy', ny);
    wpDrag.target = [nx, ny];
  } else if (drag) {
    const dx = (evt.clientX - drag.sx) * drag.upp;
    const dy = (evt.clientY - drag.sy) * drag.upp;
    if (Math.abs(dx) > 1 || Math.abs(dy) > 1) drag.moved = true;
    const { corrX, corrY, gx, gy } = pinSnap(dx, dy);
    showGuides(gx, gy);
    for (const [id, o] of Object.entries(drag.orig)) {
      const pos = { x: snap(o.x + dx) + corrX, y: snap(o.y + dy) + corrY,
                    rotation: o.rotation, flip: o.flip };
      drag.last[id] = pos;  // save exactly what is on screen
      const g = compsG.querySelector(`g.comp[data-id="${CSS.escape(id)}"]`);
      if (g) {
        g.classList.add('dragging');
        g.setAttribute('transform',
          `translate(${pos.x},${pos.y}) rotate(${pos.rotation})`);
      }
    }
  } else if (wireClick) {
    if (Math.abs(evt.clientX - wireClick.sx) > 3 ||
        Math.abs(evt.clientY - wireClick.sy) > 3) {
      wireClick = null;  // turned into a drag, not a click
    }
  } else if (pan) {
    view.x = pan.view.x - (evt.clientX - pan.sx) * pan.upp;
    view.y = pan.view.y - (evt.clientY - pan.sy) * pan.upp;
    applyView();
  } else if (band) {
    const p = toUser(evt);
    band.x1 = p.x;
    band.y1 = p.y;
    showBand();
  } else {
    const t = evt.target;
    setNetFocus(t && t.dataset ? (t.dataset.net || null) : null);
  }
});

canvas.addEventListener('pointerleave', () => setNetFocus(null));

canvas.addEventListener('pointerup', async evt => {
  canvas.classList.remove('panning');
  if (canvas.hasPointerCapture(evt.pointerId)) canvas.releasePointerCapture(evt.pointerId);

  if (noteDrag) {
    const d = noteDrag;
    noteDrag = null;
    if (!d.moved) { render(); return; }
    if (d.mode === 'resize') {
      await postNote({ id: d.id, w: d.newW });
    } else {
      await postNote({ id: d.id, x: d.newX, y: d.newY });
    }
    return;
  }

  if (wpDrag) {
    const d = wpDrag;
    wpDrag = null;
    if (d.moved && d.target) {
      pushHistory();
      const points = d.points.map(p => [p[0], p[1]]);
      points[d.index] = d.target;
      await postWaypoints(d.key, points);
    } else {
      // Nothing changed — skip the re-render so the second click of a
      // double-click is not delayed by a full rebuild of the canvas.
      d.el.classList.remove('dragging');
    }
    return;
  }

  if (wireClick) {
    const c = wireClick;
    wireClick = null;
    const edge = findEdge(c.key);
    if (edge) {
      pushHistory();
      const p = toUser(evt);
      const points = edge.waypoints.map(q => [q[0], q[1]]);
      points.splice(c.leg, 0, [snap(p.x), snap(p.y)]);
      await postWaypoints(c.key, points);
    }
    return;
  }

  if (drag) {
    const d = drag;
    drag = null;
    clearGuides();
    if (d.moved && Object.keys(d.last).length) {
      pushHistory();
      try {
        applyPayload(await api('/api/layout', { positions: d.last }));
      } catch (err) { setStatus('Error: ' + err.message); }
    } else {
      render();
    }
  }
  if (band) {
    const x0 = Math.min(band.x0, band.x1), x1 = Math.max(band.x0, band.x1);
    const y0 = Math.min(band.y0, band.y1), y1 = Math.max(band.y0, band.y1);
    band = null;
    showBand();
    if (x1 - x0 > 3 || y1 - y0 > 3) {
      // A component counts as picked when its centre is inside the frame.
      for (const c of scene.components) {
        if (c.x >= x0 && c.x <= x1 && c.y >= y0 && c.y <= y1) selected.add(c.id);
      }
      render();
    }
    return;
  }

  if (pan) { pan = null; scheduleViewSave(); }
});

canvas.addEventListener('wheel', evt => {
  evt.preventDefault();
  const p = toUser(evt);
  const factor = evt.deltaY > 0 ? 1.12 : 1 / 1.12;
  view = {
    x: p.x - (p.x - view.x) * factor,
    y: p.y - (p.y - view.y) * factor,
    w: view.w * factor,
    h: view.h * factor,
  };
  applyView();
  scheduleViewSave();
}, { passive: false });

document.addEventListener('keydown', async evt => {
  if (evt.target.tagName === 'SELECT' || evt.target.tagName === 'INPUT') return;
  if (evt.code === 'Space') {
    spaceDown = true;
    evt.preventDefault();
    return;
  }
  const key = evt.key.toLowerCase();
  if (evt.ctrlKey || evt.metaKey) {
    if (key === 'z' && evt.shiftKey || key === 'y') {
      evt.preventDefault();
      await redo();
    } else if (key === 'z') {
      evt.preventDefault();
      await undo();
    }
    return;   // leave Ctrl+R, Ctrl+F, … to the browser
  }
  if (key === 'r' && selected.size) {
    await transformSelection(evt.shiftKey ? 'ccw' : 'cw');
  } else if (key === 'm' && selected.size) {
    await transformSelection('mirror');
  } else if (evt.key.toLowerCase() === 'l' && selected.size) {
    // Lock state is a property of the selection as a whole: if anything in it is
    // still unlocked, lock everything; otherwise unlock everything.
    const picked = scene.components.filter(c => selected.has(c.id));
    const lock = picked.some(c => !c.locked);
    const updates = {};
    for (const c of picked) {
      updates[c.id] = { x: c.x, y: c.y, rotation: c.rotation, flip: c.flip,
                        locked: lock };
    }
    pushHistory();
    try {
      applyPayload(await api('/api/layout', { positions: updates }));
      setStatus(`${picked.length} part(s) ${lock ? 'locked' : 'unlocked'}.`);
    } catch (err) { setStatus('Error: ' + err.message); }
  } else if (evt.key === 'Escape') {
    selected = new Set();
    render();
  } else if (evt.key.toLowerCase() === 'f') {
    fitView();
  } else if (evt.key.startsWith('Arrow') && selected.size) {
    evt.preventDefault();
    const step = evt.shiftKey ? grid * 5 : grid;
    const dx = (evt.key === 'ArrowLeft' ? -step : evt.key === 'ArrowRight' ? step : 0);
    const dy = (evt.key === 'ArrowUp' ? -step : evt.key === 'ArrowDown' ? step : 0);
    const updates = {};
    for (const c of selectedComponents()) {
      updates[c.id] = { x: c.x + dx, y: c.y + dy, rotation: c.rotation,
                        flip: c.flip };
    }
    if (!Object.keys(updates).length) return;
    pushHistory();
    try {
      applyPayload(await api('/api/layout', { positions: updates }));
    } catch (err) { setStatus('Error: ' + err.message); }
  }
});

document.addEventListener('keyup', evt => {
  if (evt.code === 'Space') spaceDown = false;
});

// ── toolbar ────────────────────────────────────────────────────────────────

// ── align / distribute ──────────────────────────────────────────────────

function selectedComponents() {
  // Locked parts stay put; they can still be selected so they can be unlocked.
  return scene ? scene.components.filter(c => selected.has(c.id) && !c.locked) : [];
}

async function applyPositions(updates) {
  pushHistory();
  try {
    applyPayload(await api('/api/layout', { positions: updates }));
  } catch (err) { setStatus('Error: ' + err.message); }
}

/** Line the selection up on the first-clicked component (a predictable anchor). */
async function align(axis) {
  const comps = selectedComponents();
  if (comps.length < 2) return;
  const anchorId = [...selected][0];          // Set keeps insertion order
  const anchor = comps.find(c => c.id === anchorId) || comps[0];
  const updates = {};
  for (const c of comps) {
    updates[c.id] = {
      x: axis === 'x' ? anchor.x : c.x,
      y: axis === 'y' ? anchor.y : c.y,
      rotation: c.rotation,
      flip: c.flip,
    };
  }
  await applyPositions(updates);
}

/** Even spacing along one axis; the two outermost components stay put. */
async function distribute(axis) {
  const comps = selectedComponents();
  if (comps.length < 3) return;
  comps.sort((a, b) => (axis === 'x' ? a.x - b.x : a.y - b.y));
  const first = comps[0];
  const last = comps[comps.length - 1];
  const start = axis === 'x' ? first.x : first.y;
  const span = (axis === 'x' ? last.x : last.y) - start;
  const step = span / (comps.length - 1);
  const updates = {};
  comps.forEach((c, i) => {
    const v = start + step * i;
    updates[c.id] = {
      x: axis === 'x' ? v : c.x,
      y: axis === 'y' ? v : c.y,
      rotation: c.rotation,
      flip: c.flip,
    };
  });
  await applyPositions(updates);
}

btnAlignX.addEventListener('click', () => align('x'));
btnAlignY.addEventListener('click', () => align('y'));
btnDistX.addEventListener('click', () => distribute('x'));
btnDistY.addEventListener('click', () => distribute('y'));

document.getElementById('btnFit').addEventListener('click', fitView);

document.getElementById('btnArrange').addEventListener('click', async () => {
  if (!confirm('Discard all positions (locked parts stay) and lay out the schematic again?')) return;
  pushHistory();
  try {
    applyPayload(await api('/api/autoarrange', {}));
    fitView();
  } catch (err) { setStatus('Error: ' + err.message); }
});

document.getElementById('btnReroute').addEventListener('click', async () => {
  setStatus('Routing…');
  try {
    applyPayload(await api('/api/reroute', {}));
  } catch (err) { setStatus('Error: ' + err.message); }
});

document.getElementById('btnExport').addEventListener('click', async () => {
  try {
    const res = await api('/api/export', {});
    setStatus('Saved: ' + res.path);
  } catch (err) { setStatus('Error: ' + err.message); }
});

projectSelect.addEventListener('change', async () => {
  try {
    applyPayload(await api('/api/open', { name: projectSelect.value }));
    fitView();
  } catch (err) { setStatus('Error: ' + err.message); }
});

chkNotes.addEventListener('change', renderNotes);

gridSelect.addEventListener('change', async () => {
  try {
    applyPayload(await api('/api/layout', { grid: parseInt(gridSelect.value, 10) }));
  } catch (err) { setStatus('Error: ' + err.message); }
});

chkGrid.addEventListener('change', async () => {
  gridRectEl.style.display = chkGrid.checked ? '' : 'none';
  try {
    applyPayload(await api('/api/layout', { showGrid: chkGrid.checked }));
  } catch (err) { setStatus('Error: ' + err.message); }
});

// ── live reload when the LLM rewrites circuit.json ─────────────────────────

setInterval(async () => {
  if (drag || pan || wpDrag || wireClick || band || noteDrag) return;
  try {
    const v = await api('/api/version');
    if (v.version !== lastVersion) await loadState(false);
  } catch (err) { /* server gone; keep the last drawing on screen */ }
}, 1500);

window.addEventListener('resize', () => {
  const rect = canvas.getBoundingClientRect();
  const aspect = rect.width / Math.max(rect.height, 1);
  view.h = view.w / aspect;
  applyView();
});

loadState(true).catch(err => setStatus('Error while loading: ' + err.message));
