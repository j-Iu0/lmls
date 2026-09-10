/// <reference lib="dom" />
import { fromBackend, installCatalog, toBackend } from '../src/domain/backend.ts';
import type { BackendConfig, CatalogEntry } from '../src/domain/backend.ts';
import { connectionError, makeNode } from '../src/domain/model.ts';
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
