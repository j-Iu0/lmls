import {el, $, ms, number, download, jsonObject} from './dom.js';
import {normalizeConfig, clone, isActive, metadata, connect, disconnect, removeNode, renameNode, addNode, autoLayout, textTopics, validateLocal, CaptionStore, nodeQueues} from './model.mjs';
import {Graph} from './graph.js';
import {MediaManager} from './media.js';

let config = null, catalog = [], snapshot = {state: 'idle', epoch: 0, nodes: {}, metrics: {}, logs: [], media: {}}, socket;
let busy = '', connected = false, reconnectTimer, reconnectAttempts = 0, selection = null, inspectorMode = 'node', inspectorDirty = false, diagnosticTab = 'startup';
let hasSnapshot = false, pendingRun = false, seekCommands = 0, localSeeking = false;
const captions = new CaptionStore(), localLogs = [];
const locked = () => !config || Boolean(busy) || isActive(snapshot.state) || seekCommands > 0 || localSeeking;
const active = () => isActive(snapshot.state);
const formatMessage = value => typeof value === 'string' ? value : JSON.stringify(value);

function log(level, node, message) { localLogs.push({time: new Date().toISOString(), level, node, message, client: true}); if (localLogs.length > 300) localLogs.shift(); renderDiagnostics(); }
function notice(message, error = false) { $('#notice span').textContent = message; $('#notice').hidden = false; $('#notice').classList.toggle('is-error', error); if (error) log('error', 'studio', message); }
$('#notice button').onclick = () => { $('#notice').hidden = true; };
async function api(path, body, raw = false) {
  const response = await fetch(path, body === undefined ? {cache: 'no-store'} : {method: 'POST', headers: raw ? {'Content-Type': 'application/octet-stream'} : {'Content-Type': 'application/json'}, body: raw ? body : JSON.stringify(body)});
  const text = await response.text(); let data;
  try { data = text ? JSON.parse(text) : {}; } catch { data = text; }
  if (!response.ok) throw new Error(typeof data === 'string' ? data : formatMessage(data.message || data.detail || data.error || `HTTP ${response.status}`));
  return data;
}
const playbackRequests = new Set();
let lastPlaybackError = 0;
function send(message) {
  if (socket?.readyState === WebSocket.OPEN) { socket.send(JSON.stringify(message)); return; }
  if (playbackRequests.has(message.node)) return;
  playbackRequests.add(message.node);
  api('/api/playback', message).then(acceptSnapshot).catch(e => {
    if (Date.now() - lastPlaybackError > 5000) { log('error', 'playback', e.message); lastPlaybackError = Date.now(); }
  }).finally(() => playbackRequests.delete(message.node));
}

const media = new MediaManager({
  config: () => config, catalog: () => catalog, locked, active, epoch: () => snapshot.epoch,
  canSeek: () => ['running', 'completed'].includes(snapshot.state), preparing: () => pendingRun,
  seekingChanged: value => { localSeeking = value; syncControls(); },
  clockAllowed: () => !pendingRun && !seekCommands && snapshot.state === 'running', send,
  captions: () => captions.values(), clearCaptions, log, changed: () => changed(false),
  upload: async (name, file) => {
    if (locked()) return;
    busy = 'Uploading media'; syncControls();
    try {
      const result = await api(`/api/upload?name=${encodeURIComponent(file.name)}`, file, true);
      if (active()) throw new Error('Runtime started during upload. Stop it before attaching the uploaded file.');
      const node = config.nodes.find(n => n.name === name); if (!node) throw new Error('Source node no longer exists.');
      const key = Object.hasOwn(node.options, 'url') ? 'url' : Object.hasOwn(node.options, 'path') ? 'path' : Object.hasOwn(node.options, 'source') ? 'source' : /wav/i.test(node.impl) ? 'path' : 'url';
      const value = result.path || result.url; if (!value) throw new Error('Upload response did not include a path or URL.');
      node.options[key] = value; notice(`Attached ${file.name} to ${name}.`); changed();
    } finally { busy = ''; syncControls(); }
  },
  seek: async (node, position) => {
    if (!['running', 'completed'].includes(snapshot.state)) throw new Error('The runtime is not ready to seek.');
    seekCommands++; syncControls(); clearCaptions();
    try { const next = await api('/api/seek', {node, position, epoch: snapshot.epoch}); acceptSnapshot(next); }
    finally { seekCommands--; syncControls(); }
  },
});
const graph = new Graph($('#graph'), {
  locked, select: onSelect, changed: () => changed(false),
  add: (impl, point) => createNode(impl, point),
  connect: (a, b) => edit(() => { connect(config, catalog, a, b); }),
  delete: target => deleteSelection(target),
  media: (node, host) => media.mount(node, host), removeMedia: name => media.remove(name),
});

