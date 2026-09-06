export const ACTIVE = new Set(['starting', 'running', 'seeking', 'stopping']);
export const clone = value => structuredClone(value);
export const isActive = state => ACTIVE.has(state);
export function normalizeConfig(value = {}) {
  const c = clone(value);
  if (!Array.isArray(c.nodes)) throw new Error('Config must contain a nodes array.');
  const names = new Set();
  c.nodes = c.nodes.map(n => {
    if (!n.name || !n.impl || names.has(n.name)) throw new Error('Nodes need unique names and an implementation.');
    names.add(n.name);
    return {inputs: {}, outputs: {}, options: {}, mode: 'default', skip_if_finalized: null, enabled: true, ...n};
  });
  c.settings ??= {};
  c.editor = {positions: {}, subtitle_topics: [], overlays: {}, auto_pause: {}, ...c.editor};
  return c;
}
export const metadata = (catalog, node) => catalog.find(c => c.impl === node.impl) || {inputs: {}, outputs: {}, options: []};
export function basePort(key, ports) {
  if (Object.hasOwn(ports, key)) return key;
  return Object.keys(ports).find(p => key.startsWith(p + '_') && /^\d+$/.test(key.slice(p.length + 1))) || key;
}
export function typeClass(type = '') {
  return /utterance/i.test(type) ? 'utterance' : /audio/i.test(type) ? 'audio' : /text/i.test(type) ? 'text' : 'generic';
}
export const compatible = (a, b) => Boolean(a && b && a === b);
export function connections(config, catalog) {
  const result = [];
  for (const target of config.nodes) for (const [key, topic] of Object.entries(target.inputs)) {
    const port = basePort(key, metadata(catalog, target).inputs || {});
    for (const source of config.nodes) for (const [out, value] of Object.entries(source.outputs)) {
      if (topic && value === topic) result.push({id: JSON.stringify([source.name, out, target.name, key]), source: source.name, output: out, target: target.name, input: key, port, topic, type: metadata(catalog, source).outputs?.[out] || ''});
    }
  }
  return result;
}
export function connect(config, catalog, a, b) {
  if (a.direction === b.direction) throw new Error('Connect an output to an input.');
  const from = a.direction === 'output' ? a : b, to = a.direction === 'input' ? a : b;
  if (from.node === to.node) throw new Error('A node cannot connect to itself.');
  const source = config.nodes.find(n => n.name === from.node), target = config.nodes.find(n => n.name === to.node);
  if (!source || !target) throw new Error('Node no longer exists.');
  const sm = metadata(catalog, source), tm = metadata(catalog, target);
  if (!compatible(sm.outputs?.[from.port], tm.inputs?.[to.port])) throw new Error('Socket types must match exactly.');
  let topic = source.outputs[from.port];
  if (!topic) {
    const used = new Set(config.nodes.flatMap(n => Object.values(n.outputs)));
    const stem = `${source.name}.${from.port}`;
    topic = stem;
    for (let i = 2; used.has(topic); i++) topic = `${stem}.${i}`;
    source.outputs[from.port] = topic;
  }
  if (Object.entries(target.inputs).some(([k, v]) => basePort(k, tm.inputs) === to.port && v === topic)) return topic;
  let key = to.port;
  if (target.inputs[key] && Object.keys(tm.inputs).length !== 1) throw new Error('This input is occupied. Disconnect it before connecting another topic.');
  for (let i = 1; target.inputs[key]; i++) key = `${to.port}_${i}`;
  target.inputs[key] = topic;
  return topic;
}
export function disconnect(config, edge) {
  const n = config.nodes.find(n => n.name === edge.target);
  if (n) delete n.inputs[edge.input];
}
export function removeNode(config, name) {
  const removed = config.nodes.find(n => n.name === name);
  config.nodes = config.nodes.filter(n => n.name !== name);
  const remaining = new Set(config.nodes.flatMap(n => Object.values(n.outputs)));
  const orphaned = new Set(Object.values(removed?.outputs || {}).filter(t => !remaining.has(t)));
  for (const n of config.nodes) for (const [k, v] of Object.entries(n.inputs)) if (orphaned.has(v)) delete n.inputs[k];
  delete config.editor.positions[name]; delete config.editor.overlays[name]; delete config.editor.auto_pause[name];
  config.editor.subtitle_topics = config.editor.subtitle_topics.filter(t => !orphaned.has(t));
  for (const [k, v] of Object.entries(config.editor.overlays)) if (orphaned.has(v)) delete config.editor.overlays[k];
}
export function renameNode(config, oldName, newName) {
  if (!newName.trim() || config.nodes.some(n => n.name === newName && n.name !== oldName)) throw new Error('Choose a nonempty, unique node name.');
  const node = config.nodes.find(n => n.name === oldName);
  if (!node) throw new Error('Node no longer exists.');
  node.name = newName;
  for (const key of ['positions', 'overlays', 'auto_pause']) {
    if (Object.hasOwn(config.editor[key], oldName)) { config.editor[key][newName] = config.editor[key][oldName]; if (oldName !== newName) delete config.editor[key][oldName]; }
  }
}
export function addNode(config, meta, position) {
  const stem = meta.impl.split('.').pop().replace(/[^\w-]/g, '_');
  let name = stem;
  for (let i = 2; config.nodes.some(n => n.name === name); i++) name = `${stem}_${i}`;
  const options = {};
  for (const o of meta.options || []) if (o.default !== undefined && o.default !== null) options[o.name] = clone(o.default);
  const node = {name, impl: meta.impl, inputs: {}, outputs: {}, options, mode: 'default', enabled: true, skip_if_finalized: null};
  const used = new Set(config.nodes.flatMap(n => Object.values(n.outputs)));
  for (const port of Object.keys(meta.outputs || {})) {
    const stem = `${name}.${port}`; let topic = stem;
    for (let i = 2; used.has(topic); i++) topic = `${stem}.${i}`;
    node.outputs[port] = topic; used.add(topic);
  }
  config.nodes.push(node); config.editor.positions[name] = position;
  return node;
}
export function textTopics(config, catalog) {
  const topics = new Set(config.editor.subtitle_topics);
  for (const n of config.nodes) for (const [p, t] of Object.entries(n.outputs)) if (typeClass(metadata(catalog, n).outputs?.[p]) === 'text' && t) topics.add(t);
  return [...topics].sort();
}
export function autoLayout(config, catalog) {
  const edges = connections(config, catalog), levels = new Map(), visiting = new Set();
  function level(name) {
    if (levels.has(name)) return levels.get(name);
    if (visiting.has(name)) return 0;
    visiting.add(name);
    const parents = edges.filter(e => e.target === name && !visiting.has(e.source));
    const value = parents.length ? Math.max(...parents.map(e => level(e.source) + 1)) : 0;
    visiting.delete(name); levels.set(name, value); return value;
  }
  const rows = {};
  for (const n of config.nodes) { const col = level(n.name); const row = rows[col] || 0; config.editor.positions[n.name] = {x: 70 + col * 350, y: 80 + row * 470}; rows[col] = row + 1; }
  return config.editor.positions;
}
export function validateLocal(config, catalog) {
  const issues = [], names = new Set(), producers = new Map();
  for (const n of config.nodes) {
    if (names.has(n.name)) issues.push(`Duplicate node: ${n.name}`); names.add(n.name);
    const meta = catalog.find(c => c.impl === n.impl);
    if (!meta) { issues.push(`${n.name}: implementation is missing from catalog`); continue; }
    if (meta.error && n.enabled) issues.push(`${n.name}: ${meta.error}`);
    if (!n.enabled) continue;
    for (const o of meta.options || []) if (o.required && (n.options[o.name] === undefined || n.options[o.name] === null)) issues.push(`${n.name}: required option ${o.name} is missing`);
    for (const [key, topic] of Object.entries(n.outputs)) {
      const type = meta.outputs?.[key];
      if (!type) issues.push(`${n.name}: unknown output ${key}`);
      if (producers.has(topic) && producers.get(topic) !== type) issues.push(`${topic}: incompatible publishers`);
      producers.set(topic, type);
    }
  }
  for (const n of config.nodes.filter(n => n.enabled)) {
    const ports = metadata(catalog, n).inputs || {};
    for (const [key, topic] of Object.entries(n.inputs)) {
      const type = ports[basePort(key, ports)];
      if (!type) issues.push(`${n.name}: unknown input ${key}`);
      if (!producers.has(topic)) issues.push(`${n.name}: ${topic} has no enabled publisher`);
      else if (!compatible(type, producers.get(topic))) issues.push(`${n.name}: incompatible type on ${key}`);
    }
  }
  return [...new Set(issues)];
}
export class CaptionStore {
  constructor() { this.epoch = null; this.rows = new Map(); }
  clear(epoch = this.epoch) { this.epoch = epoch; this.rows.clear(); }
  ingest(epoch, rows = []) {
    if (this.epoch !== epoch) this.clear(epoch);
    for (const row of rows) {
      const key = JSON.stringify([row.topic, row.node, row.source, row.segment_id]);
      const old = this.rows.get(key);
      if (!old || Number(row.revision || 0) >= Number(old.revision || 0)) this.rows.set(key, row);
    }
    while (this.rows.size > 2000) this.rows.delete(this.rows.keys().next().value);
  }
  values() { return [...this.rows.values()]; }
}
export function overlayCaption(rows, topic, source, time, linger = 3) {
  const candidates = rows.filter(r => r.topic === topic && (!r.source || r.source === source));
  const timed = candidates.filter(r => Number.isFinite(r.media_start) && time >= r.media_start && (!Number.isFinite(r.media_end) || time <= r.media_end + linger)).sort((a, b) => a.media_start - b.media_start);
  return timed.at(-1) || candidates.filter(r => !Number.isFinite(r.media_start)).at(-1) || null;
}
export function nodeQueues(queues) {
  return Array.isArray(queues) ? queues : queues && typeof queues === 'object' ? Object.entries(queues).map(([topic, queue]) => ({topic, ...queue})) : [];
}
export function mediaSource(node) {
  if (node.options?.device) return null;
  if (!/(ffmpeg|wav|media|video)/i.test(node.impl)) return null;
  const source = node.options?.url || node.options?.path || node.options?.source || '';
  if (/ffmpeg/i.test(node.impl) && /^(https?|rtsp|rtmp):\/\//i.test(source)) return null;
  return source;
}
