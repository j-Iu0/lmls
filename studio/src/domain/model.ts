export type Kind = string;
export type Payload = string;
export type Value = string | number | boolean | null | Value[] | { [key: string]: Value };
export interface Field {
  key: string;
  label: string;
  type: 'select' | 'range' | 'toggle' | 'text' | 'number' | 'structured';
  nullable?: boolean;
  required?: boolean;
  default: Value;
  choices?: string[];
  min?: number;
  max?: number;
  step?: number | 'any';
  unit?: string;
  advanced?: boolean;
}
export interface ModuleDefinition {
  kind: Kind;
  title: string;
  subtitle: string;
  impl: string;
  color: string;
  input?: Payload;
  output?: Payload;
  fields: Field[];
  inputs?: Record<string, Payload>;
  outputs?: Record<string, Payload>;
  role?: string;
  error?: string;
  backend?: boolean;
}
export interface PipelineNode {
  id: string;
  kind: Kind;
  name: string;
  position: { x: number; y: number };
  options: Record<string, Value>;
  enabled?: boolean;
  mode?: string;
  raw?: import('./backend.ts').BackendNode;
  unwiredInputs?: Record<string, string>;
}
export interface Wire {
  id: string;
  source: string;
  target: string;
  sourceHandle?: string;
  targetHandle?: string;
  topic?: string;
}
export interface Document {
  version: 1;
  name: string;
  nodes: PipelineNode[];
  edges: Wire[];
  backend?: import('./backend.ts').BackendConfig;
}

export const catalog: Record<Kind, ModuleDefinition> = {
  source: {
    kind: 'source',
    title: 'Media source',
    subtitle: 'Audio & video',
    impl: 'ffmpeg',
    color: '#eab27b',
    output: 'audio',
    fields: [
      {
        key: 'realtime',
        label: 'Realtime playback',
        type: 'toggle',
        default: true,
        advanced: true,
      },
    ],
  },
  segmenter: {
    kind: 'segmenter',
    title: 'Speech detection',
    subtitle: 'Find the moments that matter',
    impl: 'energy',
    color: '#70c6c7',
    input: 'audio',
    output: 'utterance',
    fields: [
      {
        key: 'threshold',
        label: 'Speech threshold',
        type: 'range',
        default: 0.45,
        min: 0.1,
        max: 0.9,
        step: 0.05,
      },
      {
        key: 'silence',
        label: 'Silence duration',
        type: 'range',
        default: 450,
        min: 100,
        max: 1200,
        step: 50,
        unit: 'ms',
      },
      {
        key: 'padding',
        label: 'Speech padding',
        type: 'range',
        default: 150,
        min: 0,
        max: 500,
        step: 10,
        unit: 'ms',
        advanced: true,
      },
    ],
  },
  transcriber: {
    kind: 'transcriber',
    title: 'Transcription',
    subtitle: 'Speech → text',
    impl: 'faster_whisper',
    color: '#bba0e8',
    input: 'utterance',
    output: 'text',
    fields: [
      {
        key: 'model',
        label: 'Model',
        type: 'select',
        default: 'small.en',
        choices: ['tiny.en', 'base.en', 'small.en', 'medium.en', 'large-v3'],
      },
      {
        key: 'language',
        label: 'Language',
        type: 'select',
        default: 'English',
        choices: ['Auto detect', 'English', 'Chinese', 'Japanese', 'Vietnamese'],
      },
      {
        key: 'device',
        label: 'Device',
        type: 'select',
        default: 'Auto',
        choices: ['Auto', 'CPU', 'CUDA'],
        advanced: true,
      },
      {
        key: 'beam',
        label: 'Beam size',
        type: 'range',
        default: 5,
        min: 1,
        max: 10,
        step: 1,
        advanced: true,
      },
    ],
  },
  translator: {
    kind: 'translator',
    title: 'Translation',
    subtitle: 'A little less lost in translation',
    impl: 'ollama',
    color: '#92b5f4',
    input: 'text',
    output: 'text',
    fields: [
      {
        key: 'target',
        label: 'Translate to',
        type: 'select',
        default: 'Vietnamese',
        choices: ['Vietnamese', 'Chinese', 'Japanese', 'Spanish', 'French'],
      },
      {
        key: 'model',
        label: 'Model',
        type: 'select',
        default: 'qwen3:8b',
        choices: ['qwen3:8b', 'gemma3:4b', 'llama3.2:3b'],
      },
      {
        key: 'context',
        label: 'Context segments',
        type: 'range',
        default: 3,
        min: 0,
        max: 10,
        step: 1,
        advanced: true,
      },
    ],
  },
  output: {
    kind: 'output',
    title: 'Subtitle output',
    subtitle: 'The words, together',
    impl: 'stdout_pretty',
    color: '#bfce8a',
    input: 'text',
    fields: [
      {
        key: 'destination',
        label: 'Destination',
        type: 'select',
        default: 'Console',
        choices: ['Console', 'WebSocket', 'SRT file'],
      },
      { key: 'partials', label: 'Include partials', type: 'toggle', default: true },
    ],
  },
};
export const payloadColors: Record<Payload, string> = {
  audio: '#eab27b',
  utterance: '#70c6c7',
  text: '#bba0e8',
};

