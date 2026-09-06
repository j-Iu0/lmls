import test from 'node:test';
import assert from 'node:assert/strict';
import {normalizeConfig, connect, connections, disconnect, removeNode, renameNode, addNode, autoLayout, textTopics, CaptionStore, groupCaptions, overlayCaption, mediaSource, nodeQueues, validateLocal, isActive} from '../../livesub/web/static/model.mjs';

const catalog = [
  {impl: 'wav', inputs: {}, outputs: {audio: 'AudioFrame'}, options: [{name: 'path', required: true, default: null}, {name: 'optional', default: null}, {name: 'realtime', default: true}]},
  {impl: 'asr', inputs: {audio: 'AudioFrame'}, outputs: {text: 'TextFrame'}, options: []},
  {impl: 'sink', inputs: {text: 'TextFrame'}, outputs: {}, options: []},
  {impl: 'pair', inputs: {a: 'TextFrame', b: 'TextFrame'}, outputs: {}, options: []},
];
const node = (name, impl, inputs = {}, outputs = {}) => ({name, impl, inputs, outputs, options: {}});
const config = nodes => normalizeConfig({nodes, settings: {unknown: {keep: [1, 2]}}, editor: {custom: 'preserved'}});
const out = (node, port) => ({node, port, direction: 'output'});
const input = (node, port) => ({node, port, direction: 'input'});

