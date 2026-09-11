import { catalog, ports } from './model.ts';
import type { Document, Field, ModuleDefinition, PipelineNode, Value, Wire } from './model.ts';

export interface BackendNode {
  name: string;
  impl: string;
  enabled: boolean;
  mode: string;
  skip_if_finalized?: string | null;
  inputs: Record<string, string>;
  outputs: Record<string, string>;
  options: Record<string, Value>;
}
export interface BackendConfig {
  nodes: BackendNode[];
  settings: Record<string, Value>;
  editor: Record<string, Value>;
}
export interface CatalogEntry {
  impl: string;
  inputs: Record<string, string>;
  outputs: Record<string, string>;
  options: { name: string; default: Value; required: boolean; annotation: string }[];
  description?: string;
  error?: string;
}
const payload = (type: string) =>
  ({ AudioFrame: 'audio', Utterance: 'utterance', TextFrame: 'text' })[type] ?? type;
const title = (key: string) => key.replaceAll('_', ' ').replace(/^./, (c) => c.toUpperCase());
const hints: Record<string, Partial<Field>> = {
  silence_ms: {
    label: 'Silence duration',
    type: 'range',
    min: 50,
    max: 1500,
    step: 10,
    unit: 'ms',
  },
  start_db: {
    label: 'Speech trigger',
    type: 'range',
    min: 0,
    max: 30,
    step: 0.5,
    unit: 'dB above noise',
  },
  max_flatness: { label: 'Maximum flatness', type: 'range', min: 0, max: 1, step: 0.01 },
  beam_size: { type: 'range', min: 1, max: 10, step: 1 },
  context_lines: { type: 'range', min: 0, max: 20, step: 1 },
  target: {
    label: 'Translate to',
    type: 'select',
    choices: ['vi', 'zh', 'ja', 'es', 'fr', 'en', 'de', 'ko'],
  },
};
const primary = new Set([
  'model',
  'target',
  'language',
  'silence_ms',
  'start_db',
  'delay_ms',
  'path',
  'url',
]);
export function fieldFor(key: string, value: Value, annotation = ''): Field {
  return {
    key,
    label: title(key),
    default: value,
    nullable: value === null,
    type: typeof value === 'boolean'
      ? 'toggle'
      : typeof value === 'number' || /^(int|float)( \| None)?$/.test(annotation)
      ? 'number'
      : value !== null && typeof value === 'object'
      ? 'structured'
      : 'text',
    step: annotation === 'int' ? 1 : 'any',
    advanced: !primary.has(key),
    ...hints[key],
  };
}
export function installCatalog(entries: CatalogEntry[]) {
  for (const key of Object.keys(catalog)) delete catalog[key];
  for (const entry of entries) {
    const inputs = Object.fromEntries(
      Object.entries(entry.inputs).map(([k, v]) => [k, payload(v)]),
    );
    const outputs = Object.fromEntries(
      Object.entries(entry.outputs).map(([k, v]) => [k, payload(v)]),
    );
    const input = Object.values(inputs)[0], output = Object.values(outputs)[0];
    const role = !input
      ? 'source'
      : !output
      ? 'output'
      : output === 'utterance'
      ? 'segmenter'
      : input === 'utterance'
      ? 'transcriber'
      : output === 'text'
      ? 'translator'
      : 'segmenter';
    const colors: Record<string, string> = {
      source: '#eab27b',
      output: '#bfce8a',
      segmenter: '#70c6c7',
      transcriber: '#bba0e8',
      translator: '#92b5f4',
    };
    catalog[entry.impl] = {
      kind: entry.impl,
      impl: entry.impl,
      title: title(entry.impl),
      subtitle: entry.description ?? '',
      backend: true,
      inputs,
      outputs,
      input,
      output,
      role,
      color: colors[role],
      error: entry.error,
      fields: entry.options.filter((o) => !['stream', 'config'].includes(o.name)).map((o) => ({
        ...fieldFor(o.name, o.default, o.annotation),
        required: o.required,
      })),
    };
  }
}
export function nodeFields(node: PipelineNode): Field[] {
  const fields = [...catalog[node.kind].fields];
  for (const [key, value] of Object.entries(node.options)) {
    if (!fields.some((f) => f.key === key)) fields.push(fieldFor(key, value));
  }
  return fields;
}