function edit(fn) {
  if (locked()) return false;
  if (!commitInspector(false)) return false;
  const before = clone(config);
  try { fn(); changed(); return true; } catch (e) { config = before; notice(e.message, true); changed(false); return false; }
}
function changed(inspector = true) {
  if (!config) return;
  $('#draft-state').textContent = 'Local draft';
  graph.render(config, catalog); graph.metrics(snapshot); renderTopics(); renderSubtitles();
  if (inspector) renderInspector(); syncControls();
}
function onSelect(next) {
  if (JSON.stringify(next) === JSON.stringify(selection) && inspectorMode === 'node') return;
  if (!commitInspector(false)) { graph.selection = selection; graph.render(config, catalog); return; }
  selection = next; inspectorMode = 'node'; renderInspector();
}
function createNode(impl, point = graph.center()) {
  const meta = catalog.find(c => c.impl === impl); if (!meta) return;
  if (edit(() => { const node = addNode(config, meta, point); selection = {kind: 'node', name: node.name}; graph.selection = selection; inspectorMode = 'node'; })) closeDrawer();
}
function deleteSelection(target = selection) {
  if (!target) return;
  edit(() => {
    if (target.kind === 'edge') disconnect(config, target.edge); else removeNode(config, target.name);
    selection = null; graph.selection = null;
  });
}

function renderPalette() {
  const query = $('#module-search').value.trim().toLowerCase();
  const matches = catalog.filter(m => `${m.impl} ${m.description || ''} ${Object.values(m.inputs || {}).join(' ')} ${Object.values(m.outputs || {}).join(' ')}`.toLowerCase().includes(query));
  $('#module-count').textContent = catalog.length;
  const groups = [['Sources', m => !Object.keys(m.inputs || {}).length], ['Processors', m => Object.keys(m.inputs || {}).length && Object.keys(m.outputs || {}).length], ['Sinks', m => Object.keys(m.inputs || {}).length && !Object.keys(m.outputs || {}).length]];
  $('#palette-list').replaceChildren();
  for (const [title, predicate] of groups) {
    const modules = matches.filter(predicate); if (!modules.length) continue;
    const section = el('section', {class: 'module-group'}, el('h3', {}, title, el('span', {}, modules.length)));
    for (const m of modules) {
      const mock = /mock/i.test(m.impl) || m.mock === true || m.is_mock === true || m.tags?.includes('mock');
      const button = el('button', {class: `module${m.error ? ' unavailable' : ''}`, draggable: !locked(), disabled: locked(), title: m.error || m.description || m.impl, 'data-config-control': true},
        el('span', {class: 'module-name'}, el('span', {class: 'module-glyph'}, title === 'Sources' ? '↗' : title === 'Sinks' ? '↙' : '◇'), m.impl, mock ? el('span', {class: 'mock-badge'}, 'MOCK') : null),
        el('span', {class: 'module-description'}, m.error ? `Unavailable: ${m.error}` : m.description || 'No description available'),
        el('span', {class: 'module-types'}, `${Object.values(m.inputs || {}).join(' + ') || 'source'} → ${Object.values(m.outputs || {}).join(' + ') || 'sink'}`));
      button.addEventListener('click', () => createNode(m.impl));
      button.addEventListener('dragstart', e => { if (locked()) { e.preventDefault(); return; } e.dataTransfer.setData('application/livesub-module', m.impl); e.dataTransfer.effectAllowed = 'copy'; });
      section.append(button);
    }
    $('#palette-list').append(section);
  }
  if (!matches.length) $('#palette-list').append(el('p', {class: 'empty-small'}, catalog.length ? 'No modules match your search.' : 'No modules returned by the server.'));
}
$('#module-search').addEventListener('input', renderPalette);