test('normalization preserves unknown settings and editor fields without mutating the input', () => {
  const original = {nodes: [node('a', 'wav')], settings: {future: {value: 5}}, editor: {plugin: true}};
  const result = normalizeConfig(original); result.settings.future.value = 9;
  assert.equal(original.settings.future.value, 5); assert.equal(result.editor.plugin, true); assert.equal(result.nodes[0].enabled, true);
  assert.throws(() => normalizeConfig({nodes: [node('a', 'wav'), node('a', 'wav')]}), /unique/);
});
test('single input supports typed fan-in and output supports fan-out without duplicate edges', () => {
  const c = config([node('a', 'wav'), node('b', 'wav'), node('x', 'asr'), node('y', 'asr')]);
  connect(c, catalog, out('a', 'audio'), input('x', 'audio'));
  connect(c, catalog, input('x', 'audio'), out('b', 'audio'));
  connect(c, catalog, out('a', 'audio'), input('y', 'audio'));
  connect(c, catalog, out('a', 'audio'), input('x', 'audio'));
  assert.deepEqual(c.nodes[2].inputs, {audio: 'a.audio', audio_1: 'b.audio'}); assert.equal(connections(c, catalog).length, 3);
});
test('connections reject mismatched types, same directions and self loops', () => {
  const c = config([node('a', 'wav'), node('x', 'asr'), node('s', 'sink')]);
  assert.throws(() => connect(c, catalog, out('a', 'audio'), input('s', 'text')), /types/);
  assert.throws(() => connect(c, catalog, out('a', 'audio'), out('x', 'text')), /output to an input/);
  assert.throws(() => connect(c, catalog, out('x', 'text'), input('x', 'audio')), /itself/);
});
test('multiple named inputs never create unsupported suffixed fan-in keys', () => {
  const c = config([node('x', 'asr', {}, {text: 'x.text'}), node('y', 'asr', {}, {text: 'y.text'}), node('pair', 'pair')]);
  connect(c, catalog, out('x', 'text'), input('pair', 'a'));
  assert.throws(() => connect(c, catalog, out('y', 'text'), input('pair', 'a')), /occupied/);
  connect(c, catalog, out('y', 'text'), input('pair', 'b')); assert.equal(connections(c, catalog).length, 2);
});
test('disconnect removes the topic subscription while preserving other fan-in and fan-out', () => {
  const c = config([node('a', 'wav', {}, {audio: 'a'}), node('b', 'wav', {}, {audio: 'b'}), node('x', 'asr', {audio: 'a', audio_1: 'b'}), node('y', 'asr', {audio: 'a'})]);
  disconnect(c, connections(c, catalog).find(e => e.source === 'a' && e.target === 'x'));
  assert.deepEqual(c.nodes[2].inputs, {audio_1: 'b'}); assert.equal(c.nodes[3].inputs.audio, 'a'); assert.equal(c.nodes[0].outputs.audio, 'a');
});
test('removing a publisher cleans orphan topics but keeps a shared bus with other publishers', () => {
  const c = config([node('a', 'asr', {}, {text: 'shared'}), node('b', 'asr', {}, {text: 'shared'}), node('s', 'sink', {text: 'shared'})]);
  c.editor.subtitle_topics = ['shared']; c.editor.overlays.video = 'shared';
  removeNode(c, 'a'); assert.equal(c.nodes[1].inputs.text, 'shared');
  removeNode(c, 'b'); assert.deepEqual(c.nodes[0].inputs, {}); assert.deepEqual(c.editor.subtitle_topics, []); assert.deepEqual(c.editor.overlays, {});
});
test('renaming moves positions and source settings without renaming shared topics', () => {
  const c = config([node('a', 'wav', {}, {audio: 'audio.raw'})]);
  c.editor.positions.a = {x: 30, y: -40}; c.editor.overlays.a = 'text'; c.editor.auto_pause.a = true;
  renameNode(c, 'a', 'source'); assert.deepEqual(c.editor.positions.source, {x: 30, y: -40}); assert.equal(c.editor.auto_pause.source, true); assert.equal(c.nodes[0].outputs.audio, 'audio.raw'); assert.equal(c.editor.positions.a, undefined);
});
test('new nodes omit null defaults and immediately provision every output for monitor attachment', () => {
  const c = config([]); const a = addNode(c, catalog[0], {x: 0, y: 0});
  assert.deepEqual(a.options, {realtime: true}); assert.deepEqual(a.outputs, {audio: 'wav.audio'});
  const b = addNode(c, catalog[1], {x: 100, y: 0}); assert.deepEqual(textTopics(c, catalog), ['asr.text']);
  assert.ok(validateLocal(c, catalog).some(x => x.includes('required option path'))); assert.equal(b.enabled, true);
});
test('layout orders the DAG and terminates on cycles and disconnected nodes', () => {
  const c = config([node('a', 'wav', {}, {audio: 'raw'}), node('x', 'asr', {audio: 'raw'}, {text: 'text'}), node('s', 'sink', {text: 'text'})]);
  autoLayout(c, catalog); assert.ok(c.editor.positions.a.x < c.editor.positions.x.x); assert.ok(c.editor.positions.x.x < c.editor.positions.s.x);
  c.nodes[0].inputs.audio = 'text'; autoLayout(c, catalog); assert.equal(Object.keys(c.editor.positions).length, 3);
});
test('caption revisions replace rows; stale revisions do not regress; new epochs clear captions', () => {
  const store = new CaptionStore(), base = {topic: 'text', node: 'asr', source: 'a', segment_id: 'a:1'};
  store.ingest(1, [{...base, revision: 1, text: 'partial'}]); store.ingest(1, [{...base, revision: 2, text: 'final', is_final: true}]); store.ingest(1, [{...base, revision: 1, text: 'stale'}]);
  assert.equal(store.values().length, 1); assert.equal(store.values()[0].text, 'final'); store.ingest(2, []); assert.equal(store.values().length, 0);
});
test('translations share their original segment block even when received first, with originals ordered above them', () => {
  const c = config([
    node('translate', 'translator', {text: 'corrected'}, {text: 'translated'}),
    node('correct', 'corrector', {text: 'original'}, {text: 'corrected'}),
    node('asr', 'asr', {}, {text: 'original'}),
  ]);
  const base = {source: 'file', segment_id: '1', revision: 1};
  const translated = {...base, node: 'translate', topic: 'translated', text: 'Bonjour'};
  const original = {...base, node: 'asr', topic: 'original', text: 'Hello'};
  const corrected = {...base, node: 'correct', topic: 'corrected', text: 'Hello!'};
  const groups = groupCaptions([translated, corrected, original, {...original, segment_id: '2'}, {...original, source: 'mic'}], c);
  assert.equal(groups.length, 3);
  assert.deepEqual(groups[0].rows.map(row => row.text), ['Hello', 'Hello!', 'Bonjour']);
  assert.equal(groups[1].segment_id, '2'); assert.equal(groups[2].source, 'mic');
});
test('late translations and independent revisions retain one stable subtitle block', () => {
  const c = config([node('asr', 'asr', {}, {text: 'original'}), node('translate', 'translator', {text: 'original'}, {text: 'translated'})]);
  const store = new CaptionStore(), original = {source: 'file', segment_id: '1', node: 'asr', topic: 'original', revision: 1, text: 'Hello'};
  store.ingest(1, [original]);
  const key = groupCaptions(store.values(), c)[0].key;
  store.ingest(1, [{...original, node: 'translate', topic: 'translated', text: 'Bonjour'}]);
  store.ingest(1, [{...original, revision: 2, text: 'Hello!', is_final: true}]);
  const groups = groupCaptions(store.values(), c);
  assert.equal(groups.length, 1); assert.equal(groups[0].key, key);
  assert.deepEqual(groups[0].rows.map(row => [row.text, row.revision]), [['Hello!', 2], ['Bonjour', 1]]);
  store.ingest(2, []); assert.deepEqual(groupCaptions(store.values(), c), []);
});
test('late timed overlays linger after media_end, yield to the next interval, and eventually expire', () => {
  const first = {topic: 'text', source: 'a', text: 'late final', media_start: 1, media_end: 2};
  assert.equal(overlayCaption([first], 'text', 'a', 2.4), first);
  assert.equal(overlayCaption([first], 'text', 'a', 5.1), null);
  const next = {...first, text: 'next', media_start: 3, media_end: 4};
  assert.equal(overlayCaption([first, next], 'text', 'a', 3.1), next);
  assert.equal(overlayCaption([first], 'text', 'other', 2), null);
});
test('untimed captions fall back to latest matching topic and source', () => {
  const rows = [{topic: 'text', text: 'one'}, {topic: 'other', text: 'two'}, {topic: 'text', text: 'three'}];
  assert.equal(overlayCaption(rows, 'text', 'a', 100).text, 'three');
});
test('remote ffmpeg streams and devices never mount local interactive players', () => {
  for (const url of ['http://example.com/live', 'https://example.com/live', 'rtsp://example.com/live', 'rtmp://example.com/live']) assert.equal(mediaSource({...node('a', 'ffmpeg'), options: {url}}), null);
  assert.equal(mediaSource({...node('a', 'ffmpeg'), options: {url: 'Camera', device: true}}), null);
  assert.equal(mediaSource({...node('a', 'ffmpeg'), options: {url: 'assets/movie.mp4'}}), 'assets/movie.mp4');
  assert.equal(mediaSource({...node('a', 'wav'), options: {path: '/tmp/audio.wav'}}), '/tmp/audio.wav');
  assert.equal(mediaSource(node('a', 'sink')), null);
});
test('runtime edit locks and queue normalization follow the transport contract', () => {
  for (const state of ['starting', 'running', 'seeking', 'stopping']) assert.equal(isActive(state), true);
  for (const state of ['idle', 'completed', 'failed']) assert.equal(isActive(state), false);
  assert.deepEqual(nodeQueues({raw: {depth: 3, dropped: 2}}), [{topic: 'raw', depth: 3, dropped: 2}]);
  assert.deepEqual(nodeQueues(null), []);
});
