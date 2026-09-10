import { Activity, X } from 'lucide-react';
import { useState } from 'react';
import { catalog } from '../domain/model';
import { runtime } from '../runtime';
import type { Caption } from '../runtime';
import { editor } from '../state/editor';
import { useStore } from '../state/useStore';
import { IconButton } from './Controls';
import { ProcessingHistory } from './ProcessingHistory';

export function Profiler(
  { caption: selectedCaption, onClose, clearCaption }: {
    caption: Caption | null;
    onClose: () => void;
    clearCaption: () => void;
  },
) {
  const s = useStore(runtime), { document } = useStore(editor);
  const caption = s.captions.find((c) => c.id === selectedCaption?.id) ?? selectedCaption;
  const [tab, setTab] = useState<'timing' | 'events'>('timing');
  const max = Math.max(1, ...Object.values(s.metrics).map((m) => m.p95));
  return (
    <aside className='profiler floating-panel' aria-label='Profiler'>
      <header>
        <div>
          <Activity size={16} />
          <h2>Profiler</h2>
          <span className='demo-pill'>LIVE BACKEND</span>
        </div>
        <IconButton label='Close profiler' onClick={onClose}>
          <X size={16} />
        </IconButton>
      </header>
      <div className='profiler-tabs'>
        <button className={tab === 'timing' ? 'active' : ''} onClick={() => setTab('timing')}>
          Timing
        </button>
        <button className={tab === 'events' ? 'active' : ''} onClick={() => setTab('events')}>
          Events <small>{s.logs.length}</small>
        </button>
      </div>
      {s.failure && (
        <p className='profiler-failure' role='alert'>
          <strong>Run failed</strong>
          {s.failure}
        </p>
      )}
      {tab === 'events'
        ? (
          <div className='event-list'>
            {s.logs.length
              ? s.logs.map((log, i) => (
                <p key={`${i}:${log}`}>
                  <span className='mono'>{String(i + 1).padStart(2, '0')}</span>
                  {log}
                </p>
              ))
              : <p>Run the pipeline to see lifecycle events.</p>}
          </div>
        )
        : (
          <div className='profiler-body'>
            {caption && (
              <section className='trace'>
                <div className='trace-heading'>
                  <span>Selected subtitle</span>
                  <button onClick={clearCaption}>Clear</button>
                </div>
                <p>“{caption.text}”</p>
                <div className='latency-total'>
                  <strong>
                    {caption.latency}
                    <small>ms</small>
                  </strong>
                  <span>observed end to end</span>
                </div>
                <div className='trace-bars'>
                  {caption.trace.map((stage, i) => (
                    <div
                      key={`${stage.name}:${i}`}
                      style={{
                        flex: stage.ms,
                        background: catalog[stage.kind]?.color ?? '#aab6c4',
                      }}
                      title={`${stage.name}: ${stage.ms} ms service, queue wait is not measured`}
                    />
                  ))}
                </div>
                {caption.trace.map((stage, i) => (
                  <div className='trace-row' key={`${stage.name}:${i}`}>
                    <i style={{ background: catalog[stage.kind]?.color ?? '#aab6c4' }} />
                    <span>{stage.name}</span>
                    <b>{stage.ms} ms</b>
                    <small>service</small>
                  </div>
                ))}
                <p className='profiler-note'>
                  Bars compare service time. End-to-end timing includes waits and transport.
                </p>
              </section>
            )}
            <div className='section-label'>
              STAGE SERVICE TIME <span>milliseconds</span>
            </div>
            <p className='profiler-note chart-explanation'>
              Each line tracks the lifetime mean processing time as the processed count changes. Up
              to 80 observed updates; spacing is by update, not elapsed time. Queue wait is
              excluded.
            </p>
            {!Object.keys(s.metrics).length && (
              <p className='profiler-note'>
                Run the pipeline to collect stage timing, queue depth and processed counts.
              </p>
            )}
            {document.nodes.map((n) => {
              const m = s.metrics[n.id];
              if (!m) return null;
              return (
                <section key={n.id} className='profile-stage'>
                  <div className='profile-stage-heading'>
                    <span>
                      <i style={{ background: catalog[n.kind].color }} />
                      {n.name}
                    </span>
                    <b>
                      {m.mean} <small>ms</small>
                    </b>
                  </div>
                  <div className='timing-bar-label'>
                    p95 processing time <span>{m.p95} ms</span>
                  </div>
                  <div
                    className='timing-track'
                    title={`p95 processing time: ${m.p95} ms; shared scale across stages`}
                  >
                    <div
                      style={{ width: `${m.p95 / max * 100}%`, background: catalog[n.kind].color }}
                    />
                  </div>
                  <div className='profile-numbers'>
                    <span>
                      p50 <b>{m.p50}</b>
                    </span>
                    <span>
                      p95 <b>{m.p95}</b>
                    </span>
                    <span>
                      mean <b>{m.mean}</b>
                    </span>
                    <span>
                      samples <b>{m.count}</b>
                    </span>
                  </div>
                  <div className='profile-queue'>
                    <span className={m.queue ? 'amber' : ''}>
                      queue {m.queue} · dropped {m.drops}
                    </span>
                  </div>
                  <ProcessingHistory values={m.samples} color={catalog[n.kind].color} />
                </section>
              );
            })}
            <p className='profiler-note'>
              Measured by the Python graph. Means and counts cover the run; percentiles use the most
              recent 2,048 operations. Mock modules report their configured artificial delays.
            </p>
          </div>
        )}
    </aside>
  );
}
