import { fromBackend, installCatalog, toBackend } from '../domain/backend.ts';
import type { BackendConfig, CatalogEntry } from '../domain/backend.ts';
import type { Document } from '../domain/model.ts';
import { commands, editor } from '../state/editor.ts';
import { publishNotification, resolveNotification } from '../state/notifications.ts';
import { createStore } from '../state/store.ts';
import type { Caption, MediaClock, Metric, RuntimeSnapshot } from './types.ts';

export interface Snapshot {
  state: RuntimeSnapshot['status'];
  epoch: number;
  config: BackendConfig;
  error: string | null;
  nodes: Record<
    string,
    {
      state?: string;
      startup?: { phase: string; message?: string };
      level_dbfs?: number;
      queues?: { depth: number; dropped: number }[];
    }
  >;
  metrics: { stages?: Record<string, { n: number; mean: number; p50: number; p95: number }> };
  subtitles: {
    segment_id: string;
    source?: string;
    node: string;
    topic: string;
    lang: string;
    text: string;
    is_final: boolean;
    revision: number;
    media_start?: number;
    media_end?: number;
    end_to_end_ms: number;
    stage_latency_ms: Record<string, number>;
  }[];
  logs: { time: number; level: string; message: string; node: string }[];
  media: Record<
    string,
    { position: number; paused: boolean; auto_paused: boolean; status: string }
  >;
}
export const activeState = (state: string) =>
  ['starting', 'running', 'seeking', 'stopping'].includes(state);
