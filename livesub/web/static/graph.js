import {el, ms, number} from './dom.js';
import {metadata, connections, typeClass, nodeQueues} from './model.mjs';

const NS = 'http://www.w3.org/2000/svg';
const path = (a, b) => { const bend = Math.max(65, Math.abs(b.x - a.x) * .48); return `M ${a.x} ${a.y} C ${a.x + bend} ${a.y}, ${b.x - bend} ${b.y}, ${b.x} ${b.y}`; };
const svg = (tag, attrs) => { const n = document.createElementNS(NS, tag); for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v); return n; };

export class Graph {
  constructor(host, hooks) {
    this.host = host; this.hooks = hooks; this.world = host.querySelector('#graph-world');
    this.nodes = host.querySelector('#nodes'); this.svg = host.querySelector('#edges');
    this.cards = new Map(); this.view = {x: 40, y: 35, zoom: .85}; this.selection = null; this.wire = null;
    this.host.addEventListener('pointerdown', e => this.pointerDown(e));
    this.host.addEventListener('wheel', e => {
      if (e.target.closest('video, select, textarea, .media-controls')) return;
      e.preventDefault(); this.zoomAt(e.clientX, e.clientY, this.view.zoom * Math.exp(-e.deltaY * .0015));
    }, {passive: false});
    this.host.addEventListener('dragover', e => { if (!hooks.locked()) { e.preventDefault(); e.dataTransfer.dropEffect = 'copy'; } });
    this.host.addEventListener('drop', e => {
      e.preventDefault(); if (hooks.locked()) return;
      const impl = e.dataTransfer.getData('application/livesub-module'); if (impl) hooks.add(impl, this.point(e.clientX, e.clientY));
    });
    window.addEventListener('keydown', e => this.key(e));
    this.observer = new ResizeObserver(() => this.draw()); this.observer.observe(host);
  }
  point(x, y) { const r = this.host.getBoundingClientRect(); return {x: (x - r.left - this.view.x) / this.view.zoom, y: (y - r.top - this.view.y) / this.view.zoom}; }
  center() { const r = this.host.getBoundingClientRect(); return this.point(r.left + r.width / 2 - 140, r.top + r.height / 2 - 100); }
  transform() {
    const {x, y, zoom} = this.view; this.world.style.transform = `translate(${x}px, ${y}px) scale(${zoom})`;
    this.host.style.backgroundPosition = `${x}px ${y}px`; this.host.style.backgroundSize = `${24 * zoom}px ${24 * zoom}px`;
    document.querySelector('#zoom-label').textContent = `${Math.round(zoom * 100)}%`;
  }
  zoomAt(x, y, zoom) { const p = this.point(x, y), r = this.host.getBoundingClientRect(); this.view.zoom = Math.max(.2, Math.min(2, zoom)); this.view.x = x - r.left - p.x * this.view.zoom; this.view.y = y - r.top - p.y * this.view.zoom; this.transform(); }
  zoom(factor) { const r = this.host.getBoundingClientRect(); this.zoomAt(r.left + r.width / 2, r.top + r.height / 2, this.view.zoom * factor); }
  fit() {
    if (!this.cards.size) { this.view = {x: 40, y: 35, zoom: .85}; this.transform(); return; }
    const bounds = [...this.cards.values()].map(c => ({x: parseFloat(c.style.left), y: parseFloat(c.style.top), w: c.offsetWidth, h: c.offsetHeight}));
    const left = Math.min(...bounds.map(b => b.x)), top = Math.min(...bounds.map(b => b.y));
    const width = Math.max(...bounds.map(b => b.x + b.w)) - left, height = Math.max(...bounds.map(b => b.y + b.h)) - top;
    const zoom = Math.max(.2, Math.min(1, (this.host.clientWidth - 100) / width, (this.host.clientHeight - 100) / height));
    this.view = {zoom, x: (this.host.clientWidth - width * zoom) / 2 - left * zoom, y: (this.host.clientHeight - height * zoom) / 2 - top * zoom}; this.transform();
  }
  select(selection) {
    this.selection = selection;
    for (const [name, c] of this.cards) c.classList.toggle('selected', selection?.kind === 'node' && name === selection.name);
    this.draw(); this.hooks.select(selection);
  }
  render(config, catalog) {
    this.config = config; this.catalog = catalog;
    const names = new Set(config.nodes.map(n => n.name));
    for (const [name, card] of this.cards) if (!names.has(name)) { this.hooks.removeMedia(name); this.observer.unobserve(card); card.remove(); this.cards.delete(name); }
    config.nodes.forEach((node, i) => {
      const meta = metadata(catalog, node), mock = /mock/i.test(node.impl) || meta.mock === true || meta.is_mock === true || meta.tags?.includes('mock');
      let card = this.cards.get(node.name);
      if (!card) {
        card = el('article', {class: 'node-card', dataset: {node: node.name}, tabindex: '0', 'aria-label': `${node.name} node`},
          el('div', {class: 'node-header'}, el('span', {class: 'node-icon'}, '◈'), el('div', {class: 'node-title'}, el('strong'), el('small')), el('span', {class: 'mock-badge', hidden: !mock}, 'MOCK'), el('span', {class: 'node-state-dot'})),
          el('div', {class: 'node-ports'}), el('div', {class: 'node-live'}, el('div', {class: 'node-status'}, el('span', {class: 'status-label'}, 'idle'), el('span', {class: 'processed mono'}, '—')),
            el('div', {class: 'node-message', hidden: true}), el('div', {class: 'node-metrics'}, el('span', {class: 'mean'}, 'mean —'), el('span', {class: 'p95'}, 'p95 —')),
            el('div', {class: 'mic-meter', hidden: true}, el('div')), el('div', {class: 'queue-list'})), el('div', {class: 'node-media'}));
        this.cards.set(node.name, card); this.nodes.append(card); this.observer.observe(card);
      }
      card.querySelector('.node-title strong').textContent = node.name; card.querySelector('.node-title small').textContent = node.impl;
      card.classList.toggle('disabled-node', !node.enabled); card.classList.toggle('selected', this.selection?.kind === 'node' && this.selection.name === node.name);
      card.querySelector('.mock-badge').hidden = !mock;
      const position = config.editor.positions[node.name] || {x: 70 + i * 350, y: 80};
      card.style.left = `${Number.isFinite(position.x) ? position.x : 70}px`; card.style.top = `${Number.isFinite(position.y) ? position.y : 80}px`;
      const signature = JSON.stringify([meta.inputs, meta.outputs, node.inputs, node.outputs]);
      if (card.portSignature !== signature) {
        card.portSignature = signature; const ports = card.querySelector('.node-ports'); ports.replaceChildren();
        for (const direction of ['input', 'output']) {
          const column = el('div', {class: `port-column ${direction}`});
          for (const [port, type] of Object.entries(meta[direction + 's'] || {})) {
            const topics = direction === 'output' ? [node.outputs[port]].filter(Boolean) : Object.entries(node.inputs).filter(([k]) => k === port || (Object.keys(meta.inputs).length === 1 && k.startsWith(port + '_'))).map(([, t]) => t);
            const socket = el('button', {class: `socket ${typeClass(type)}`, 'aria-label': `${node.name} ${direction} ${port}: ${type}`, title: `${type} · ${topics.join(', ') || 'unconnected'}`, dataset: {node: node.name, direction, port, type}});
            const label = el('span', {class: 'port-label'}, port, el('small', {}, topics.length > 1 ? `${topics.length} topics` : topics[0] || type));
            column.append(el('div', {class: 'port-row'}, ...(direction === 'input' ? [socket, label] : [label, socket])));
          }
          ports.append(column);
        }
      }
      this.hooks.media(node, card.querySelector('.node-media'));
    });
    document.querySelector('#empty-graph').hidden = !!config.nodes.length;
    document.querySelector('#graph-count').textContent = `${config.nodes.length} nodes / ${connections(config, catalog).length} links`;
    this.transform(); this.draw(); this.setLocked(this.hooks.locked());
  }
  setLocked(locked) { this.host.classList.toggle('locked', locked); document.querySelector('#graph-lock').hidden = !locked; if (locked && this.wire) this.cancelWire(); }
  metrics(snapshot) {
    for (const [name, card] of this.cards) {
      const n = snapshot.nodes?.[name] || {}, state = n.state || 'idle';
      card.dataset.state = state; card.querySelector('.status-label').textContent = state;
      card.querySelector('.processed').textContent = `${number(n.processed)} processed`;
      card.querySelector('.mean').textContent = `mean ${ms(n.mean_ms)}`; card.querySelector('.p95').textContent = `p95 ${ms(n.p95_ms)}`;
      const progress = n.startup?.progress;
      const detail = [n.startup?.message || n.message, progress ? `${progress.current ?? '—'} / ${progress.total ?? '—'} ${progress.unit || ''}` : ''].filter(Boolean).join(' · ');
      const message = card.querySelector('.node-message'); message.textContent = detail; message.hidden = !detail;
      const queues = card.querySelector('.queue-list'); queues.replaceChildren(...nodeQueues(n.queues).map(q => el('div', {class: 'queue-row', title: q.topic}, el('span', {}, q.topic), el('b', {class: q.dropped ? 'warning-text' : ''}, `${number(q.depth)} queued${q.dropped ? ` · ${q.dropped} dropped` : ''}`))));
      const db = n.level_dbfs;
      const level = Number.isFinite(db) ? (db + 60) / 60 : n.mic_level ?? n.audio_level ?? n.level ?? n.rms;
      const meter = card.querySelector('.mic-meter'); meter.hidden = !Number.isFinite(level);
      if (!meter.hidden) { const value = Math.max(0, Math.min(1, level)); meter.firstChild.style.width = `${value * 100}%`; meter.title = Number.isFinite(db) ? `Input level: ${db.toFixed(1)} dBFS` : `Input level: ${level.toFixed(3)}`; }
    }
  }
  socketPoint(endpoint) {
    const card = this.cards.get(endpoint.node); if (!card) return null;
    const socket = [...card.querySelectorAll('.socket')].find(s => s.dataset.direction === endpoint.direction && s.dataset.port === endpoint.port);
    if (!socket) return null;
    const rect = socket.getBoundingClientRect(); return this.point(rect.left + rect.width / 2, rect.top + rect.height / 2);
  }
  draw() {
    if (!this.config) return;
    this.svg.replaceChildren();
    for (const edge of connections(this.config, this.catalog)) {
      const a = this.socketPoint({node: edge.source, port: edge.output, direction: 'output'}), b = this.socketPoint({node: edge.target, port: edge.port, direction: 'input'});
      if (!a || !b) continue;
      const g = svg('g', {class: `edge ${typeClass(edge.type)}${this.selection?.kind === 'edge' && this.selection.edge.id === edge.id ? ' selected' : ''}`});
      const title = svg('title', {}); title.textContent = `${edge.topic} · ${edge.source} → ${edge.target} (Delete removes this input's topic subscription)`;
      const hit = svg('path', {d: path(a, b), class: 'edge-hit', tabindex: '0', role: 'button', 'aria-label': title.textContent});
      hit.addEventListener('pointerdown', e => { e.stopPropagation(); this.select({kind: 'edge', edge}); });
      hit.addEventListener('keydown', e => { if (e.key === 'Enter') this.select({kind: 'edge', edge}); });
      g.append(title, hit, svg('path', {d: path(a, b), class: 'edge-line'})); this.svg.append(g);
    }
    if (this.wire && this.wirePoint) {
      const point = this.socketPoint(this.wire);
      if (point) this.svg.append(svg('path', {class: 'wire-preview', d: this.wire.direction === 'output' ? path(point, this.wirePoint) : path(this.wirePoint, point)}));
    }
  }
  cancelWire() { this.wire = null; this.wirePoint = null; this.host.classList.remove('wiring'); for (const c of this.cards.values()) c.querySelectorAll('.socket').forEach(s => s.classList.remove('connecting', 'compatible')); this.draw(); }
  startWire(socket) {
    this.wire = {...socket.dataset}; this.wirePoint = this.socketPoint(this.wire); this.host.classList.add('wiring'); socket.classList.add('connecting');
    for (const c of this.cards.values()) for (const s of c.querySelectorAll('.socket')) s.classList.toggle('compatible', s.dataset.type === this.wire.type && s.dataset.direction !== this.wire.direction && s.dataset.node !== this.wire.node);
    this.draw();
  }
  finishWire(socket) { if (!this.wire || this.hooks.locked()) return; const a = this.wire, b = {...socket.dataset}; this.cancelWire(); if (a.node === b.node && a.port === b.port && a.direction === b.direction) return; this.hooks.connect(a, b); }
  pointerDown(e) {
    if (e.button !== 0 && e.button !== 1) return;
    const socket = e.target.closest('.socket');
    if (socket && e.button === 0) {
      e.preventDefault(); if (this.hooks.locked()) return;
      if (this.wire) { this.finishWire(socket); return; }
      this.startWire(socket); const start = {x: e.clientX, y: e.clientY}; let moved = false;
      const move = ev => { moved ||= Math.hypot(ev.clientX - start.x, ev.clientY - start.y) > 5; this.wirePoint = this.point(ev.clientX, ev.clientY); this.draw(); };
      const up = ev => { window.removeEventListener('pointermove', move); window.removeEventListener('pointerup', up); window.removeEventListener('pointercancel', cancel); if (moved) { const target = document.elementFromPoint(ev.clientX, ev.clientY)?.closest('.socket'); if (target) this.finishWire(target); else this.cancelWire(); } };
      const cancel = () => { window.removeEventListener('pointermove', move); window.removeEventListener('pointerup', up); window.removeEventListener('pointercancel', cancel); this.cancelWire(); };
      window.addEventListener('pointermove', move); window.addEventListener('pointerup', up); window.addEventListener('pointercancel', cancel); return;
    }
    if (e.target.closest('video, button, input, select, textarea, .node-media, a')) return;
    const card = e.target.closest('.node-card');
    if (card && e.button === 0) {
      this.select({kind: 'node', name: card.dataset.node}); if (this.hooks.locked()) return;
      e.preventDefault(); const start = this.point(e.clientX, e.clientY), initial = {x: parseFloat(card.style.left), y: parseFloat(card.style.top)}; let moved = false;
      this.track(e, ev => {
        if (this.hooks.locked()) return;
        const p = this.point(ev.clientX, ev.clientY); const x = initial.x + p.x - start.x, y = initial.y + p.y - start.y;
        card.style.left = `${x}px`; card.style.top = `${y}px`; this.config.editor.positions[card.dataset.node] = {x, y}; moved = true; this.draw();
      }, () => { if (moved) this.hooks.changed(false); });
    } else {
      e.preventDefault(); this.host.focus({preventScroll: true});
      if (this.wire) this.cancelWire(); else if (e.button === 0) this.select(null);
      const initial = {...this.view}, start = {x: e.clientX, y: e.clientY}; this.host.classList.add('panning');
      this.track(e, ev => { this.view.x = initial.x + ev.clientX - start.x; this.view.y = initial.y + ev.clientY - start.y; this.transform(); }, () => this.host.classList.remove('panning'));
    }
  }
  track(e, move, finish) {
    this.host.setPointerCapture(e.pointerId);
    const end = () => { this.host.removeEventListener('pointermove', move); this.host.removeEventListener('pointerup', end); this.host.removeEventListener('pointercancel', end); if (this.host.hasPointerCapture(e.pointerId)) this.host.releasePointerCapture(e.pointerId); finish(); };
    this.host.addEventListener('pointermove', move); this.host.addEventListener('pointerup', end); this.host.addEventListener('pointercancel', end);
  }
  key(e) {
    if (e.target.closest('input, textarea, select, [contenteditable], video, dialog') || e.ctrlKey || e.metaKey || e.altKey) return;
    if (e.key === 'Escape') { this.cancelWire(); return; }
    if (e.key.toLowerCase() === 'f' && !e.target.closest('button')) { e.preventDefault(); this.fit(); }
    if (e.key === 'Enter' && e.target.matches('.socket') && !this.hooks.locked()) { e.preventDefault(); if (this.wire) this.finishWire(e.target); else this.startWire(e.target); }
    if (this.hooks.locked()) return;
    if ((e.key === 'Delete' || e.key === 'Backspace') && this.selection) { e.preventDefault(); this.hooks.delete(this.selection); }
    if (this.selection?.kind === 'node' && ['ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight'].includes(e.key) && !e.target.closest('button')) {
      e.preventDefault(); const card = this.cards.get(this.selection.name), step = e.shiftKey ? 50 : 10;
      const p = {x: parseFloat(card.style.left), y: parseFloat(card.style.top)};
      p.x += e.key === 'ArrowRight' ? step : e.key === 'ArrowLeft' ? -step : 0; p.y += e.key === 'ArrowDown' ? step : e.key === 'ArrowUp' ? -step : 0;
      this.config.editor.positions[this.selection.name] = p; card.style.left = `${p.x}px`; card.style.top = `${p.y}px`; this.draw(); this.hooks.changed(false);
    }
  }
}
