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
const ncG = document.getElementById('noConnect');
const openPinsG = document.getElementById('openPins');
const chkNotes = document.getElementById('chkNotes');
const chkOpen = document.getElementById('chkOpen');
const chkNetNames = document.getElementById('chkNetNames');
const netPinsG = document.getElementById('netPins');
const guidesG = document.getElementById('guides');
const bandEl = document.getElementById('band');
const gridRectEl = document.getElementById('gridRect');
const chkGrid = document.getElementById('chkGrid');
const errorsBox = document.getElementById('errors');
const warningsBox = document.getElementById('warnings');
const statusEl = document.getElementById('status');
const projectSelect = document.getElementById('projectSelect');
const gridSelect = document.getElementById('gridSelect');
const btnAlignX = document.getElementById('btnAlignX');
const btnAlignY = document.getElementById('btnAlignY');
const btnDistX = document.getElementById('btnDistX');
const btnDistY = document.getElementById('btnDistY');
const btnHandBack = document.getElementById('btnHandBack');
const bbCanvas = document.getElementById('bbCanvas');
const bbContent = document.getElementById('bbContent');
const bbEmpty = document.getElementById('bbEmpty');
const btnModeSch = document.getElementById('btnModeSch');
const btnModeBb = document.getElementById('btnModeBb');

let scene = null;
let lastProject = null;
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
let review = { reviewed: false, current: false };
let mode = 'schematic';     // or 'breadboard' — stored per project
let bb = null;              // breadboard payload from the server
let bbSvg = '';
let bbView = null;
let bbPan = null;
let bbHover = null;
let bbViewSaveTimer = null;
let fitPending = false;     // schematic fit requested while it was hidden
let boxCollapsed = { errors: false, warnings: false };   // stored per project
const undoStack = [];
const redoStack = [];
const HISTORY_MAX = 50;
const DBLCLICK_MS = 700;
const SNAP_PX = 9;          // pin-alignment catch radius, in screen pixels
const PNG_TARGET_W = 1400;  // picture handed back: readable, but not huge

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

/** Fill one of the two message boxes: a headline that folds the box away on
 *  click, the details below it. Folded, only the headline stays on screen,
 *  so a long list no longer covers the drawing. */
function fillBox(box, head, body) {
  box.hidden = false;
  const folded = !!boxCollapsed[box.id];
  box.classList.toggle('collapsed', folded);
  box.innerHTML =
    `<div class="box-head" title="${folded ? 'Show the details' : 'Fold away'}">` +
    `<span class="box-arrow">${folded ? '▸' : '▾'}</span>${escapeHtml(head)}</div>` +
    `<div class="box-body">${escapeHtml(body)}</div>`;
}

for (const box of [errorsBox, warningsBox]) {
  box.addEventListener('click', evt => {
    const head = evt.target.closest('.box-head');
    if (!head) return;
    boxCollapsed[box.id] = !boxCollapsed[box.id];
    const folded = boxCollapsed[box.id];
    box.classList.toggle('collapsed', folded);
    head.title = folded ? 'Show the details' : 'Fold away';
    head.querySelector('.box-arrow').textContent = folded ? '▸' : '▾';
    api('/api/layout', { collapsed: boxCollapsed }).catch(() => {});
  });
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

/** Where a waypoint dropped at (x, y) should really go: onto the x or y line
 *  of a nearby pin if there is one (so the wire runs straight into it),
 *  otherwise onto the grid. Returns the point plus guide lines to show. */
function snapWaypoint(x, y, upp) {
  const tol = Math.max(SNAP_PX * upp, grid * 0.75);
  let bx = null, by = null, dxBest = tol, dyBest = tol;
  for (const c of scene.components) {
    for (const p of c.pins) {
      const ddx = Math.abs(p.x - x), ddy = Math.abs(p.y - y);
      if (ddx <= dxBest) { dxBest = ddx; bx = p.x; }
      if (ddy <= dyBest) { dyBest = ddy; by = p.y; }
    }
  }
  return {
    x: bx !== null ? bx : snap(x),
    y: by !== null ? by : snap(y),
    gx: bx, gy: by,
  };
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
  const rect = canvas.getBoundingClientRect();
  // Hidden behind the breadboard view: fitting now would compute (and save)
  // a nonsense view, so do it when the schematic is shown again.
  if (!rect.width || !rect.height) { fitPending = true; return; }
  fitPending = false;
  const [x, y, w, h] = scene.viewBox;
  const aspect = rect.width / rect.height;
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

  // No-connect crosses are part of the drawing (they end up in the export);
  // the open-pin rings are an editor aid and stay out of it.
  ncG.innerHTML = (scene.ncMarks || []).map(m =>
    `<g class="nc"><title>${escapeHtml(m.ref)} — deliberately left open</title>` +
    `<line x1="${m.x - 5}" y1="${m.y - 5}" x2="${m.x + 5}" y2="${m.y + 5}"/>` +
    `<line x1="${m.x - 5}" y1="${m.y + 5}" x2="${m.x + 5}" y2="${m.y - 5}"/></g>`
  ).join('');

  openPinsG.innerHTML = chkOpen.checked ? (scene.openPins || []).map(m =>
    `<circle class="openpin" cx="${m.x}" cy="${m.y}" r="6">` +
    `<title>${escapeHtml(m.ref)} — not connected to anything</title></circle>`
  ).join('') : '';

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
    fillBox(errorsBox, `${scene.errors.length} problem(s) while drawing`,
            scene.errors.join('\n'));
  } else {
    errorsBox.hidden = true;
  }

  const erc = scene.erc || [];
  if (erc.length) {
    const wiring = erc.filter(w => w.kind !== 'placement');
    const placement = erc.filter(w => w.kind === 'placement');
    const parts = [];
    if (wiring.length) {
      parts.push(`${wiring.length} wiring warning(s) — that is the assistant's ` +
                 `job, so tell it:\n` + wiring.map(w => '· ' + w.message).join('\n'));
    }
    if (placement.length) {
      parts.push(`${placement.length} placement warning(s) — yours to fix ` +
                 `by dragging:\n` + placement.map(w => '· ' + w.message).join('\n'));
    }
    const split = [wiring.length && `${wiring.length} wiring`,
                   placement.length && `${placement.length} placement`].filter(Boolean);
    fillBox(warningsBox, `${erc.length} warning(s): ${split.join(', ')}`,
            parts.join('\n\n'));
  } else {
    warningsBox.hidden = true;
  }

  setStatus(defaultStatus());

  renderNotes();
  if (mode === 'breadboard') renderBbBoxes();

  btnAlignX.disabled = btnAlignY.disabled = selectedComponents().length < 2;
  btnDistX.disabled = btnDistY.disabled = selectedComponents().length < 3;
}