function field(label, control, hint) { return el('label', {class: 'field'}, el('span', {}, label), control, hint ? el('small', {class: 'muted'}, hint) : null); }
function editable(tag, id, attrs = {}) { return el(tag, {id, 'data-config-control': true, disabled: locked(), ...attrs}); }
function jsonField(id, value, rows = 7) { const input = editable('textarea', id, {rows, spellcheck: 'false', class: 'json-editor'}); input.value = JSON.stringify(value, null, 2); return input; }
function dirtyInspector() { inspectorDirty = true; const status = $('#inspector-save-state'); if (status) status.textContent = 'Unapplied edits'; }
function renderInspector() {
  inspectorDirty = false;
  const content = $('#inspector-content'); content.replaceChildren(); if (!config) { content.append(el('p', {class: 'empty-small'}, 'Waiting for bootstrap…')); return; }
  if (inspectorMode === 'settings') {
    content.append(el('div', {class: 'inspector-title'}, el('span', {class: 'eyebrow'}, 'GLOBAL'), el('h2', {}, 'Pipeline settings')), field('Settings · JSON', jsonField('settings-json', config.settings, 14), 'All keys, including unknown settings, are preserved.'),
      el('div', {class: 'inspector-save'}, el('span', {id: 'inspector-save-state', class: 'muted'}, 'Applied to draft'), editable('button', 'apply-inspector', {class: 'primary', text: 'Apply settings'})));
  } else if (selection?.kind === 'edge') {
    const e = selection.edge;
    content.append(el('div', {class: 'inspector-title'}, el('span', {class: 'eyebrow'}, 'TOPIC CONNECTION'), el('h2', {}, e.topic)), el('dl', {class: 'properties'}, el('dt', {}, 'Type'), el('dd', {}, e.type || 'Unknown'), el('dt', {}, 'Publisher'), el('dd', {}, `${e.source}.${e.output}`), el('dt', {}, 'Subscriber'), el('dd', {}, `${e.target}.${e.input}`)),
      el('p', {class: 'hint'}, 'Topics are shared buses. Disconnecting removes this input subscription from every publisher of the topic.'), editable('button', 'delete-selection', {class: 'danger wide', text: 'Disconnect topic'}));
  } else {
    const node = config.nodes.find(n => n.name === selection?.name);
    if (!node) { content.append(el('div', {class: 'inspector-empty'}, el('span', {class: 'empty-symbol'}, '⌖'), el('h2', {}, 'Inspect a node'), el('p', {}, 'Select a node or connection to configure its signal path.'), el('dl', {class: 'shortcut-list'}, el('dt', {}, 'Delete'), el('dd', {}, 'Remove selection'), el('dt', {}, 'F'), el('dd', {}, 'Fit graph'), el('dt', {}, 'Esc'), el('dd', {}, 'Cancel connection'), el('dt', {}, '⌘ / Ctrl S'), el('dd', {}, 'Save layout')))); return; }
    const meta = metadata(catalog, node);
    const name = editable('input', 'node-name', {value: node.name, autocomplete: 'off'}), enabled = editable('input', 'node-enabled', {type: 'checkbox', checked: node.enabled});
    const mode = editable('input', 'node-mode', {value: node.mode || 'default', list: 'mode-values'});
    const skip = editable('input', 'node-skip', {value: node.skip_if_finalized ?? '', placeholder: 'None'});
    content.append(el('div', {class: 'inspector-title'}, el('span', {class: 'eyebrow'}, node.impl), el('h2', {}, node.name), el('p', {}, meta.description || 'Implementation details unavailable.')),
      el('p', {class: 'error-text', hidden: !meta.error}, meta.error || ''),
      field('Node name', name), el('div', {class: 'field-row'}, field('Mode', mode), el('label', {class: 'check-label enabled-toggle'}, enabled, 'Enabled')),
      el('datalist', {id: 'mode-values'}, el('option', {value: 'default'}), el('option', {value: 'latest'})), field('Skip if finalized · topic', skip),
      field('Options · JSON', jsonField('node-options', node.options)),
      el('details', {class: 'option-reference'}, el('summary', {}, `Option reference (${(meta.options || []).length})`), ...(meta.options || []).map(o => el('div', {class: 'option-item'}, el('strong', {}, o.name, o.required ? ' *' : ''), el('small', {}, formatMessage(o.annotation || '')), el('code', {}, o.default === undefined ? 'No default' : `default: ${JSON.stringify(o.default)}`)))),
      el('details', {class: 'port-editor', open: true}, el('summary', {}, 'Port topics'), el('p', {class: 'hint'}, 'Connect sockets on the canvas or edit topic maps. A single input accepts port, port_1, port_2…'),
        field('Inputs · JSON', jsonField('node-inputs', node.inputs, 4)), field('Outputs · JSON', jsonField('node-outputs', node.outputs, 4)),
        el('div', {class: 'port-reference'}, ...['inputs', 'outputs'].flatMap(kind => Object.entries(meta[kind] || {}).map(([p, t]) => el('div', {}, el('span', {}, `${kind === 'inputs' ? 'IN' : 'OUT'} · ${p}`), el('code', {}, t)))))),
      el('div', {class: 'inspector-save'}, el('span', {id: 'inspector-save-state', class: 'muted'}, 'Applied to draft'), editable('button', 'apply-inspector', {class: 'primary', text: 'Apply changes'})),
      editable('button', 'delete-selection', {class: 'danger wide', text: 'Delete node'}));
  }
  content.querySelectorAll('input, textarea').forEach(input => input.addEventListener('input', dirtyInspector));
  $('#apply-inspector')?.addEventListener('click', () => { if (commitInspector()) notice('Changes applied to the local draft.'); });
  $('#delete-selection')?.addEventListener('click', () => deleteSelection());
  syncControls();
}
function commitInspector(render = true) {
  if (!inspectorDirty || locked()) return true;
  try {
    if (inspectorMode === 'settings') config.settings = jsonObject($('#settings-json').value, 'Settings');
    else if (selection?.kind === 'node') {
      const node = config.nodes.find(n => n.name === selection.name); if (!node) return true;
      const name = $('#node-name').value.trim(), options = jsonObject($('#node-options').value, 'Options'), inputs = jsonObject($('#node-inputs').value, 'Inputs'), outputs = jsonObject($('#node-outputs').value, 'Outputs');
      for (const value of [...Object.values(inputs), ...Object.values(outputs)]) if (typeof value !== 'string' || !value.trim()) throw new Error('Every port topic must be a nonempty string.');
      renameNode(config, node.name, name);
      Object.assign(node, {options, inputs, outputs, enabled: $('#node-enabled').checked, mode: $('#node-mode').value.trim() || 'default', skip_if_finalized: $('#node-skip').value.trim() || null});
      selection = {kind: 'node', name}; graph.selection = selection;
    }
    inspectorDirty = false; changed(render); return true;
  } catch (e) { notice(e.message, true); $('#inspector-save-state')?.classList.add('error-text'); return false; }
}
$('#settings').onclick = () => { if (!commitInspector(false)) return; inspectorMode = 'settings'; renderInspector(); };

