import type { Document, Kind } from '../domain/model.ts';
import { validate } from '../domain/model.ts';
import { commands, editor } from '../state/editor.ts';
import { createStore } from '../state/store.ts';

import type { Caption, Metric, RuntimeAdapter, RuntimeSnapshot } from './types.ts';

const script = [
  ['The best interfaces get out of your way.', 'Giao diện tốt nhất không cản trở bạn.'],
  [
    'You can focus on the idea, and let the tools do the rest.',
    'Bạn có thể tập trung vào ý tưởng, và để công cụ làm phần còn lại.',
  ],
  [
    'Every small detail changes how the whole system feels.',
    'Mỗi chi tiết nhỏ thay đổi cảm nhận về toàn bộ hệ thống.',
  ],
  [
    'Make something simple. Then make it work beautifully.',
    'Hãy tạo ra điều đơn giản. Rồi làm cho nó hoạt động thật tốt.',
  ],
];
const translations: Record<string, string[]> = {
  Vietnamese: script.map((row) => row[1]),
  Chinese: [
    '最好的界面不会妨碍你。',
    '你可以专注于想法，让工具完成剩下的工作。',
    '每个小细节都会改变整个系统的体验。',
    '做一些简单的东西，然后让它出色地工作。',
  ],
  Japanese: [
    '最高のインターフェースは邪魔をしません。',
    'アイデアに集中し、残りはツールに任せましょう。',
    '小さな細部がシステム全体の印象を変えます。',
    'シンプルなものを作り、美しく機能させましょう。',
  ],
  Spanish: [
    'Las mejores interfaces no se interponen en tu camino.',
    'Puedes centrarte en la idea y dejar el resto a las herramientas.',
    'Cada detalle cambia cómo se siente todo el sistema.',
    'Crea algo sencillo. Luego haz que funcione de maravilla.',
  ],
  French: [
    'Les meilleures interfaces savent se faire oublier.',
    'Concentrez-vous sur votre idée et laissez les outils faire le reste.',
    'Chaque détail change la perception du système.',
    'Créez quelque chose de simple. Puis faites-le fonctionner à merveille.',
  ],
};
const base: Record<Kind, number> = {
  source: 2,
  segmenter: 12,
  transcriber: 184,
  translator: 268,
  output: 1,
};
export const runtime = createStore<RuntimeSnapshot>({
  status: 'idle',
  failure: null,
  transportError: null,
  epoch: 0,
  time: 0,
  media: {},
  metrics: {},
  captions: [],
  logs: [],
});
let active: Document | null = null;
let timer: ReturnType<typeof setInterval> | undefined;

function upstream(doc: Document, id: string): string[] {
  const found = new Set<string>();
  const visit = (at: string) => {
    for (const e of doc.edges.filter((e) => e.target === at)) {
      if (!found.has(e.source)) {
        found.add(e.source);
        visit(e.source);
      }
    }
  };
  visit(id);
  return [...found];
}

export function advanceSimulation() {
  const s = runtime.get();
  if (!active || s.status !== 'running') return;
  const time = s.time + 0.25;
  const media = Object.fromEntries(
    Object.entries(s.media).map((
      [id, m],
    ) => [id, {
      ...m,
      position: m.paused ? m.position : Math.min(m.duration, m.position + 0.25),
      paused: m.paused || m.position + 0.25 >= m.duration,
    }]),
  );
  const metrics = { ...s.metrics };
  const captions = [...s.captions];
  for (const n of active.nodes) {
    const sourceIds = n.kind === 'source'
      ? [n.id]
      : upstream(active, n.id).filter((id) =>
        active!.nodes.find((n) => n.id === id)?.kind === 'source'
      );
    if (!sourceIds.some((id) => !media[id]?.paused)) continue;
    const sample = Math.round(base[n.kind] * (1 + Math.sin(time * 1.4 + n.id.length) * 0.14));
    const old = metrics[n.id], samples = [...(old?.samples ?? []), sample].slice(-80);
    const sorted = [...samples].sort((a, b) => a - b);
    metrics[n.id] = {
      mean: Math.round(samples.reduce((a, b) => a + b, 0) / samples.length),
      p50: sorted[Math.floor((sorted.length - 1) * 0.5)],
      p95: sorted[Math.floor((sorted.length - 1) * 0.95)],
      queue: n.kind === 'translator' && Math.floor(time) % 9 === 6 ? 2 : 0,
      drops: 0,
      count: (old?.count ?? 0) + 1,
      samples,
    };
    if (n.kind !== 'transcriber' && n.kind !== 'translator') continue;
    for (const sourceId of sourceIds) {
      const clock = media[sourceId];
      if (!clock || clock.paused || clock.position < 1) continue;
      const segment = Math.floor(clock.position / 6), phase = clock.position % 6;
      const index = segment % script.length;
      const language = n.kind === 'translator' ? String(n.options.target) : 'English';
      const full = n.kind === 'translator'
        ? translations[language]?.[index] ?? script[index][0]
        : script[index][0];
      const final = phase >= 3;
      const text = final
        ? full
        : full.slice(0, Math.max(4, Math.round(full.length * Math.min(1, phase / 3))));
      const id = `${s.epoch}:${sourceId}:${segment}:${n.id}`;
      const ancestry = new Set([...upstream(active, n.id), n.id]);
      const trace = active.nodes.filter((node) => ancestry.has(node.id)).map((node) => ({
        name: node.name,
        ms: metrics[node.id]?.mean ?? base[node.kind],
        wait: node.kind === 'translator' ? 24 : 3,
        kind: node.kind,
      }));
      const row: Caption = {
        id,
        source: sourceId,
        producer: n.name,
        topic: `${n.id}.text`,
        language,
        text,
        final,
        revision: final ? 3 : Math.max(1, Math.ceil(phase)),
        start: segment * 6,
        end: segment * 6 + 3,
        latency: n.kind === 'translator' ? 514 : 216,
        stage: sample,
        trace,
      };
      const existing = captions.findIndex((c) => c.id === id);
      if (existing < 0) captions.push(row);
      else captions[existing] = row;
    }
  }
  runtime.set({ ...s, time, media, metrics, captions: captions.slice(-160) });
}