function applyPayload(payload) {
  scene = payload.scene;
  lastVersion = payload.version;
  lastProject = payload.project;
  grid = scene.grid || 20;
  gridSelect.value = String(grid);
  chkGrid.checked = payload.showGrid !== false;
  chkNetNames.checked = payload.showNetNames !== false;
  gridRectEl.style.display = chkGrid.checked ? '' : 'none';
  review = payload.review || { reviewed: false, current: false };
  bbReview = payload.bbReview || { reviewed: false, current: false };
  updateHandBack();
  document.title = `${scene.title} — CircuitStudio`;

  const names = payload.projects.length ? payload.projects : [payload.project];
  projectSelect.innerHTML = names
    .map(n => `<option value="${escapeHtml(n)}">${escapeHtml(n)}</option>`).join('');
  projectSelect.value = payload.project;

  const ids = new Set(scene.components.map(c => c.id));
  selected = new Set([...selected].filter(id => ids.has(id)));

  bb = payload.breadboard || { exists: false };
  renderBreadboard();
  render();
  return payload;
}

// ── loading ────────────────────────────────────────────────────────────────

async function loadState(resetView) {
  const payload = await api('/api/state');
  if (resetView) {
    boxCollapsed = { errors: false, warnings: false, ...(payload.collapsed || {}) };
    mode = payload.mode === 'breadboard' ? 'breadboard' : 'schematic';
    bbView = payload.bbView && payload.bbView.w > 0 ? payload.bbView : null;
    bbSvg = '';   // another project: its board must be drawn afresh
  }
  applyPayload(payload);
  applyMode();
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
  return { positions, wires, ties: { ...(scene.ties || {}) } };
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
  } else if (compEl && evt.altKey) {
    assignTie(compEl.dataset.id);
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
    const s = snapWaypoint(o[0] + dx, o[1] + dy, wpDrag.upp);
    showGuides(s.gx, s.gy);
    wpDrag.el.setAttribute('cx', s.x);
    wpDrag.el.setAttribute('cy', s.y);
    wpDrag.target = [s.x, s.y];
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
    clearGuides();
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
      const s = snapWaypoint(p.x, p.y, viewMetrics().upp);
      const points = edge.waypoints.map(q => [q[0], q[1]]);
      points.splice(c.leg, 0, [s.x, s.y]);
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
  if (mode === 'breadboard') {
    // Its own shortcuts: the schematic's would act on parts that are not
    // even visible.
    await bbKeydown(evt);
    return;
  }
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

const TIE_TYPES = new Set(['ground', 'vcc', 'vdd', 'label']);

/** Alt+click on a part: wire its pins to the selected GND/VCC/label symbol
 *  instead of the nearest one. Alt+click with no such symbol selected hands
 *  the part back to the nearest-symbol rule. */
async function assignTie(partId) {
  const sel = scene.components.filter(c => selected.has(c.id));
  const symbol = sel.length === 1 && TIE_TYPES.has(sel[0].type) ? sel[0].id : null;
  if (symbol === partId) return;
  pushHistory();
  try {
    const payload = await api('/api/tie', { part: partId, symbol });
    applyPayload(payload);
    const n = (payload.changed || []).length;
    if (symbol) {
      setStatus(n ? `${partId}: ${n} pin(s) now wired to ${symbol}.`
                  : `${partId} shares no net with ${symbol}.`);
    } else {
      setStatus(n ? `${partId}: back to the nearest symbol.`
                  : 'Select one GND/VCC/label symbol first, then Alt+click a part.');
    }
  } catch (err) { setStatus('Error: ' + err.message); }
}

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

document.getElementById('btnFit').addEventListener('click',
  () => (mode === 'breadboard' ? fitBb() : fitView()));

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
    if (mode !== 'breadboard') {
      const res = await api('/api/export', {});
      setStatus('Saved: ' + res.path);
      return;
    }
    // The breadboard also goes out as a PNG — handy for a chat or a forum.
    let png = null;
    try {
      png = await svgToPng((await api('/api/svg', { what: 'breadboard' })).svg);
    } catch (err) { /* the SVG alone still gets written */ }
    const res = await api('/api/export', png ? { what: 'breadboard', png }
                                             : { what: 'breadboard' });
    setStatus('Saved: ' + res.path + (res.png ? ' and ' + res.png : ''));
  } catch (err) { setStatus('Error: ' + err.message); }
});

// ── handing the finished arrangement back to the assistant ─────────────────

/** Rasterise the exported SVG in the browser.
 *
 *  Python cannot turn SVG into a bitmap without pulling in a rendering library,
 *  and the whole app is deliberately dependency-free — but the browser is
 *  already here and is the thing that defines what the drawing looks like, so
 *  it does the job and the result is WYSIWYG by construction.
 */