function renderTopics() {
  if (!config) return;
  const topics = [...new Set([...textTopics(config, catalog), ...captions.values().map(r => r.topic).filter(Boolean)])].sort();
  const picker = $('#topic-picker'); picker.replaceChildren(el('p', {class: 'hint'}, 'Subscribe the monitor to text topics. Topic and overlay wiring is editable while idle.'));
  for (const topic of topics) {
    const checkbox = el('input', {type: 'checkbox', checked: config.editor.subtitle_topics.includes(topic), disabled: locked(), 'data-config-control': true});
    checkbox.addEventListener('change', () => edit(() => { const values = new Set(config.editor.subtitle_topics); checkbox.checked ? values.add(topic) : values.delete(topic); config.editor.subtitle_topics = [...values]; }));
    picker.append(el('label', {class: 'topic-option'}, checkbox, el('i', {class: 'dot text'}), topic));
  }
  if (!topics.length) picker.append(el('p', {class: 'empty-small'}, 'Wire a text output or add a topic below.'));
  const input = el('input', {placeholder: 'Text topic name', 'aria-label': 'Additional subtitle topic', disabled: locked(), 'data-config-control': true});
  const add = el('button', {disabled: locked(), 'data-config-control': true}, 'Connect');
  add.onclick = () => { const topic = input.value.trim(); if (topic) edit(() => { config.editor.subtitle_topics = [...new Set([...config.editor.subtitle_topics, topic])]; }); };
  input.onkeydown = e => { if (e.key === 'Enter') add.click(); };
  picker.append(el('div', {class: 'topic-add'}, input, add));
  $('#subtitle-topics').textContent = `⚯ Topics · ${config.editor.subtitle_topics.length}`;
}
function clearCaptions() { captions.clear(snapshot.epoch); renderSubtitles(); media?.overlays(); }
function renderSubtitles() {
  if (!config) return;
  const feed = $('#subtitle-feed'), rows = captions.values().filter(r => config.editor.subtitle_topics.includes(r.topic));
  const atBottom = feed.scrollHeight - feed.scrollTop - feed.clientHeight < 55;
  $('#caption-count').textContent = rows.length;
  if (!rows.length) {
    const label = config.editor.subtitle_topics.length ? 'Waiting for subtitle events' : 'No text topics connected';
    const hint = config.editor.subtitle_topics.length ? 'Live revisions and final captions will appear here.' : 'Open Topics to connect text outputs to this monitor.';
    if (!feed.querySelector('.empty-caption') || feed.dataset.empty !== label) { feed.replaceChildren(el('div', {class: 'empty-caption'}, label, el('span', {}, hint))); feed.dataset.empty = label; }
    return;
  }
  feed.querySelector('.empty-caption')?.remove();
  const existing = new Map([...feed.children].map(n => [n.dataset.key, n])), keep = new Set();
  for (const row of rows) {
    const key = JSON.stringify([row.topic, row.node, row.source, row.segment_id]); keep.add(key);
    let element = existing.get(key);
    if (!element) { element = el('article', {class: 'caption-row', dataset: {key}}, el('div', {class: 'caption-meta'}), el('p', {class: 'caption-text'}), el('div', {class: 'caption-timing'})); feed.append(element); }
    const signature = JSON.stringify(row); if (element.signature === signature) continue; element.signature = signature;
    element.classList.toggle('interim', !row.is_final);
    element.querySelector('.caption-meta').replaceChildren(el('span', {class: 'topic-chip'}, row.topic), el('span', {}, row.lang || '—'), el('span', {class: row.is_final ? 'final-tag' : 'interim-tag'}, row.is_final ? 'FINAL' : 'LIVE'), el('span', {class: 'muted'}, `#${row.segment_id ?? '—'} · r${row.revision ?? 0}`));
    element.querySelector('.caption-text').textContent = row.text || '';
    const stage = row.stage_latency_ms;
    element.querySelector('.caption-timing').textContent = `${row.node || '—'} · end-to-end ${ms(row.end_to_end_ms)}${Number.isFinite(stage) ? ` · stage ${ms(stage)}` : ''}${Number.isFinite(row.media_start) ? ` · ${row.media_start.toFixed(2)}–${Number.isFinite(row.media_end) ? row.media_end.toFixed(2) : '…'}s` : ''}`;
    element.title = stage && typeof stage === 'object' ? `Stage latency: ${JSON.stringify(stage)}` : '';
  }
  for (const [key, node] of existing) if (!keep.has(key)) node.remove();
  if (atBottom) feed.scrollTop = feed.scrollHeight;
}