export const demo: RuntimeAdapter = {
  start(document) {
    if (runtime.get().status !== 'idle') {
      return ['Stop the current simulation before starting another.'];
    }
    const errors = validate(document);
    if (errors.length) return errors;
    active = structuredClone(document);
    commands.lock(true);
    if (timer) clearInterval(timer);
    runtime.set({
      status: 'running',
      epoch: runtime.get().epoch + 1,
      time: 0,
      media: Object.fromEntries(
        document.nodes.filter((n) => n.kind === 'source').map(
          (n) => [n.id, { position: 0, duration: 154, paused: false }],
        ),
      ),
      metrics: {},
      captions: [],
      logs: [
        'Simulation started · configuration locked.',
        `${document.nodes.length} nodes ready. No inference or upload.`,
      ],
    });
    timer = setInterval(advanceSimulation, 250);
    return [];
  },
  stop() {
    if (timer) clearInterval(timer);
    timer = undefined;
    active = null;
    commands.lock(false);
    const s = runtime.get();
    runtime.set({
      ...s,
      status: 'idle',
      media: Object.fromEntries(
        Object.entries(s.media).map(([id, m]) => [id, { ...m, paused: true }]),
      ),
      logs: [...s.logs, 'Stopped · last trace retained.'].slice(-100),
    });
  },
  toggle() {
    const s = runtime.get();
    if (s.status === 'idle') {
      this.start(editor.get().document);
      return;
    }
    runtime.set({ ...s, status: s.status === 'running' ? 'paused' : 'running' });
  },
  seek(id, position) {
    const s = runtime.get(), m = s.media[id];
    if (!m || s.status === 'idle') return;
    runtime.set({
      ...s,
      epoch: s.epoch + 1,
      captions: [],
      media: { ...s.media, [id]: { ...m, position: Math.max(0, Math.min(m.duration, position)) } },
      logs: [...s.logs, `Seek to ${position.toFixed(1)}s · subtitle generation reset.`].slice(-100),
    });
  },
  pauseSource(id) {
    const s = runtime.get(), m = s.media[id];
    if (m) runtime.set({ ...s, media: { ...s.media, [id]: { ...m, paused: !m.paused } } });
  },
};
export function setDuration(id: string, duration: number) {
  const s = runtime.get(), m = s.media[id];
  if (Number.isFinite(duration) && duration > 0) {
    runtime.set({
      ...s,
      media: {
        ...s.media,
        [id]: {
          position: Math.min(m?.position ?? 0, duration),
          paused: m?.paused ?? true,
          duration,
        },
      },
    });
  }
}
export function timestamp(seconds: number) {
  return `${Math.floor(seconds / 60).toString().padStart(2, '0')}:${
    Math.floor(seconds % 60).toString().padStart(2, '0')
  }`;
}

// Replacing the simulator during Vite HMR must release its interval and edit lock.
// The optional host hook keeps this module importable by the Deno domain tests.
const hot = (import.meta as { hot?: { dispose: (callback: () => void) => void } }).hot;
hot?.dispose(() => demo.stop());