/** Topics every node will publish when this document runs: saved topics first,
 * then the editor's wiring, naming new connections `${name}.${port}`. Ports
 * left unwired are absent, so a draft preview matches the running graph. */
function publishedOutputs(doc: Document): Map<string, Record<string, string>> {
  const outputs = new Map<string, Record<string, string>>();
  for (const n of doc.nodes) {
    const topics: Record<string, string> = { ...(n.raw?.outputs ?? {}) };
    for (const edge of doc.edges.filter((e) => e.source === n.id)) {
      const port = edge.sourceHandle ?? Object.keys(catalog[n.kind].outputs ?? {})[0];
      topics[port] ??= edge.topic ?? `${n.name}.${port}`;
    }
    outputs.set(n.id, topics);
  }
  return outputs;
}

/** Text endpoints, identified exactly like the subtitle monitor's rows. Every
 * text output port of an enabled node is listed, so the list follows the nodes
 * on the canvas and survives adoption of a run. `topic` is the name the run
 * will publish for that port; '' until the port is wired. */
export function textEndpoints(doc: Document): {
  node: string;
  port: string;
  id: string;
  label: string;
  topic: string;
}[] {
  const result: { node: string; port: string; id: string; label: string; topic: string }[] = [];
  const outputs = publishedOutputs(doc);
  for (const n of doc.nodes) {
    if (n.enabled === false) continue;
    const topics = outputs.get(n.id) ?? {};
    for (const [port, payload] of Object.entries(ports(n, 'source'))) {
      if (payload !== 'text') continue;
      result.push({
        node: n.name,
        port,
        id: `${n.name}:${port}`,
        label: `${n.name} · ${port}`,
        topic: topics[port] ?? '',
      });
    }
  }
  return result;
}
export function fromBackend(config: BackendConfig): Document {
  const publishers = new Map<string, { node: string; port: string }[]>();
  for (const n of config.nodes) {
    for (const [port, topic] of Object.entries(n.outputs)) {
      publishers.set(topic, [...(publishers.get(topic) ?? []), { node: n.name, port }]);
    }
  }
  const edges: Wire[] = [];
  const positions = config.editor.positions as Record<string, { x: number; y: number }> | undefined;
  const overlays = (config.editor.overlays ?? {}) as Record<string, Value>;
  const autoPause = (config.editor.auto_pause ?? {}) as Record<string, Value>;
  const nodes = config.nodes.map((n, index): PipelineNode => {
    if (!catalog[n.impl]) throw new Error(`Unknown implementation: ${n.impl}`);
    const unwiredInputs: Record<string, string> = {};
    const inputKeys = Object.keys(catalog[n.impl].inputs ?? {});
    for (const [port, topic] of Object.entries(n.inputs)) {
      const from = publishers.get(topic) ?? [];
      if (!from.length) unwiredInputs[port] = topic;
      for (const p of from) {
        edges.push({
          id: `${p.node}:${p.port}:${n.name}:${port}`,
          source: p.node,
          sourceHandle: p.port,
          target: n.name,
          targetHandle: inputKeys.length === 1 ? inputKeys[0] : port,
          topic,
        });
      }
    }
    const saved = positions?.[n.name];
    return {
      id: n.name,
      name: n.name,
      kind: n.impl,
      options: structuredClone(n.options),
      enabled: n.enabled,
      mode: n.mode,
      overlay: String(overlays[n.name] ?? ''),
      autoPause: Boolean(autoPause[n.name]),
      raw: structuredClone(n),
      unwiredInputs,
      position: saved && Number.isFinite(saved.x) && Number.isFinite(saved.y)
        ? { x: saved.x, y: saved.y }
        : { x: index * 310, y: 0 },
    };
  });
  return {
    version: 1,
    name: String(config.editor.name ?? 'Pipeline'),
    nodes,
    edges,
    backend: structuredClone(config),
  };
}