function renderDiagnostics() {
  const container = $('#diagnostic-content'); if (!container) return;
  const openNodes = new Set([...container.querySelectorAll('.startup-node[open]')].map(n => n.dataset.node));
  const scroll = container.scrollTop, atBottom = container.scrollHeight - scroll - container.clientHeight < 35;
  container.replaceChildren();
  const logs = [...(snapshot.logs || []), ...localLogs].sort((a, b) => String(a.time).localeCompare(String(b.time))).slice(-500);
  $('#log-count').textContent = logs.length;
  if (diagnosticTab === 'startup') {
    const nodes = Object.entries(snapshot.nodes || {});
    const ready = nodes.filter(([, n]) => ['running', 'ready', 'completed', 'done'].includes(n.state)).length;
    container.append(el('div', {class: 'startup-summary'}, el('strong', {}, `${ready} / ${nodes.length}`), el('span', {}, 'stages ready'), el('span', {class: 'state'}, snapshot.state)));
    if (!nodes.length) container.append(el('p', {class: 'empty-small'}, 'Startup status will appear when the runtime reports it.'));
    for (const [name, n] of nodes) {
      const queues = nodeQueues(n.queues).map(q => `${q.topic}: ${q.depth ?? '—'} queued, ${q.dropped ?? '—'} dropped`).join('\n');
      const startup = n.startup, progress = startup?.progress;
      container.append(el('details', {class: 'startup-node', open: openNodes.has(name), dataset: {state: n.state || 'idle', node: name}}, el('summary', {}, el('span', {class: 'node-state-dot'}), el('strong', {}, name), el('span', {}, startup?.phase && n.state === 'starting' ? startup.phase : n.state || 'unknown')),
        n.message || startup?.message ? el('p', {class: 'startup-message'}, startup?.message || n.message) : null,
        progress ? el('p', {class: 'mono muted'}, `${progress.current ?? '—'} / ${progress.total ?? '—'} ${progress.unit || ''}`) : null,
        el('p', {class: 'mono muted'}, `Processed ${number(n.processed)} · mean ${ms(n.mean_ms)} · p95 ${ms(n.p95_ms)}`),
        queues ? el('pre', {class: 'queue-details'}, queues) : null, el('pre', {class: 'raw-data'}, JSON.stringify(n, null, 2))));
    }
    if (snapshot.error) container.append(el('p', {class: 'error-text'}, snapshot.error));
  } else if (diagnosticTab === 'profiling') {
    const stages = Object.entries(snapshot.metrics?.stages || {});
    const table = el('table', {class: 'profile-table'}, el('thead', {}, el('tr', {}, ...['Stage', 'n', 'p50', 'p95', 'mean'].map(t => el('th', {}, t)))));
    table.append(el('tbody', {}, ...stages.map(([name, s]) => el('tr', {}, el('td', {title: name}, name), el('td', {}, number(s.n)), ...[s.p50, s.p95, s.mean].map(v => el('td', {}, Number.isFinite(v) ? v.toFixed(1) : '—'))))));
    container.append(el('p', {class: 'eyebrow'}, 'STAGE LATENCY · MILLISECONDS'), table);
    if (!stages.length) container.append(el('p', {class: 'empty-small'}, 'No profiling samples received.'));
    const e2e = snapshot.metrics?.end_to_end;
    container.append(el('p', {class: 'eyebrow'}, 'END TO END'), el('pre', {class: 'raw-data'}, e2e && Object.keys(e2e).length ? JSON.stringify(e2e, null, 2) : 'No measurements yet.'));
  } else {
    if (!logs.length) container.append(el('p', {class: 'empty-small'}, 'No runtime logs yet.'));
    for (const item of logs) {
      const time = typeof item.time === 'number' ? new Date(item.time < 1e12 ? item.time * 1000 : item.time).toLocaleTimeString() : String(item.time || '').replace(/^.*T/, '').slice(0, 12);
      container.append(el('div', {class: `log-row ${String(item.level || 'info').toLowerCase()}`}, el('div', {}, el('time', {}, time), el('b', {}, item.level || 'info'), el('span', {}, `${item.client ? 'client/' : ''}${item.node || 'runtime'}`)), el('p', {}, formatMessage(item.message ?? ''))));
    }
  }
  container.scrollTop = diagnosticTab === 'logs' && atBottom ? container.scrollHeight : scroll;
}
document.querySelectorAll('[data-tab]').forEach(button => button.onclick = () => { diagnosticTab = button.dataset.tab; document.querySelectorAll('[data-tab]').forEach(b => b.setAttribute('aria-selected', String(b === button))); renderDiagnostics(); });
$('#diagnostics-download').onclick = () => download(JSON.stringify({downloaded_at: new Date().toISOString(), snapshot, client_logs: localLogs, draft: config}, null, 2), 'livesub-diagnostics.json', 'application/json');
$('#diagnostics-dock').onclick = () => { const docked = $('#studio').classList.toggle('diagnostics-bottom'); $('#diagnostics-dock').title = docked ? 'Dock diagnostics at right' : 'Dock diagnostics at bottom'; graph.draw(); };