function svgToPng(svgText) {
  return new Promise((resolve, reject) => {
    const blob = new Blob([svgText], { type: 'image/svg+xml;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const img = new Image();
    img.onload = () => {
      try {
        const w = img.naturalWidth || img.width;
        const h = img.naturalHeight || img.height;
        const scale = Math.min(2, Math.max(0.5, PNG_TARGET_W / Math.max(w, 1)));
        const cv = document.createElement('canvas');
        cv.width = Math.max(1, Math.round(w * scale));
        cv.height = Math.max(1, Math.round(h * scale));
        const ctx = cv.getContext('2d');
        ctx.fillStyle = '#ffffff';
        ctx.fillRect(0, 0, cv.width, cv.height);
        ctx.drawImage(img, 0, 0, cv.width, cv.height);
        resolve(cv.toDataURL('image/png'));
      } catch (err) {
        reject(err);
      } finally {
        URL.revokeObjectURL(url);
      }
    };
    img.onerror = () => {
      URL.revokeObjectURL(url);
      reject(new Error('the browser could not render the SVG'));
    };
    img.src = url;
  });
}

/** The button reflects the view that is shown: schematic and breadboard are
 *  handed back separately. */
function updateHandBack() {
  if (!btnHandBack) return;
  const r = mode === 'breadboard' ? bbReview : review;
  const what = mode === 'breadboard' ? 'breadboard' : 'arrangement';
  btnHandBack.disabled = mode === 'breadboard' && !(bb && bb.exists);
  btnHandBack.classList.toggle('done', !!(r.reviewed && r.current));
  btnHandBack.classList.toggle('stale', !!(r.reviewed && !r.current));
  btnHandBack.title = !r.reviewed
    ? `Mark the ${what} as finished and render a picture the assistant can look at`
    : r.current
      ? `Handed back ${r.at} — the assistant can see this ${what}`
      : `Handed back ${r.at}, but the ${what} has changed since. Click again.`;
}

btnHandBack.addEventListener('click', async () => {
  const what = mode === 'breadboard' ? { what: 'breadboard' } : {};
  btnHandBack.disabled = true;
  setStatus('Rendering the picture…');
  let png = null;
  try {
    const { svg } = await api('/api/svg', what);
    png = await svgToPng(svg);
  } catch (err) {
    // Still worth recording the sign-off; the assistant then gets the numbers
    // and the SVG path instead of a picture.
    setStatus('Could not render a picture (' + err.message + ') — handing back the SVG only.');
  }
  try {
    const payload = await api('/api/review', png ? { png, ...what } : what);
    applyPayload(payload);
    setStatus(`Handed back${png ? ' with a picture' : ''} — the assistant can review it now.`);
  } catch (err) {
    setStatus('Error: ' + err.message);
  } finally {
    btnHandBack.disabled = false;
    updateHandBack();
  }
});

projectSelect.addEventListener('change', async () => {
  const previous = scene ? lastProject : null;
  try {
    applyPayload(await api('/api/open', { name: projectSelect.value }));
    fitView();
  } catch (err) {
    if (previous) projectSelect.value = previous;   // we did not switch
    setStatus('Error: ' + err.message);
  }
});

chkNotes.addEventListener('change', renderNotes);

chkOpen.addEventListener('change', render);

gridSelect.addEventListener('change', async () => {
  try {
    applyPayload(await api('/api/layout', { grid: parseInt(gridSelect.value, 10) }));
  } catch (err) { setStatus('Error: ' + err.message); }
});

chkNetNames.addEventListener('change', async () => {
  try {
    applyPayload(await api('/api/layout', { showNetNames: chkNetNames.checked }));
  } catch (err) { setStatus('Error: ' + err.message); }
});

chkGrid.addEventListener('change', async () => {
  gridRectEl.style.display = chkGrid.checked ? '' : 'none';
  try {
    applyPayload(await api('/api/layout', { showGrid: chkGrid.checked }));
  } catch (err) { setStatus('Error: ' + err.message); }
});

// ── breadboard view ────────────────────────────────────────────────────────
//
// The server draws the plan and checks it against the netlist; the editor
// moves things around. Every change edits a copy of the plan and sends the
// whole plan back — undo is simply sending an older one. Holes, leads and
// wires carry their electrical node in data-node, so pointing at one lights
// up everything connected to it.

const BB_KINDS = {
  short: 'short(s) — nets that must stay apart meet',
  open: 'open connection(s) — the board does not join what the schematic joins',
  stray: 'unused pin(s) sitting in a live column',
  unplaced: 'not plugged in yet',
};
// Lead spacing a new part gets when it comes off the tray, in columns.
const BB_SPAN = { resistor: 4, inductor: 4, diode: 4, zener: 4, fuse: 4,
                  capacitor: 2, capacitor_pol: 2, crystal: 2, switch: 2, led: 1 };
// Parts that do not sit on a breadboard; only their leads end in a hole.
const BB_OFFBOARD = new Set(['speaker', 'battery', 'source_dc', 'source_ac',
                             'connector', 'esp32', 'rpi', 'arduino_uno', 'arduino_nano']);
const bbTray = document.getElementById('bbTray');
const bbSize = document.getElementById('bbSize');
const bbSplit = document.getElementById('bbSplit');
const bbGhost = document.getElementById('bbGhost');
const bbUndo = [];
const bbRedo = [];
let bbSel = null;           // { part: id } or { wire: index }
let bbDrag = null;
let bbReview = { reviewed: false, current: false };

// ── geometry ─────────────────────────────────────────────────────────────

function bbGeo() { return bb.geometry; }

function parseHole(name) {
  const m = /^([a-j])(\d+)$/.exec(name) || /^([TB][+-])(\d+)$/.exec(name);
  return m ? { row: m[1], col: parseInt(m[2], 10) } : null;
}

function isRail(row) { return row in bbGeo().railY; }

function holeXY(name) {
  const g = bbGeo(), h = parseHole(name);
  const y = isRail(h.row) ? g.railY[h.row] : g.rowY[h.row];
  return { x: h.col * g.pitch, y: y * g.pitch };
}

/** The hole under a point, if the point is reasonably close to one. */
function nearestHole(x, y, terminalOnly = false) {
  const g = bbGeo(), P = g.pitch;
  const col = Math.round(x / P);
  if (col < 1 || col > g.columns || Math.abs(col * P - x) > P * 0.8) return null;
  let best = null, bd = Infinity;
  for (const row of g.rowOrder) {
    if (terminalOnly && isRail(row)) continue;
    const d = Math.abs((isRail(row) ? g.railY[row] : g.rowY[row]) * P - y);
    if (d < bd) { bd = d; best = row; }
  }
  return bd <= P * 0.8 ? best + col : null;
}

function rotOffset(dx, dy, r) {
  for (let i = 0; i < r / 90; i++) [dx, dy] = [-dy, dx];
  return [dx, dy];
}

/** Holes of a rigid part with pin 1 at `anchor`, or null if it does not fit. */
function rigidHoles(offsets, anchor, rotation) {
  const g = bbGeo(), a = anchor && parseHole(anchor);
  if (!a || isRail(a.row)) return null;
  const rowAt = {};
  for (const [r, y] of Object.entries(g.rowY)) rowAt[y] = r;
  const out = {};
  for (const [num, [dx, dy]] of Object.entries(offsets)) {
    const [rx, ry] = rotOffset(dx, dy, rotation);
    const row = rowAt[g.rowY[a.row] + ry], col = a.col + rx;
    if (!row || col < 1 || col > g.columns) return null;
    out[num] = row + col;
  }
  return out;
}

/** A hole moved by whole columns and rows (rails count as rows). */
function shiftHole(name, dcol, drow) {
  const g = bbGeo(), h = parseHole(name);
  if (!h) return null;
  const i = g.rowOrder.indexOf(h.row) + drow, col = h.col + dcol;
  if (i < 0 || i >= g.rowOrder.length || col < 1 || col > g.columns) return null;
  return g.rowOrder[i] + col;
}

function rowIndex(name) { return bbGeo().rowOrder.indexOf(parseHole(name).row); }

/** Free, or taken only by what is being moved. */
function holeFree(name, mine = () => false) {
  const u = bb.used[name];
  return !u || mine(u);
}

// ── plan editing + undo ──────────────────────────────────────────────────

function planCopy() {
  const p = JSON.parse(JSON.stringify(bb.plan || {}));
  if (!p.parts || typeof p.parts !== 'object') p.parts = {};
  if (!Array.isArray(p.wires)) p.wires = [];
  return p;
}

async function sendPlan(plan, msg) {
  try {
    applyPayload(await api('/api/breadboard', { plan }));
    if (msg) setStatus(msg);
  } catch (err) { setStatus('Error: ' + err.message); }
}

async function commitPlan(plan, msg) {
  bbUndo.push(JSON.stringify(bb.plan));
  if (bbUndo.length > HISTORY_MAX) bbUndo.shift();
  bbRedo.length = 0;
  await sendPlan(plan, msg);
}

async function bbTravel(from, to, verb) {
  const s = from.pop();
  if (!s) { setStatus(`Nothing to ${verb}.`); return; }
  to.push(JSON.stringify(bb.plan));
  await sendPlan(JSON.parse(s), `${verb === 'undo' ? 'Undone' : 'Redone'} — ` +
                 `${bbUndo.length} undo / ${bbRedo.length} redo step(s) left.`);
}

// ── view ─────────────────────────────────────────────────────────────────

function bbMetrics() {
  const rect = bbCanvas.getBoundingClientRect();
  const upp = Math.max(bbView.w / rect.width, bbView.h / rect.height);
  return {
    rect, upp,
    offX: (rect.width - bbView.w / upp) / 2,
    offY: (rect.height - bbView.h / upp) / 2,
  };
}

function bbToUser(evt) {
  const m = bbMetrics();
  return {
    x: bbView.x + (evt.clientX - m.rect.left - m.offX) * m.upp,
    y: bbView.y + (evt.clientY - m.rect.top - m.offY) * m.upp,
  };
}

function applyBbView() {
  if (bbView) {
    bbCanvas.setAttribute('viewBox', `${bbView.x} ${bbView.y} ${bbView.w} ${bbView.h}`);
  }
}

function scheduleBbViewSave() {
  clearTimeout(bbViewSaveTimer);
  bbViewSaveTimer = setTimeout(() => {
    api('/api/layout', { bbView }).catch(() => {});
  }, 900);
}

function fitBb() {
  if (!bb || !bb.exists) return;
  const rect = bbCanvas.getBoundingClientRect();
  if (!rect.width || !rect.height) return;   // not visible yet
  const [x, y, w, h] = bb.viewBox;
  const aspect = rect.width / rect.height;
  let vw = w, vh = h;
  if (w / h > aspect) vh = w / aspect; else vw = h * aspect;
  bbView = { x: x - (vw - w) / 2, y: y - (vh - h) / 2, w: vw, h: vh };
  applyBbView();
  scheduleBbViewSave();
}

// ── rendering ────────────────────────────────────────────────────────────

function bbProblems() {
  return ((bb && bb.findings) || []).filter(f => f.kind !== 'unplaced');
}

function bbDefaultStatus() {
  if (!bb || !bb.exists) return 'No breadboard plan yet';
  const all = bb.findings || [];
  const problems = bbProblems().length;
  if (problems) return `Breadboard · ${problems} problem(s), see the list`;
  if (all.length) return 'Breadboard · no problems so far, but not everything is plugged in';
  return 'Breadboard · matches the schematic ✓';
}

/** The message boxes belong to whichever view is shown. */
function renderBbBoxes() {
  if (!bb || !bb.exists) {
    errorsBox.hidden = true;
    if (bb && bb.error) fillBox(errorsBox, 'breadboard.json cannot be read', bb.error);
    warningsBox.hidden = true;
    setStatus(bbDefaultStatus());
    return;
  }
  const findings = bb.findings || [];
  const structure = findings.filter(f => f.kind === 'structure');
  errorsBox.hidden = !structure.length;
  if (structure.length) {
    fillBox(errorsBox, `The plan itself has ${structure.length} problem(s)`,
            structure.map(f => '· ' + f.message).join('\n'));
  }
  const parts = [];
  for (const [kind, text] of Object.entries(BB_KINDS)) {
    if (kind === 'unplaced') continue;    // the tray shows those
    const list = findings.filter(f => f.kind === kind);
    if (list.length) {
      parts.push(`${list.length} ${text}:\n` + list.map(f => '· ' + f.message).join('\n'));
    }
  }
  warningsBox.hidden = !parts.length;
  if (parts.length) {
    const n = bbProblems().filter(f => f.kind !== 'structure').length;
    fillBox(warningsBox, `${n} problem(s) on the board`, parts.join('\n\n'));
  }
  setStatus(bbDefaultStatus());
}

/** Parts the plan does not place yet — drag them onto the board. */
function renderTray() {
  const list = (bb && bb.exists && bb.unplaced) || [];
  const loose = ((bb && bb.findings) || [])
    .filter(f => f.kind === 'unplaced' && !/is not on the breadboard/.test(f.message));
  bbTray.hidden = !list.length && !loose.length;
  let html = '';
  if (list.length) {
    html += `<div class="tray-head">Not on the board (${list.length}) — drag onto a hole</div>`;
    for (const p of list) {
      const label = `${escapeHtml(p.id)}<span class="tray-value">` +
                    `${escapeHtml(p.value || p.type)}</span>`;
      html += p.reason
        ? `<div class="tray-item disabled" title="${escapeHtml(p.reason)}">${label}</div>`
        : `<div class="tray-item" data-id="${escapeHtml(p.id)}" ` +
          `title="${escapeHtml(p.package || p.type)}">${label}</div>`;
    }
  }
  if (loose.length) {
    html += '<div class="tray-head">Leads not plugged in</div>' +
            loose.map(f => `<div class="tray-note">${escapeHtml(f.message)}</div>`).join('');
  }
  bbTray.innerHTML = html;
}

function bbApplySelection() {
  for (const el of bbContent.querySelectorAll('.sel')) el.classList.remove('sel');
  if (!bbSel) return;
  const sel = bbSel.part !== undefined
    ? `g.bbpart[data-part="${CSS.escape(bbSel.part)}"]`
    : `g.bbwire[data-wire="${bbSel.wire}"]`;
  for (const el of bbContent.querySelectorAll(sel)) el.classList.add('sel');
}

function bbSelect(sel) {
  bbSel = sel;
  bbApplySelection();
}

/** Board size and split rails, shown for the plan on screen. */
function renderBoardControls() {
  const has = !!(bb && bb.exists);
  bbSize.disabled = bbSplit.disabled = !has;
  if (!has) return;
  const cols = String(bb.columns);
  if (![...bbSize.options].some(o => o.value === cols)) {
    bbSize.insertAdjacentHTML('beforeend', `<option value="${cols}">${cols} columns</option>`);
  }
  bbSize.value = cols;
  bbSplit.checked = !!((bb.plan || {}).board || {}).split_rails;
}

async function changeBoard(patch, msg) {
  // Hand the keyboard back, or Ctrl+Z would go to the control, not the board.
  bbSize.blur();
  bbSplit.blur();
  const plan = planCopy();
  plan.board = { ...(plan.board || {}), ...patch };
  bbView = null;       // the board changed size: fit it again
  await commitPlan(plan, msg);
  fitBb();
}

bbSize.addEventListener('change', async () => {
  const n = parseInt(bbSize.value, 10);
  const beyond = Object.keys(bb.used || {}).filter(h => parseHole(h).col > n).length;
  if (beyond && !confirm(`${beyond} hole(s) in use lie beyond column ${n} and ` +
                         `would be off the board. Change the size anyway?`)) {
    renderBoardControls();
    return;
  }
  await changeBoard({ columns: n }, `Board: ${n} columns.`);
});

bbSplit.addEventListener('change', () => changeBoard(
  { split_rails: bbSplit.checked },
  bbSplit.checked ? 'Rails split in the middle.' : 'Rails run the full length.'));

function renderBreadboard() {
  renderBoardControls();
  const n = bbProblems().length;
  btnModeBb.innerHTML = 'Breadboard' + (n ? `<span class="badge">${n}</span>` : '');
  btnModeBb.title = !bb || !bb.exists
    ? 'No breadboard plan yet — the assistant writes one with write_breadboard'
    : n ? `${n} problem(s) on the breadboard` : 'The breadboard plan matches the schematic';
  renderTray();

  if (!bb || !bb.exists) {
    bbContent.innerHTML = '';
    bbSvg = '';
    if (mode === 'breadboard') applyMode();
    return;
  }
  if (bb.svg !== bbSvg) {
    bbSvg = bb.svg;
    bbContent.innerHTML = bb.svg;
    bbHover = null;
    bbCanvas.classList.remove('focus');
  }
  if (bbSel && bbSel.part !== undefined && !(bbSel.part in (bb.parts || {}))) bbSel = null;
  if (bbSel && bbSel.wire !== undefined && !(bb.plan.wires || [])[bbSel.wire]) bbSel = null;
  bbApplySelection();
  if (mode === 'breadboard') {
    bbCanvas.style.display = '';
    bbEmpty.hidden = true;
    if (bbView) applyBbView(); else fitBb();
  }
}

/** Throw away drag previews: redraw from the last server picture. */
function bbRedraw() {
  bbContent.innerHTML = bbSvg;
  bbGhost.innerHTML = '';
  bbHover = null;
  bbApplySelection();
}

function applyMode() {
  const isBb = mode === 'breadboard';
  const has = !!(bb && bb.exists);
  document.body.classList.toggle('bb', isBb);
  canvas.style.display = isBb ? 'none' : '';
  bbCanvas.style.display = isBb && has ? '' : 'none';
  bbEmpty.hidden = !(isBb && !has);
  btnModeSch.classList.toggle('active', !isBb);
  btnModeBb.classList.toggle('active', isBb);
  updateHandBack();
  if (isBb) {
    if (has) { if (bbView) applyBbView(); else fitBb(); }
    renderBbBoxes();
  } else {
    if (fitPending) fitView();
    render();     // puts the schematic's messages and status back
  }
}

function setMode(m) {
  if (m === mode) return;
  setNetFocus(null);
  setBbFocus(null);
  mode = m;
  applyMode();
  api('/api/layout', { mode }).catch(() => {});
}

btnModeSch.addEventListener('click', () => setMode('schematic'));
btnModeBb.addEventListener('click', () => setMode('breadboard'));

/** Light up one electrical node and say what is in it. */
function setBbFocus(node) {
  if (node === bbHover) return;
  bbHover = node;
  for (const el of bbContent.querySelectorAll('.hl')) el.classList.remove('hl');
  bbCanvas.classList.toggle('focus', !!node);
  if (mode !== 'breadboard') return;
  if (!node) { setStatus(bbDefaultStatus()); return; }
  for (const el of bbContent.querySelectorAll(`[data-node="${CSS.escape(node)}"]`)) {
    el.classList.add('hl');
  }
  const info = (bb.nodes || {})[node];
  if (!info) { setStatus('Empty — nothing plugged in here'); return; }
  // The net first: in a big node (GND) the list of places gets long.
  const pins = info.pins, where = info.where;
  setStatus((info.nets.length ? `Net ${info.nets.join(' + ')}` : 'No net') +
            ` · ${where.slice(0, 3).join(', ')}` +
            (where.length > 3 ? ` +${where.length - 3} more places` : '') +
            (pins.length ? ` · ${pins.slice(0, 6).join(', ')}` : '') +
            (pins.length > 6 ? ` … (+${pins.length - 6})` : ''));
}

// ── drag previews ────────────────────────────────────────────────────────

function showGhost(holes, ok, line) {
  let html = '';
  if (line) {
    html += `<line x1="${line[0].x}" y1="${line[0].y}" x2="${line[1].x}" y2="${line[1].y}" ` +
            `class="ghost-line${ok ? '' : ' bad'}"/>`;
  }
  for (const h of holes) {
    if (!h) continue;
    const { x, y } = holeXY(h);
    html += `<circle cx="${x}" cy="${y}" r="5.5" class="ghost-pad${ok ? '' : ' bad'}"/>`;
  }
  bbGhost.innerHTML = html;
}

function moveGroup(selector, dx, dy) {
  for (const el of bbContent.querySelectorAll(selector)) {
    el.setAttribute('transform', `translate(${dx},${dy})`);
    el.classList.add('dragging');
  }
}

/** Where a dragged part would land, and whether it may. */
function partTarget(d, p) {
  const plan = bb.plan.parts[d.id] || {};
  const mine = u => u.part === d.id;
  if (d.rigid) {
    const o = holeXY(d.anchor);
    const anchor = nearestHole(o.x + p.x - d.start.x, o.y + p.y - d.start.y, true);
    const holes = anchor && rigidHoles(d.offsets, anchor, d.rotation);
    const ok = !!holes && Object.values(holes).every(h => holeFree(h, mine));
    return { ok, holes: holes ? Object.values(holes) : [], place: { ...plan, anchor },
             moved: anchor !== d.anchor, dx: anchor ? holeXY(anchor).x - o.x : 0,
             dy: anchor ? holeXY(anchor).y - o.y : 0 };
  }
  const from = nearestHole(d.start.x, d.start.y), to = nearestHole(p.x, p.y);
  if (!from || !to) return { ok: false, holes: [], moved: false };
  const dcol = parseHole(to).col - parseHole(from).col;
  const drow = rowIndex(to) - rowIndex(from);
  const legs = {};
  for (const [pin, h] of Object.entries(plan.legs || {})) legs[pin] = shiftHole(h, dcol, drow);
  const holes = Object.values(legs);
  const ok = holes.every(h => h && holeFree(h, mine));
  const first = Object.values(plan.legs || {})[0];
  const a = first && holeXY(first), b = holes[0] && holeXY(holes[0]);
  return { ok, holes, place: { ...plan, legs }, moved: dcol !== 0 || drow !== 0,
           dx: a && b ? b.x - a.x : 0, dy: a && b ? b.y - a.y : 0 };
}

/** Holes a part from the tray would take with its first lead at `hole`. */
function trayPlacement(entry, p) {
  if (entry.kind === 'rigid') {
    const anchor = nearestHole(p.x, p.y, true);
    const holes = anchor && rigidHoles(entry.offsets, anchor, 0);
    return holes ? { holes: Object.values(holes), place: { anchor, rotation: 0 } } : null;
  }
  const start = nearestHole(p.x, p.y);
  if (!start) return null;
  const n = entry.pins.length;
  const offboard = BB_OFFBOARD.has(entry.type);
  const steps = n === 2 && !offboard
    ? [0, BB_SPAN[entry.type] || 3]
    : entry.pins.map((_, i) => i);
  const legs = {};
  entry.pins.forEach((pin, i) => { legs[pin] = shiftHole(start, steps[i], 0); });
  if (Object.values(legs).some(h => !h)) return null;
  return { holes: Object.values(legs),
           place: offboard ? { offboard: true, legs } : { legs } };
}

// ── pointer ──────────────────────────────────────────────────────────────

bbCanvas.addEventListener('pointerdown', evt => {
  if (!bb || !bb.exists) return;
  if (evt.button !== 0 && evt.button !== 1) return;
  evt.preventDefault();
  setBbFocus(null);
  const p = bbToUser(evt);
  const base = { sx: evt.clientX, sy: evt.clientY, start: p, moved: false };
  const t = evt.button === 0 && !spaceDown ? evt.target : null;
  const inside = el => (el && bbContent.contains(el) ? el : null);
  const wend = t && inside(t.closest('.wend'));
  const lead = t && inside(t.closest('.pad.lead'));
  const wireEl = t && inside(t.closest('g.bbwire'));
  const partEl = t && inside(t.closest('g.bbpart'));

  if (wend) {
    const i = parseInt(wend.dataset.wire, 10), end = wend.dataset.end;
    bbSelect({ wire: i });
    bbDrag = { ...base, kind: 'wend', wire: i, end,
               other: bb.plan.wires[i][end === 'from' ? 'to' : 'from'] };
  } else if (lead) {
    const id = lead.dataset.part, pin = lead.dataset.pin;
    bbSelect({ part: id });
    bbDrag = { ...base, kind: 'lead', id, pin, el: lead,
               origin: ((bb.plan.parts[id] || {}).legs || {})[pin] };
  } else if (wireEl) {
    bbSelect({ wire: parseInt(wireEl.dataset.wire, 10) });
  } else if (partEl) {
    const id = partEl.dataset.part, info = (bb.parts || {})[id];
    bbSelect({ part: id });
    if (info && !info.offboard) {
      bbDrag = { ...base, kind: 'part', id, rigid: info.kind === 'rigid',
                 anchor: info.anchor, rotation: info.rotation || 0,
                 offsets: info.offsets };
    }
  } else {
    const hole = t && nearestHole(p.x, p.y);
    const near = hole && Math.hypot(holeXY(hole).x - p.x, holeXY(hole).y - p.y)
                         < bbGeo().pitch * 0.45;
    bbSelect(null);
    if (near && holeFree(hole)) {
      bbDrag = { ...base, kind: 'wire', from: hole };
    } else {
      bbPan = { sx: evt.clientX, sy: evt.clientY, upp: bbMetrics().upp,
                view: { ...bbView }, moved: false };
      bbCanvas.classList.add('panning');
    }
  }
  try { bbCanvas.setPointerCapture(evt.pointerId); } catch (e) { /* ignore */ }
});

bbCanvas.addEventListener('pointermove', evt => {
  if (bbPan) {
    const dx = evt.clientX - bbPan.sx, dy = evt.clientY - bbPan.sy;
    if (Math.abs(dx) > 2 || Math.abs(dy) > 2) bbPan.moved = true;
    bbView.x = bbPan.view.x - dx * bbPan.upp;
    bbView.y = bbPan.view.y - dy * bbPan.upp;
    applyBbView();
    return;
  }
  if (bbDrag && bbDrag.kind !== 'tray') {
    const d = bbDrag;
    if (Math.abs(evt.clientX - d.sx) > 3 || Math.abs(evt.clientY - d.sy) > 3) d.moved = true;
    if (!d.moved) return;
    const p = bbToUser(evt);
    if (d.kind === 'part') {
      d.target = partTarget(d, p);
      moveGroup(`g.bbpart[data-part="${CSS.escape(d.id)}"]`, d.target.dx, d.target.dy);
      showGhost(d.target.holes, d.target.ok);
    } else if (d.kind === 'lead') {
      const h = nearestHole(p.x, p.y);
      d.to = h;
      d.ok = !!h && holeFree(h, u => u.part === d.id && u.pin === d.pin);
      if (h) { const q = holeXY(h); d.el.setAttribute('cx', q.x); d.el.setAttribute('cy', q.y); }
      showGhost([h], d.ok);
    } else if (d.kind === 'wend' || d.kind === 'wire') {
      const h = nearestHole(p.x, p.y);
      const anchor = d.kind === 'wire' ? d.from : d.other;
      d.to = h;
      d.ok = !!h && h !== anchor && holeFree(h, u => d.kind === 'wend' &&
                                                u.wire === d.wire && u.end === d.end);
      if (d.kind === 'wend') {
        for (const el of bbContent.querySelectorAll(`g.bbwire[data-wire="${d.wire}"]`)) {
          el.style.opacity = '0.25';
        }
      }
      showGhost([h], d.ok, [holeXY(anchor), h ? holeXY(h) : p]);
    }
    return;
  }
  if (!bbDrag) {
    const el = evt.target.closest ? evt.target.closest('[data-node]') : null;
    setBbFocus(el && bbContent.contains(el) ? el.dataset.node : null);
  }
});

bbCanvas.addEventListener('pointerup', async evt => {
  bbCanvas.classList.remove('panning');
  if (bbCanvas.hasPointerCapture(evt.pointerId)) bbCanvas.releasePointerCapture(evt.pointerId);
  if (bbPan) {
    if (bbPan.moved) scheduleBbViewSave();
    bbPan = null;
    return;
  }
  const d = bbDrag;
  if (!d || d.kind === 'tray') return;
  bbDrag = null;
  if (!d.moved) { bbGhost.innerHTML = ''; return; }

  const plan = planCopy();
  let msg = null;
  if (d.kind === 'part' && d.target && d.target.moved && d.target.ok) {
    plan.parts[d.id] = d.target.place;
    msg = `${d.id} moved.`;
  } else if (d.kind === 'lead' && d.ok && d.to && d.to !== d.origin) {
    plan.parts[d.id].legs[d.pin] = d.to;
    msg = `${d.id}.${d.pin} now in ${d.to}.`;
  } else if (d.kind === 'wend' && d.ok) {
    plan.wires[d.wire][d.end] = d.to;
    msg = `Wire now ends in ${d.to}.`;
  } else if (d.kind === 'wire' && d.ok) {
    plan.wires.push({ from: d.from, to: d.to });
    bbSel = { wire: plan.wires.length - 1 };
    msg = `Wire ${d.from} → ${d.to} added.`;
  }
  if (!msg) {
    bbRedraw();
    const taken = (d.target && !d.target.ok) || d.ok === false;
    if (taken) setStatus("Doesn't fit there — a hole is taken, or it lands off the strips.");
    return;
  }
  bbGhost.innerHTML = '';
  await commitPlan(plan, msg);
});

bbCanvas.addEventListener('pointerleave', () => { if (!bbPan && !bbDrag) setBbFocus(null); });

bbCanvas.addEventListener('wheel', evt => {
  evt.preventDefault();
  if (!bbView) return;
  const p = bbToUser(evt);
  const factor = evt.deltaY > 0 ? 1.12 : 1 / 1.12;
  bbView = {
    x: p.x - (p.x - bbView.x) * factor,
    y: p.y - (p.y - bbView.y) * factor,
    w: bbView.w * factor,
    h: bbView.h * factor,
  };
  applyBbView();
  scheduleBbViewSave();
}, { passive: false });

// Parts come off the tray by drag and drop; the tray lives outside the SVG,
// so this drag is followed on the document.
bbTray.addEventListener('pointerdown', evt => {
  const item = evt.target.closest('.tray-item[data-id]');
  if (!item || evt.button !== 0) return;
  const entry = (bb.unplaced || []).find(u => u.id === item.dataset.id);
  if (!entry) return;
  evt.preventDefault();
  bbDrag = { kind: 'tray', entry, target: null };
  document.body.classList.add('tray-dragging');
});

document.addEventListener('pointermove', evt => {
  if (!bbDrag || bbDrag.kind !== 'tray') return;
  const r = bbCanvas.getBoundingClientRect();
  const over = evt.clientX >= r.left && evt.clientX <= r.right &&
               evt.clientY >= r.top && evt.clientY <= r.bottom;
  const t = over ? trayPlacement(bbDrag.entry, bbToUser(evt)) : null;
  bbDrag.target = t;
  bbDrag.ok = !!t && t.holes.every(h => holeFree(h));
  if (t) showGhost(t.holes, bbDrag.ok); else bbGhost.innerHTML = '';
});

document.addEventListener('pointerup', async () => {
  if (!bbDrag || bbDrag.kind !== 'tray') return;
  const d = bbDrag;
  bbDrag = null;
  document.body.classList.remove('tray-dragging');
  bbGhost.innerHTML = '';
  if (!d.target) return;
  if (!d.ok) { setStatus("Doesn't fit there — a hole is taken."); return; }
  const plan = planCopy();
  plan.parts[d.entry.id] = d.target.place;
  bbSel = { part: d.entry.id };
  await commitPlan(plan, `${d.entry.id} placed.`);
});

// ── keys ─────────────────────────────────────────────────────────────────

/** Turn the selected part around: a chip's notch to the other side, a
 *  polarised part's leads swapped. The part keeps its spot. */
async function bbTurn() {
  if (!bbSel || bbSel.part === undefined) return;
  const id = bbSel.part, info = (bb.parts || {})[id];
  const plan = planCopy(), entry = plan.parts[id];
  if (!info || !entry) return;
  const mine = u => u.part === id;
  if (info.kind === 'rigid') {
    // Keep the centre of the footprint where it is.
    const g = bbGeo();
    const centre = holes => {
      const pts = holes.map(h => ({ c: parseHole(h).col, y: g.rowY[parseHole(h).row] }));
      return { c: (Math.min(...pts.map(q => q.c)) + Math.max(...pts.map(q => q.c))) / 2,
               y: (Math.min(...pts.map(q => q.y)) + Math.max(...pts.map(q => q.y))) / 2 };
    };
    const rotation = (info.rotation + 180) % 360;
    const now = centre(info.holes);
    const offs = Object.values(info.offsets).map(([dx, dy]) => rotOffset(dx, dy, rotation));
    const oc = (Math.min(...offs.map(o => o[0])) + Math.max(...offs.map(o => o[0]))) / 2;
    const oy = (Math.min(...offs.map(o => o[1])) + Math.max(...offs.map(o => o[1]))) / 2;
    const row = Object.keys(g.rowY).find(r => g.rowY[r] === now.y - oy);
    const anchor = row ? row + (now.c - oc) : null;
    const holes = anchor && rigidHoles(info.offsets, anchor, rotation);
    if (!holes || !Object.values(holes).every(h => holeFree(h, mine))) {
      setStatus(`${id} can't turn around here.`);
      return;
    }
    plan.parts[id] = { ...entry, anchor, rotation };
  } else {
    const pins = Object.keys(entry.legs || {});
    if (pins.length !== 2) { setStatus(`${id}: only two-lead parts can be turned.`); return; }
    const [a, b] = pins;
    entry.legs = { [a]: entry.legs[b], [b]: entry.legs[a] };
  }
  await commitPlan(plan, `${id} turned around.`);
}

async function bbDelete() {
  if (!bbSel) return;
  const plan = planCopy();
  if (bbSel.wire !== undefined) {
    plan.wires.splice(bbSel.wire, 1);
    bbSel = null;
    await commitPlan(plan, 'Wire removed.');
  } else if (plan.parts[bbSel.part]) {
    const id = bbSel.part;
    delete plan.parts[id];
    bbSel = null;
    await commitPlan(plan, `${id} unplugged — it is back in the tray.`);
  }
}

/** Let the server lay the board out: the missing parts only, or all afresh. */
async function bbAutoplace(mode) {
  const has = !!(bb && bb.exists);
  if (mode === 'all' && has &&
      !confirm('Lay the whole breadboard out afresh? Ctrl+Z brings the current one back.')) return;
  if (has) {
    bbUndo.push(JSON.stringify(bb.plan));
    if (bbUndo.length > HISTORY_MAX) bbUndo.shift();
    bbRedo.length = 0;
  }
  setStatus('Placing…');
  try {
    const payload = await api('/api/breadboard/autoplace', { mode });
    if (mode === 'all' || !has) bbView = null;
    applyPayload(payload);
    applyMode();
    const { log, failed } = payload.autoplace;
    const placed = log.filter(l => l.startsWith('placed') || l.includes('off the board')).length;
    setStatus(failed.length
      ? `Placed ${placed} part(s); ${failed.length} could not go on — ${failed.join(' · ')}`
      : placed ? `Placed ${placed} part(s). ${bbDefaultStatus().replace('Breadboard · ', '')}`
               : 'Nothing left to place.');
  } catch (err) { setStatus('Error: ' + err.message); }
}

document.getElementById('btnBbRest').addEventListener('click', () => bbAutoplace('rest'));
document.getElementById('btnBbAll').addEventListener('click', () => bbAutoplace('all'));
document.getElementById('btnBbCreate').addEventListener('click', () => bbAutoplace('all'));

async function bbKeydown(evt) {
  if (evt.code === 'Space') { spaceDown = true; evt.preventDefault(); return; }
  if (!bb || !bb.exists) return;
  const key = evt.key.toLowerCase();
  if (evt.ctrlKey || evt.metaKey) {
    if (key === 'z' && evt.shiftKey || key === 'y') {
      evt.preventDefault();
      await bbTravel(bbRedo, bbUndo, 'redo');
    } else if (key === 'z') {
      evt.preventDefault();
      await bbTravel(bbUndo, bbRedo, 'undo');
    }
    return;
  }
  if (key === 'f') fitBb();
  else if (key === 'escape') bbSelect(null);
  else if (key === 'r') await bbTurn();
  else if (evt.key === 'Delete' || evt.key === 'Backspace') {
    evt.preventDefault();
    await bbDelete();
  }
}

// ── live reload when the LLM rewrites circuit.json ─────────────────────────

setInterval(async () => {
  if (drag || pan || wpDrag || wireClick || band || noteDrag || bbPan || bbDrag) return;
  try {
    const v = await api('/api/version');
    // Another tab may have switched this editor to a different project; its
    // version counter restarts, so the number alone can look unchanged.
    if (v.project !== lastProject) await loadState(true);
    else if (v.version !== lastVersion) await loadState(false);
  } catch (err) { /* server gone; keep the last drawing on screen */ }
}, 1500);

window.addEventListener('resize', () => {
  const rect = canvas.getBoundingClientRect();
  if (rect.width > 0 && rect.height > 0) {
    view.h = view.w * rect.height / rect.width;
    applyView();
  }
  if (bbView) {
    const r = bbCanvas.getBoundingClientRect();
    if (r.width > 0) { bbView.h = bbView.w * r.height / r.width; applyBbView(); }
  }
});

loadState(true).catch(err => setStatus('Error while loading: ' + err.message));