/** Keep the backend document's extension settings; edit only graph-owned fields. */
export function toBackend(doc: Document): BackendConfig {
  if (!doc.backend) throw new Error('Open a pipeline TOML or JSON configuration first.');
  const result = structuredClone(doc.backend);
  const names = new Set(doc.nodes.map((n) => n.name));
  if (names.size !== doc.nodes.length) throw new Error('Node names must be unique.');
  const outputs = publishedOutputs(doc);
  result.nodes = doc.nodes.map((n) => {
    const inputs = { ...n.unwiredInputs };
    // Preserve original fan-in aliases and ordering when the wiring is unchanged.
    const incoming = doc.edges.filter((e) => e.target === n.id);
    const byPort = new Map<string, string[]>();
    for (const e of incoming) {
      const upstream = outputs.get(e.source)!;
      const source = doc.nodes.find((n) => n.id === e.source)!;
      const topic = upstream[e.sourceHandle ?? Object.keys(catalog[source.kind].outputs ?? {})[0]];
      const port = e.targetHandle ?? Object.keys(catalog[n.kind].inputs ?? {})[0];
      byPort.set(port, [...new Set([...(byPort.get(port) ?? []), topic])]);
    }
    for (const [port, topics] of byPort) {
      const original = Object.entries(n.raw?.inputs ?? {}).filter(([key, t]) =>
        topics.includes(t) &&
        (Object.keys(catalog[n.kind].inputs ?? {}).length === 1 || key === port)
      );
      const used = new Set<string>();
      for (const [key, topic] of original) {
        inputs[key] = topic;
        used.add(topic);
      }
      for (const topic of topics.filter((t) => !used.has(t))) {
        let key = port, i = 0;
        while (inputs[key] !== undefined) key = `${port}_${i++}`;
        inputs[key] = topic;
      }
    }
    return {
      ...n.raw,
      name: n.name,
      impl: n.kind,
      enabled: n.enabled ?? true,
      mode: n.mode ?? 'default',
      inputs,
      outputs: outputs.get(n.id)!,
      options: structuredClone(n.options),
    };
  });
  const textTopics = new Set(
    result.nodes.filter((n) => n.enabled).flatMap((n) =>
      Object.entries(n.outputs).filter(([p]) => catalog[n.impl].outputs?.[p] === 'text').map((
        [, t],
      ) => t)
    ),
  );
  // Bus topics are broadcasts. Reject a drawing that pretends to disconnect just
  // one publisher while another publisher still feeds the same subscribed topic.
  for (const n of result.nodes) {
    const id = doc.nodes.find((node) => node.name === n.name)!.id;
    for (const topic of Object.values(n.inputs)) {
      for (
        const publisher of result.nodes.filter((p) => Object.values(p.outputs).includes(topic))
      ) {
        const upstream = doc.nodes.find((node) => node.name === publisher.name)!;
        if (
          !doc.edges.some((e) =>
            e.source === upstream.id && e.target === id &&
            outputs.get(e.source)
                ?.[e.sourceHandle ?? Object.keys(catalog[upstream.kind].outputs ?? {})[0]] === topic
          )
        ) {
          throw new Error(
            `Topic ${topic} is shared by several publishers. Disconnect the entire topic, or give the publishers distinct topics in the config.`,
          );
        }
      }
    }
  }
  // Auto pause needs an attached text output; drop it when its overlay did not survive.
  const overlays = Object.fromEntries(
    doc.nodes
      .map((n) => [n.name, n.overlay ?? ''] as const)
      .filter(([, topic]) => topic && textTopics.has(topic)),
  );
  result.editor = {
    ...result.editor,
    name: doc.name,
    positions: Object.fromEntries(doc.nodes.map((n) => [n.name, n.position])),
    overlays,
    auto_pause: Object.fromEntries(
      doc.nodes.filter((n) => n.autoPause && overlays[n.name]).map((n) => [n.name, true]),
    ),
  };
  return result;
}