function closeDrawer() {
  $('#studio').classList.remove('drawer-library', 'drawer-inspector', 'drawer-diagnostics');
  $('#drawer-backdrop').hidden = true;
  document.querySelectorAll('[data-drawer]').forEach(b => b.setAttribute('aria-expanded', 'false'));
}
document.querySelectorAll('[data-drawer]').forEach(button => button.onclick = () => {
  const open = button.getAttribute('aria-expanded') !== 'true'; closeDrawer();
  if (open) { $('#studio').classList.add(`drawer-${button.dataset.drawer}`); $('#drawer-backdrop').hidden = false; button.setAttribute('aria-expanded', 'true'); }
});
$('#drawer-backdrop').onclick = closeDrawer;
window.matchMedia('(max-width: 1000px)').addEventListener('change', closeDrawer);

function syncControls() {
  const lock = locked();
  document.querySelectorAll('[data-config-control]').forEach(c => { c.disabled = lock; if (c.classList.contains('module')) c.draggable = !lock; });
  for (const id of ['import', 'layout', 'save-layout']) $('#' + id).disabled = lock;
  $('#export').disabled = !config || Boolean(busy); $('#validate').disabled = lock;
  $('#run').disabled = lock || !connected; $('#stop').disabled = !active() || snapshot.state === 'stopping' || busy === 'Stopping' || seekCommands > 0;
  $('#run').textContent = busy === 'Starting' || snapshot.state === 'starting' ? '◌ Starting' : '▶ Run';
  $('#runtime-state').textContent = snapshot.state; $('#runtime-state').dataset.state = snapshot.state;
  $('#epoch').textContent = `epoch ${hasSnapshot ? snapshot.epoch : '—'}`;
  $('#draft-state').textContent = active() ? 'Runtime configuration · locked' : inspectorDirty ? 'Unapplied inspector edits' : 'Local draft';
  $('#runtime-error').textContent = snapshot.error || '';
  $('#status-text').textContent = busy || (seekCommands ? 'Seeking · waiting for new generation' : active() ? 'Runtime owns the graph · media and viewport controls remain available' : config ? 'Ready · edit your pipeline, validate, then run' : 'Waiting for server');
  graph.setLocked(lock); media.setLocked();
}
function acceptSnapshot(next) {
  if (!next || typeof next !== 'object' || typeof next.state !== 'string' || !Number.isInteger(next.epoch)) { log('error', 'events', 'Ignored malformed runtime snapshot.'); return; }
  if (hasSnapshot && next.epoch < snapshot.epoch) return;
  const prior = snapshot, newEpoch = !hasSnapshot || next.epoch !== prior.epoch;
  const adopt = isActive(next.state) && (!isActive(prior.state) || newEpoch || !hasSnapshot);
  if (adopt && next.config) {
    try { config = normalizeConfig(next.config); selection = config.nodes.some(n => n.name === selection?.name) ? selection : null; graph.selection = selection; }
    catch (e) { notice(`Server configuration could not be loaded: ${e.message}`, true); }
  }
  snapshot = next; hasSnapshot = true;
  if (newEpoch || (!isActive(prior.state) && ['starting', 'running'].includes(next.state))) captions.clear(next.epoch);
  if (!pendingRun && !seekCommands && next.state !== 'seeking' && next.state !== 'starting') captions.ingest(next.epoch, next.subtitles || []);
  if (adopt) { graph.render(config, catalog); renderInspector(); renderTopics(); }
  if (isActive(prior.state) && !isActive(next.state) && next.state !== 'completed') media.stop();
  graph.metrics(next); media.snapshot(next); renderSubtitles(); renderDiagnostics(); syncControls();
}
function openEvents() {
  clearTimeout(reconnectTimer);
  const url = new URL('/api/events', location.href); url.protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
  socket = new WebSocket(url);
  socket.addEventListener('open', () => { connected = true; reconnectAttempts = 0; $('#connection').textContent = 'Live connection'; $('#connection').className = 'connection online'; syncControls(); media.clocks(); });
  socket.addEventListener('message', event => {
    try {
      const message = JSON.parse(event.data);
      if (message.type === 'snapshot') acceptSnapshot(message);
      else if (message.type === 'command_error') notice(`Runtime command failed: ${message.message}`, true);
    } catch (e) { log('error', 'events', `Invalid event: ${e.message}`); }
  });
  socket.addEventListener('close', () => {
    connected = false; $('#connection').textContent = 'Reconnecting…'; $('#connection').className = 'connection offline'; syncControls();
    reconnectTimer = setTimeout(openEvents, Math.min(10000, 800 * 2 ** Math.min(reconnectAttempts++, 4)));
  });
  socket.addEventListener('error', () => socket.close());
}

