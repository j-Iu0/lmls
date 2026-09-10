import type { Document, Kind } from '../domain/model.ts';

export interface Metric {
  mean: number;
  p50: number;
  p95: number;
  queue: number;
  drops: number;
  count: number;
  samples: number[];
  state?: string;
  detail?: string;
  level?: number;
}
export interface Caption {
  id: string;
  segment?: string;
  source: string;
  producer: string;
  port?: string;
  topic?: string;
  language: string;
  text: string;
  final: boolean;
  revision: number;
  start: number;
  end: number;
  latency: number;
  stage: number;
  trace: { name: string; ms: number; wait: number | null; kind: Kind }[];
}
export interface MediaClock {
  position: number;
  duration: number;
  paused: boolean;
  autoPaused?: boolean;
  status?: string;
}
export interface RuntimeSnapshot {
  status:
    | 'idle'
    | 'running'
    | 'paused'
    | 'starting'
    | 'seeking'
    | 'stopping'
    | 'failed'
    | 'completed';
  connected?: boolean;
  /** Authoritative failure reported by the current backend session. */
  failure?: string | null;
  /** Current transport problem; cleared by a successful backend handshake. */
  transportError?: string | null;
  epoch: number;
  time: number;
  media: Record<string, MediaClock>;
  metrics: Record<string, Metric>;
  captions: Caption[];
  logs: string[];
}
export interface RuntimeAdapter {
  start(document: Document): string[];
  stop(): void;
  toggle(): void;
  seek(id: string, position: number): void;
  pauseSource(id: string): void;
}