export const runtime = createStore<RuntimeSnapshot>({
  status: 'idle',
  connected: false,
  failure: null,
  transportError: null,
  epoch: 0,
  time: 0,
  media: {},
  metrics: {},
  captions: [],
  logs: [],
});
const round = (n: number) => Math.round(n * 10) / 10;
export function mapSnapshot(data: Snapshot, previous: RuntimeSnapshot): RuntimeSnapshot {
  const sameEpoch = data.epoch === previous.epoch;
  const metrics: Record<string, Metric> = {};
  for (const [id, node] of Object.entries(data.nodes)) {
    const m = data.metrics.stages?.[id], old = sameEpoch ? previous.metrics[id] : undefined;
    const samples = [...(old?.samples ?? [])];
    // A history point is the lifetime mean at a changed processing count, not a fabricated operation.
    if (m && m.n !== old?.count) samples.push(m.mean);
    metrics[id] = {
      mean: m?.mean ?? 0,
      p50: m?.p50 ?? 0,
      p95: m?.p95 ?? 0,
      count: m?.n ?? 0,
      queue: node.queues?.reduce((n, q) => n + q.depth, 0) ?? 0,
      drops: node.queues?.reduce((n, q) => n + q.dropped, 0) ?? 0,
      samples: samples.slice(-80),
      state: node.state,
      level: node.level_dbfs,
      detail: node.startup?.message ?? node.startup?.phase,
    };
  }
  const captions: Caption[] = data.subtitles.map((c) => {
    const outputs = Object.entries(
      data.config.nodes.find((n) => n.name === c.node)?.outputs ?? {},
    );
    return {
      id: `${data.epoch}:${c.topic}:${c.segment_id}`,
      segment: c.segment_id,
      source: c.source ?? 'Live source',
      producer: c.node,
      port: outputs.find(([, t]) => t === c.topic)?.[0] ?? (outputs.length === 1 ? outputs[0][0] : undefined),
      topic: c.topic,
    language: c.lang,
    text: c.text,
    final: c.is_final,
    revision: c.revision,
    start: c.media_start ?? NaN,
    end: c.media_end ?? NaN,
    latency: round(c.end_to_end_ms),
    stage: round(c.stage_latency_ms[c.node] ?? 0),
    trace: Object.entries(c.stage_latency_ms).map(([name, ms]) => ({
        name,
        ms: round(ms),
        wait: null,
        kind: data.config.nodes.find((n) => n.name === name)?.impl ?? '',
      })),
    };
  });
  const media = Object.fromEntries(
    Object.entries(data.media).map(([id, m]) => [id, {
      position: m.position,
      paused: m.paused,
      autoPaused: m.auto_paused,
      status: m.status,
      duration: previous.media[id]?.duration ?? 0,
    }]),
  );
  return {
    status: data.state,
    connected: true,
    failure: data.error,
    transportError: previous.transportError,
    epoch: data.epoch,
    time: Math.max(0, ...Object.values(media).map((m) => m.position)),
    metrics,
    media,
    captions,
    logs: data.logs.map((l) =>
      `${new Date(l.time * 1000).toLocaleTimeString()} · ${l.level}${
        l.node ? ` · ${l.node}` : ''
      } · ${l.message}`
    ),
  };
}
export async function api<T>(path: string, body?: unknown): Promise<T> {
  const response = await fetch(
    `/api/${path}`,
    body === undefined ? { signal: AbortSignal.timeout(15000) } : {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(30000),
    },
  );
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.error ?? `Backend request failed (${response.status}).`);
  }
  return response.json();
}
let socket: WebSocket | undefined;
let reconnect: ReturnType<typeof setTimeout> | undefined;
let disposed = true, initialized = false, commandPending = false;
let generation = 0;
let lastAdoptedEpoch = -1;
let lastFailureKey: string | null = null;
let transportIncident = false;
type ClockCommand = { node: string; position: number; paused: boolean; epoch: number };
const clocks = new Map<string, ClockCommand>();
let sendingClock = false;
async function flushClocks() {
  if (sendingClock) return;
  sendingClock = true;
  try {
    while (clocks.size && !disposed) {
      const [id, clock] = clocks.entries().next().value!;
      clocks.delete(id);
      if (clock.epoch !== runtime.get().epoch || commandPending) continue;
      try {
        // Commands have acknowledgements; snapshots remain on the WebSocket.
        // Keep only the latest pending clock per source when a request is slow.
        await api<Snapshot>('playback', clock);
      } catch (error) {
        if (clock.epoch === runtime.get().epoch) reportError(error);
      }
    }
  } finally {
    sendingClock = false;
  }
}
export function reportError(error: unknown, key?: string) {
  publishNotification(error instanceof Error ? error.message : String(error), {
    key,
    level: 'error',
    persistent: true,
  });
}
function accept(data: Snapshot, force = false) {
  const previous = runtime.get();
  if (data.epoch < previous.epoch) return;
  if (commandPending && !force) return;
  if (activeState(data.state) && data.epoch !== lastAdoptedEpoch) {
    const doc = fromBackend(data.config);
    // Keep local layout when a seek recreates the same graph.
    const positions = new Map(editor.get().document.nodes.map((n) => [n.id, n.position]));
    doc.nodes.forEach((n) => {
      n.position = positions.get(n.id) ?? n.position;
    });
    commands.adopt(doc, true);
    lastAdoptedEpoch = data.epoch;
  }
  commands.lock(activeState(data.state));
  const failureKey = data.error ? `run-failure:${data.epoch}` : null;
  if (lastFailureKey && lastFailureKey !== failureKey) resolveNotification(lastFailureKey);
  if (failureKey && failureKey !== lastFailureKey) reportError(data.error, failureKey);
  lastFailureKey = failureKey;
  runtime.set(mapSnapshot(data, previous));
}
export function connectBackend() {
  disposed = false;
  const token = ++generation;
  async function connect() {
    try {
      const bootstrap = await api<
        { catalog: CatalogEntry[]; config: BackendConfig; snapshot: Snapshot }
      >('bootstrap');
      if (disposed || generation !== token) return;
      // The Python process may have restarted with changed modules or option defaults.
      // Refresh definitions on every successful handshake while preserving the draft.
      installCatalog(bootstrap.catalog);
      if (!initialized) {
        if (!editor.get().document.backend) {
          commands.adopt(fromBackend(bootstrap.config), activeState(bootstrap.snapshot.state));
        }
        initialized = true;
      }
      // A restarted server has a new epoch namespace. Idle drafts remain untouched.
      if (lastFailureKey) resolveNotification(lastFailureKey);
      lastFailureKey = null;
      transportIncident = false;
      resolveNotification('transport');
      runtime.set({ ...runtime.get(), epoch: -1, transportError: null });
      lastAdoptedEpoch = -1;
      accept(bootstrap.snapshot, true);
      const url = new URL('/api/events', location.href);
      url.protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
      socket = new WebSocket(url);
      socket.onmessage = (event) => {
        if (generation !== token || disposed) return;
        try {
          const data = JSON.parse(event.data);
          if (data.type === 'snapshot') accept(data);
          else if (data.type === 'command_error') reportError(data.message);
        } catch (error) {
          reportError(error);
        }
      };
      socket.onclose = () => retry();
      socket.onerror = () => socket?.close();
    } catch (error) {
      retry(error);
    }
  }
  function retry(error?: unknown) {
    if (disposed || generation !== token) return;
    const message = error instanceof Error ? error.message : 'Backend disconnected. Reconnecting…';
    runtime.set({
      ...runtime.get(),
      connected: false,
      transportError: message,
    });
    if (!transportIncident) reportError(message, 'transport');
    transportIncident = true;
    // An interrupted run stays locked until the server confirms its state.
    clearTimeout(reconnect);
    reconnect = setTimeout(connect, 1500);
  }
  void connect();
  return () => {
    disposed = true;
    ++generation;
    clearTimeout(reconnect);
    socket?.close();
  };
}
async function command(path: string, body: unknown) {
  if (commandPending || !runtime.get().connected) return;
  commandPending = true;
  if (lastFailureKey) resolveNotification(lastFailureKey);
  lastFailureKey = null;
  commands.lock(true);
  runtime.set({
    ...runtime.get(),
    failure: null,
    status: path === 'run' ? 'starting' : path === 'seek' ? 'seeking' : 'stopping',
  });
  try {
    accept(await api<Snapshot>(path, body), true);
  } catch (error) {
    // Reconcile an uncertain POST without retrying an operation that may have succeeded.
    try {
      accept((await api<{ snapshot: Snapshot }>('bootstrap')).snapshot, true);
    } catch {
      runtime.set({ ...runtime.get(), connected: false });
    }
    const message = error instanceof Error ? error.message : String(error);
    if (runtime.get().failure !== message) reportError(message);
  } finally {
    commandPending = false;
  }
}
export const session = {
  async start(document: Document): Promise<string[]> {
    if (!runtime.get().connected) return ['Connect to the backend before running.'];
    if (commandPending || activeState(runtime.get().status)) return ['A run is already active.'];
    try {
      // /run validates authoritatively before starting; avoid an edit window
      // between a separate preflight request and accepting the run.
      await command('run', { config: toBackend(document) });
      return [];
    } catch (error) {
      return [error instanceof Error ? error.message : String(error)];
    }
  },
  stop() {
    void command('stop', {});
  },
  toggle() {
    const media = Object.entries(runtime.get().media);
    const paused = media.some(([, m]) => !m.paused);
    for (const [id, m] of media) this.clock(id, m.position, paused);
  },
  pauseSource(id: string) {
    const m = runtime.get().media[id];
    if (m) this.clock(id, m.position, !m.paused);
  },
  clock(id: string, position: number, paused: boolean) {
    const s = runtime.get(), m = s.media[id];
    if (!m || s.status !== 'running' || !s.connected || commandPending) {
      return;
    }
    clocks.set(id, { node: id, position, paused, epoch: s.epoch });
    void flushClocks();
    runtime.set({ ...s, media: { ...s.media, [id]: { ...m, position, paused } } });
  },
  async seek(id: string, position: number) {
    if (runtime.get().status !== 'running') return;
    await command('seek', {
      node: id,
      position: Math.max(0, position),
      epoch: runtime.get().epoch,
    });
  },
};
export function setDuration(id: string, duration: number) {
  if (!Number.isFinite(duration) || duration <= 0) return;
  const s = runtime.get();
  const m: MediaClock = s.media[id] ?? { position: 0, paused: true, duration };
  if (m.duration !== duration) {
    runtime.set({ ...s, media: { ...s.media, [id]: { ...m, duration } } });
  }
}
export function timestamp(seconds: number) {
  if (!Number.isFinite(seconds)) return '—';
  return `${Math.floor(seconds / 60).toString().padStart(2, '0')}:${
    Math.floor(seconds % 60).toString().padStart(2, '0')
  }`;
}

// Release this module's socket when Vite replaces the adapter itself.
const hot = (import.meta as { hot?: { dispose(callback: () => void): void } }).hot;
hot?.dispose(() => {
  disposed = true;
  ++generation;
  clearTimeout(reconnect);
  clocks.clear();
  socket?.close();
});