$('#run').onclick = async () => {
  if (locked() || !connected || !commitInspector()) return;
  const submitted = clone(config);
  busy = 'Starting'; pendingRun = true; clearCaptions(); syncControls(); media.startFromGesture();
  try { const next = await api('/api/run', {config: submitted}); pendingRun = false; acceptSnapshot(next); }
  catch (e) { if (!active()) media.stop(); notice(`Run failed: ${e.message}`, true); }
  finally { pendingRun = false; busy = ''; syncControls(); }
};
$('#stop').onclick = async () => {
  if (!active() || busy === 'Stopping') return;
  busy = 'Stopping'; media.stop(); syncControls();
  try { acceptSnapshot(await api('/api/stop', {})); } catch (e) { notice(`Stop failed: ${e.message}`, true); }
  finally { busy = ''; syncControls(); }
};
$('#validate').onclick = async () => {
  if (locked() || !commitInspector()) return;
  busy = 'Validating'; syncControls();
  try {
    const result = await api('/api/validate', {config}); const warnings = [...new Set([...validateLocal(config, catalog), ...(result.warnings || []).map(formatMessage)])];
    if (warnings.length) { warnings.forEach(message => log('warning', 'validation', message)); notice(`Validation: ${warnings.length} warning${warnings.length === 1 ? '' : 's'}. ${warnings.join(' · ')}`); }
    else notice('Configuration validated. No warnings.');
  } catch (e) { notice(`Validation failed: ${e.message}`, true); }
  finally { busy = ''; syncControls(); }
};
$('#import').onclick = () => { if (!locked()) { $('#import-error').textContent = ''; $('#import-dialog').showModal(); } };
$('#config-file').onchange = async e => {
  const file = e.target.files?.[0]; if (!file) return;
  try { $('#import-text').value = await file.text(); $('#import-format').value = file.name.toLowerCase().endsWith('.json') ? 'json' : 'toml'; }
  catch (error) { $('#import-error').textContent = error.message; }
  e.target.value = '';
};
$('#import-apply').onclick = async () => {
  if (locked()) { $('#import-error').textContent = 'Stop the runtime before importing.'; return; }
  busy = 'Importing'; syncControls(); $('#import-apply').disabled = true;
  try {
    const result = await api('/api/config/import', {text: $('#import-text').value, format: $('#import-format').value});
    if (active()) throw new Error('Runtime started while importing. Stop it before replacing the configuration.');
    config = normalizeConfig(result.config); selection = null; graph.selection = null; inspectorMode = 'node'; captions.clear();
    changed(); graph.fit(); $('#import-dialog').close(); notice('Configuration imported into your local draft.');
  } catch (e) { $('#import-error').textContent = e.message; }
  finally { busy = ''; $('#import-apply').disabled = false; syncControls(); }
};
$('#export').onclick = async () => {
  if (!config || busy || !commitInspector()) return;
  const format = $('#export-format').value; busy = 'Exporting'; syncControls();
  try {
    const response = await fetch('/api/config/export', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({config, format})});
    const text = await response.text(); if (!response.ok) throw new Error(text || `HTTP ${response.status}`);
    download(text, `livesub-pipeline.${format}`, format === 'json' ? 'application/json' : 'application/toml'); notice(`Exported ${format.toUpperCase()} configuration.`);
  } catch (e) { notice(`Export failed: ${e.message}`, true); }
  finally { busy = ''; syncControls(); }
};