export function makeNode(kind: Kind, id: string, position: PipelineNode['position']): PipelineNode {
  const module = catalog[kind];
  return {
    id,
    kind,
    name: module.backend ? `${kind}_${id.slice(0, 8)}` : module.title,
    position,
    options: Object.fromEntries(
      module.fields.filter((f) => !module.backend || f.required).map((f) => [f.key, f.default]),
    ),
  };
}
export function example(): Document {
  return {
    version: 1,
    name: 'Lecture · bilingual',
    nodes: [
      makeNode('source', 'source', { x: 0, y: 0 }),
      makeNode('segmenter', 'vad', { x: 300, y: 0 }),
      makeNode('transcriber', 'asr', { x: 600, y: 0 }),
      makeNode('translator', 'translate', { x: 900, y: 0 }),
    ],
    edges: [
      { id: 'source-vad', source: 'source', target: 'vad' },
      { id: 'vad-asr', source: 'vad', target: 'asr' },
      { id: 'asr-translate', source: 'asr', target: 'translate' },
    ],
  };
}
export function ports(node: PipelineNode, direction: 'source' | 'target'): Record<string, Payload> {
  const d = catalog[node.kind];
  const declared = direction === 'source' ? d.outputs : d.inputs;
  if (declared) return declared;
  const payload = direction === 'source' ? d.output : d.input;
  return payload ? { [direction === 'source' ? 'out' : 'in']: payload } : {};
}
export function connectionError(
  doc: Document,
  source: string,
  target: string,
  sourceHandle?: string,
  targetHandle?: string,
): string | null {
  const a = doc.nodes.find((n) => n.id === source), b = doc.nodes.find((n) => n.id === target);
  if (!a || !b) return 'Both nodes must exist.';
  if (source === target) return 'A node cannot connect to itself.';
  const outputs = ports(a, 'source'), inputs = ports(b, 'target');
  sourceHandle ??= Object.keys(outputs)[0];
  targetHandle ??= Object.keys(inputs)[0];
  if (!outputs[sourceHandle] || outputs[sourceHandle] !== inputs[targetHandle]) {
    return 'Connect matching payload types.';
  }
  if (
    doc.edges.some((e) =>
      e.source === source && e.target === target &&
      (e.sourceHandle ?? Object.keys(outputs)[0]) === sourceHandle &&
      (e.targetHandle ?? Object.keys(inputs)[0]) === targetHandle
    )
  ) {
    return 'This connection already exists.';
  }
  if (
    Object.keys(inputs).length > 1 &&
    doc.edges.some((e) => e.target === target && e.targetHandle === targetHandle)
  ) return 'This input already has a connection.';
  const seen = new Set<string>();
  const reaches = (id: string): boolean => {
    if (id === source) return true;
    if (seen.has(id)) return false;
    seen.add(id);
    return doc.edges.filter((e) => e.source === id).some((e) => reaches(e.target));
  };
  return reaches(target) ? 'This connection would create a cycle.' : null;
}
export function validate(doc: Document): string[] {
  const errors: string[] = [];
  if (!doc.nodes.some((n) => n.kind === 'source')) errors.push('Add a media source.');
  if (!doc.nodes.some((n) => n.kind === 'transcriber')) errors.push('Add a transcription node.');
  for (const node of doc.nodes) {
    if (catalog[node.kind].input && !doc.edges.some((e) => e.target === node.id)) {
      errors.push(`${node.name} needs an input.`);
    }
  }
  return errors;
}

/** Only studio documents are accepted; this is not a livesub config decoder. */
export function parseDocument(text: string): Document {
  const raw = JSON.parse(text);
  if (
    raw?.version !== 1 || typeof raw.name !== 'string' || !Array.isArray(raw.nodes) ||
    !Array.isArray(raw.edges)
  ) throw new Error('Choose a livesub studio document (version 1).');
  if (raw.nodes.length > 150 || raw.edges.length > 600) {
    throw new Error('Demo limit: 150 nodes and 600 connections.');
  }
  const ids = new Set<string>();
  for (const n of raw.nodes) {
    if (
      !n || typeof n.id !== 'string' || ids.has(n.id) || !Object.hasOwn(catalog, n.kind) ||
      typeof n.name !== 'string' || !Number.isFinite(n.position?.x) ||
      !Number.isFinite(n.position?.y) || !n.options || typeof n.options !== 'object'
    ) throw new Error('Invalid or duplicate node.');
    ids.add(n.id);
    for (const f of catalog[n.kind as Kind].fields) {
      const v = n.options[f.key];
      if (
        typeof v !== typeof f.default ||
        (typeof v === 'number' && (!Number.isFinite(v) || v < f.min! || v > f.max!)) ||
        (f.choices && !f.choices.includes(String(v)))
      ) throw new Error(`Invalid ${f.label}.`);
    }
  }
  const clean: Document = {
    version: 1,
    name: raw.name,
    nodes: raw.nodes.map((n: PipelineNode) => ({
      id: n.id,
      kind: n.kind,
      name: n.name,
      position: { ...n.position },
      options: { ...n.options },
    })),
    edges: [],
  };
  const edgeIds = new Set<string>();
  for (const e of raw.edges) {
    if (
      typeof e?.id !== 'string' || edgeIds.has(e.id) || connectionError(clean, e.source, e.target)
    ) throw new Error('Invalid graph connection.');
    edgeIds.add(e.id);
    clean.edges.push({ id: e.id, source: e.source, target: e.target });
  }
  return clean;
}
