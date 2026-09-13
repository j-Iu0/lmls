/// <reference lib="dom" />
import {
  attachEndpoint,
  attachOverlay,
  fromBackend,
  installCatalog,
  textEndpoints,
  toBackend,
} from '../src/domain/backend.ts';
import type { BackendConfig, CatalogEntry } from '../src/domain/backend.ts';
import { catalog, connectionError, makeNode } from '../src/domain/model.ts';
import { mapSnapshot } from '../src/runtime/backend.ts';
import type { Snapshot } from '../src/runtime/backend.ts';
import type { RuntimeSnapshot } from '../src/runtime/types.ts';

const fixtures = JSON.parse(
  Deno.readTextFileSync(new URL('./backend_fixtures.json', import.meta.url)),
) as {
  catalog: CatalogEntry[];
  configs: Record<string, BackendConfig>;
};
installCatalog(fixtures.catalog);
function assert(value: unknown, message = 'Assertion failed'): asserts value {
  if (!value) throw new Error(message);
}
function equal(a: unknown, b: unknown) {
  const sorted = (v: unknown): unknown =>
    Array.isArray(v) ? v.map(sorted) : v && typeof v === 'object'
      ? Object.fromEntries(
        Object.entries(v).sort(([a], [b]) => a.localeCompare(b)).map(([k, v]) => [k, sorted(v)]),
      )
      : v;
  assert(
    JSON.stringify(sorted(a)) === JSON.stringify(sorted(b)),
    `${JSON.stringify(a)} != ${JSON.stringify(b)}`,
  );
}
Deno.test('all checked-in pipelines preserve implementations, options, fan-in, topics and global settings', () => {
  for (const [name, config] of Object.entries(fixtures.configs)) {
    const doc = fromBackend(config), output = toBackend(doc);
    equal(output.nodes, config.nodes);
    equal(output.settings, config.settings);
    assert(doc.nodes.length === config.nodes.length, name);
  }
});
Deno.test('unknown nested options, disabled nodes and editor extension metadata survive edits', () => {
  const config = structuredClone(fixtures.configs['mock.toml']);
  config.nodes[1].options.extension = { entries: [1, false, null, { text: 'kept' }] };
  config.nodes[1].enabled = false;
  config.editor.extension = { custom: true };
  const doc = fromBackend(config);
  doc.nodes[0].position = { x: 13, y: 47 };
  const output = toBackend(doc);
  equal(output.nodes[1], config.nodes[1]);
  equal(output.editor.extension, config.editor.extension);
  equal((output.editor.positions as Record<string, unknown>)[doc.nodes[0].name], { x: 13, y: 47 });
});
Deno.test('overlay and auto-pause editor settings round trip and follow validity of their topic', () => {
  const config = structuredClone(fixtures.configs['mock.toml']);
  config.editor.overlays = { src: 'text.out' };
  config.editor.auto_pause = { src: true };
  const doc = fromBackend(config);
  assert(doc.nodes[0].overlay === 'text.out');
  assert(doc.nodes[0].autoPause === true);
  equal(toBackend(doc).editor.overlays, { src: 'text.out' });
  equal(toBackend(doc).editor.auto_pause, { src: true });
  // Clearing the overlay clears auto pause in the same produced document.
  doc.nodes[0].overlay = '';
  const cleared = toBackend(doc);
  equal(cleared.editor.overlays, {});
  equal(cleared.editor.auto_pause, {});
  // An overlay topic no enabled text port publishes is dropped with its auto pause.
  doc.nodes[0].overlay = 'text.gone';
  doc.nodes[0].autoPause = true;
  const stale = toBackend(doc);
  equal(stale.editor.overlays, {});
  equal(stale.editor.auto_pause, {});
  // Endpoints follow the canvas nodes; only wired ports carry a topic.
  const endpoints = textEndpoints(doc);
  equal(
    endpoints.map((e) => e.id).sort(),
    ['asr:text', 'fix:text_out', 'vi:corrected', 'vi:text_out'],
  );
  equal(endpoints.find((e) => e.id === 'vi:corrected')!.topic, '');
  equal(endpoints.find((e) => e.id === 'vi:text_out')!.topic, 'text.out');
});
Deno.test('endpoint list follows the canvas nodes; topics follow the wiring', () => {
  const doc = fromBackend(fixtures.configs['mock.toml']);
  // A library-added node lists its declared text ports right away, topic-less
  // until wired; adoption of a run keeps the same ports listed.
  const terminal = makeNode('mock_translator', 'new', { x: 0, y: 0 });
  doc.nodes.push(terminal);
  let endpoints = textEndpoints(doc);
  equal(
    endpoints.filter((e) => e.node === terminal.name).map((e) => e.id),
    [`${terminal.name}:text_out`, `${terminal.name}:corrected`],
  );
  assert(endpoints.find((e) => e.id === `${terminal.name}:text_out`)!.topic === '');
  // Wiring one port names its topic exactly as the run will publish it.
  doc.edges.push({
    id: 'new',
    source: terminal.id,
    target: doc.nodes.at(-1)!.id,
    sourceHandle: 'text_out',
    targetHandle: 'text',
  });
  endpoints = textEndpoints(doc);
  assert(
    endpoints.find((e) => e.id === `${terminal.name}:text_out`)!.topic ===
      `${terminal.name}.text_out`,
  );
  assert(endpoints.find((e) => e.id === `${terminal.name}:corrected`)!.topic === '');
  equal(
    toBackend(doc).nodes.find((n) => n.name === terminal.name)?.outputs,
    { text_out: `${terminal.name}.text_out` },
  );
});
Deno.test('attaching an overlay wires the endpoint it needs', () => {
  const doc = fromBackend(fixtures.configs['mock.toml']);
  const src = doc.nodes.find((n) => n.name === 'src')!;
  // vi's corrected port is unwired and topic-less until the overlay needs it.
  assert(textEndpoints(doc).find((e) => e.id === 'vi:corrected')!.topic === '');
  const attached = attachOverlay(doc, src.id, { node: 'vi', port: 'corrected' });
  assert(attached.nodes.find((n) => n.id === src.id)!.overlay === 'vi.corrected');
  const config = toBackend(attached);
  equal(config.editor.overlays, { src: 'vi.corrected' });
  equal(config.nodes.find((n) => n.name === 'vi')!.outputs, {
    text_out: 'text.out',
    corrected: 'vi.corrected',
  });
  // Attaching to a publishing endpoint never rewrites its topic.
  const rebased = attachOverlay(attached, src.id, { node: 'vi', port: 'text_out' });
  assert(rebased.nodes.find((n) => n.id === src.id)!.overlay === 'text.out');
  equal(
    toBackend(rebased).nodes.find((n) => n.name === 'vi')!.outputs,
    { text_out: 'text.out', corrected: 'vi.corrected' },
  );
  // A library-added node is wired on attach as well; its raw is synthesized.
  const fresh = makeNode('mock_translator', 'fresh', { x: 0, y: 0 });
  rebased.nodes.push(fresh);
  const wired = attachOverlay(rebased, src.id, { node: fresh.name, port: 'text_out' });
  assert(wired.nodes.find((n) => n.id === src.id)!.overlay === `${fresh.name}.text_out`);
  equal(
    toBackend(wired).nodes.find((n) => n.name === fresh.name)!.outputs,
    { text_out: `${fresh.name}.text_out` },
  );
  // Clearing detaches the overlay and its auto pause.
  const cleared = attachOverlay(wired, src.id, null);
  assert(cleared.nodes.find((n) => n.id === src.id)!.overlay === '');
  assert(cleared.nodes.find((n) => n.id === src.id)!.autoPause === false);
});
Deno.test('watching a topic-less endpoint wires it without touching any overlay', () => {
  const doc = fromBackend(fixtures.configs['mock.toml']);
  assert(textEndpoints(doc).find((e) => e.id === 'vi:corrected')!.topic === '');
  const watched = attachEndpoint(doc, { node: 'vi', port: 'corrected' });
  assert(textEndpoints(watched).find((e) => e.id === 'vi:corrected')!.topic === 'vi.corrected');
  equal(toBackend(watched).nodes.find((n) => n.name === 'vi')!.outputs, {
    text_out: 'text.out',
    corrected: 'vi.corrected',
  });
  // Watching a publishing endpoint never rewrites its topic.
  const republished = attachEndpoint(watched, { node: 'vi', port: 'text_out' });
  equal(toBackend(republished).nodes.find((n) => n.name === 'vi')!.outputs, {
    text_out: 'text.out',
    corrected: 'vi.corrected',
  });
});
Deno.test('real port types distinguish both outputs of translators and reject incompatible links', () => {
  const doc = fromBackend(fixtures.configs['mock.toml']);
  const vi = doc.nodes.find((n) => n.kind === 'mock_translator')!;
  const out = makeNode('collect', 'new', { x: 1, y: 2 });
  doc.nodes.push(out);
  assert(connectionError(doc, vi.id, out.id, 'corrected', 'text') === null);
  doc.edges.push({
    id: 'new',
    source: vi.id,
    target: out.id,
    sourceHandle: 'corrected',
    targetHandle: 'text',
  });
  const config = toBackend(doc);
  assert(config.nodes.at(-1)?.inputs.text === `${vi.name}.corrected`);
  assert(connectionError(doc, vi.id, out.id, 'text_out', 'text') === null);
  assert(connectionError(doc, doc.nodes[0].id, out.id, 'audio', 'text') !== null);
});
Deno.test('disconnect removes subscription; orphan topics are retained for server validation', () => {
  const config = structuredClone(fixtures.configs['mock.toml']);
  config.nodes[1].inputs.audio = 'missing.topic';
  const doc = fromBackend(config);
  equal(toBackend(doc).nodes[1].inputs, { audio: 'missing.topic' });
  const output = doc.nodes.find((n) => n.kind === 'stdout_pretty')!;
  doc.edges = doc.edges.filter((e) => e.target !== output.id);
  equal(toBackend(doc).nodes.find((n) => n.name === output.name)?.inputs, {});
});
Deno.test('catalog choices render a labelled system-audio select that round trips', () => {
  const entries: CatalogEntry[] = [{
    impl: 'ffmpeg',
    inputs: {},
    outputs: { audio: 'AudioFrame' },
    options: [
      { name: 'url', default: null, required: false, annotation: 'str | None' },
      {
        name: 'pulse',
        default: null,
        required: false,
        annotation: 'str | bool | None',
        choices: ['monitor', 'alsa_output.spk.monitor'],
      },
    ],
  }];
  installCatalog(entries);
  const pulse = catalog['ffmpeg'].fields.find((f) => f.key === 'pulse')!;
  assert(pulse.type === 'select');
  assert(pulse.nullable === true);
  equal(pulse.choices, ['monitor', 'alsa_output.spk.monitor']);
  assert(pulse.labels!['monitor'] === 'Default output · all applications');
  // An 'off' select choice is stored as null and survives the round trip.
  const doc = fromBackend({
    nodes: [{
      name: 'src',
      impl: 'ffmpeg',
      enabled: true,
      mode: 'default',
      inputs: {},
      outputs: { audio: 'audio.raw' },
      options: { pulse: 'monitor', realtime: true },
    }],
    settings: {},
    editor: {},
  });
  assert(doc.nodes[0].options.pulse === 'monitor');
  doc.nodes[0].options.pulse = null;
  equal(toBackend(doc).nodes[0].options, { pulse: null, realtime: true });
  installCatalog(fixtures.catalog);
});
Deno.test('real snapshots retain segment identity, exact service metrics and generation-bounded history', () => {
  const config = fixtures.configs['mock.toml'];
  const previous: RuntimeSnapshot = {
    status: 'idle',
    epoch: 1,
    time: 0,
    media: {},
    metrics: {},
    captions: [],
    logs: [],
  };
  const snapshot: Snapshot = {
    state: 'running',
    epoch: 1,
    config,
    error: null,
    nodes: { asr: { state: 'running', queues: [{ depth: 2, dropped: 3 }], level_dbfs: -23 } },
    metrics: { stages: { asr: { n: 1, mean: 120, p50: 119, p95: 122 } } },
    media: { src: { position: 3, paused: false, auto_paused: false, status: 'Playing' } },
    logs: [],
    subtitles: [{
      segment_id: 'src:u1',
      node: 'asr',
      topic: 'text.raw',
      lang: 'en',
      text: 'Real event',
      revision: 2,
      is_final: true,
      end_to_end_ms: 200,
      stage_latency_ms: { asr: 120 },
    }],
  };
  const first = mapSnapshot(snapshot, previous), repeated = mapSnapshot(snapshot, first);
  equal(repeated.metrics.asr.samples, [120]);
  equal(repeated.metrics.asr.queue, 2);
  equal(repeated.metrics.asr.drops, 3);
  assert(repeated.captions[0].segment === 'src:u1');
  assert(repeated.captions[0].trace[0].wait === null);
  assert(Number.isNaN(repeated.captions[0].start));
  snapshot.epoch = 2;
  snapshot.metrics.stages!.asr.mean = 80;
  snapshot.subtitles = [];
  const sought = mapSnapshot(snapshot, repeated);
  equal(sought.metrics.asr.samples, [80]);
  equal(sought.captions, []);
});