function layoutKey() { return `livesub.layout.${JSON.stringify(config.nodes.map(n => [n.name, n.impl]).sort())}`; }
function saveLayout() {
  if (locked() || !commitInspector()) return;
  for (const [name, card] of graph.cards) config.editor.positions[name] = {x: parseFloat(card.style.left), y: parseFloat(card.style.top)};
  config.editor.viewport = {...graph.view};
  try { localStorage.setItem(layoutKey(), JSON.stringify({positions: config.editor.positions, viewport: graph.view})); notice('Layout saved in this browser and included in configuration exports.'); }
  catch { notice('Layout is included in exports. Browser storage is unavailable.'); }
}
$('#save-layout').onclick = saveLayout;
$('#layout').onclick = () => { if (edit(() => autoLayout(config, catalog))) graph.fit(); };
$('#fit').onclick = () => graph.fit(); $('#zoom-in').onclick = () => graph.zoom(1.2); $('#zoom-out').onclick = () => graph.zoom(1 / 1.2);
window.addEventListener('keydown', e => {
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's') { e.preventDefault(); saveLayout(); }
  if (e.key === 'Escape' && $('#subtitles').classList.contains('expanded')) toggleSubtitleExpand();
  if (e.key === 'Escape') closeDrawer();
});

$('#subtitle-topics').onclick = () => { $('#topic-picker').hidden = !$('#topic-picker').hidden; if (!$('#topic-picker').hidden) renderTopics(); };
$('#subtitle-collapse').onclick = () => {
  const collapsed = $('#subtitles').classList.toggle('collapsed'); $('#subtitle-collapse').textContent = collapsed ? '+' : '−'; $('#subtitle-collapse').setAttribute('aria-expanded', String(!collapsed)); $('#subtitle-collapse').setAttribute('aria-label', collapsed ? 'Expand subtitles' : 'Collapse subtitles');
  if (collapsed) { $('#subtitles').classList.remove('expanded'); $('#studio').classList.add('subtitles-collapsed'); } else $('#studio').classList.remove('subtitles-collapsed');
};
function toggleSubtitleExpand() {
  const expanded = $('#subtitles').classList.toggle('expanded'); $('#subtitles').classList.remove('collapsed'); $('#studio').classList.remove('subtitles-collapsed');
  $('#subtitle-collapse').textContent = '−'; $('#subtitle-collapse').setAttribute('aria-expanded', 'true'); $('#subtitle-expand').setAttribute('aria-label', expanded ? 'Restore subtitle panel' : 'Expand subtitles to full viewport'); $('#subtitle-expand').textContent = expanded ? '↙' : '⛶';
}
$('#subtitle-expand').onclick = toggleSubtitleExpand;
const resizer = $('#subtitle-resizer');
resizer.onpointerdown = e => {
  if ($('#subtitles').classList.contains('expanded')) return;
  e.preventDefault(); const initialY = e.clientY, height = $('#subtitles').getBoundingClientRect().height;
  resizer.setPointerCapture(e.pointerId);
  const move = event => { $('#subtitles').classList.remove('collapsed'); $('#studio').classList.remove('subtitles-collapsed'); setSubtitleHeight(height + initialY - event.clientY); };
  const end = () => { resizer.removeEventListener('pointermove', move); resizer.removeEventListener('pointerup', end); resizer.removeEventListener('pointercancel', end); };
  resizer.addEventListener('pointermove', move); resizer.addEventListener('pointerup', end); resizer.addEventListener('pointercancel', end);
};
function setSubtitleHeight(height) { $('#studio').style.setProperty('--subtitle-height', `${Math.max(100, Math.min(window.innerHeight * .75, height))}px`); }
resizer.onkeydown = e => { if (['ArrowUp', 'ArrowDown'].includes(e.key)) { e.preventDefault(); setSubtitleHeight($('#subtitles').offsetHeight + (e.key === 'ArrowUp' ? 25 : -25)); } };

async function bootstrap() {
  busy = 'Loading catalog'; syncControls();
  try {
    const data = await api('/api/bootstrap'); catalog = Array.isArray(data.catalog) ? data.catalog : []; config = normalizeConfig(data.config);
    let view = config.editor.viewport;
    if (!isActive(data.snapshot?.state)) {
      try { const saved = JSON.parse(localStorage.getItem(layoutKey()) || 'null'); if (saved) { config.editor.positions = {...config.editor.positions, ...saved.positions}; view = saved.viewport; } } catch { /* Storage may be disabled. */ }
    }
    if (view && [view.x, view.y, view.zoom].every(Number.isFinite) && view.zoom >= .2 && view.zoom <= 2) graph.view = view;
    graph.render(config, catalog); renderPalette(); renderInspector(); renderTopics();
    acceptSnapshot(data.snapshot); if (!view) requestAnimationFrame(() => graph.fit());
    openEvents();
  } catch (e) {
    notice(`Could not load Livesub: ${e.message}`, true); $('#connection').textContent = 'Server unavailable'; $('#connection').className = 'connection offline';
    const retry = el('button', {class: 'retry-button', onclick: () => { retry.remove(); bootstrap(); }}, 'Retry connection'); $('#inspector-content').replaceChildren(el('p', {class: 'empty-small'}, 'The workbench requires /api/bootstrap. Check that the Livesub server is running.'), retry);
  } finally { busy = ''; syncControls(); }
}
bootstrap();
