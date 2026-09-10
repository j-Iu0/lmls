import { useEffect, useRef, useState } from 'react';
import type { CSSProperties } from 'react';
import {
  Activity,
  ArrowDown,
  Captions,
  Check,
  ChevronDown,
  ChevronUp,
  Maximize2,
  Minimize2,
  SlidersHorizontal,
} from 'lucide-react';
import { runtime, timestamp } from '../runtime';
import type { Caption } from '../runtime';
import { ports } from '../domain/model';
import { editor } from '../state/editor';
import { useStore } from '../state/useStore';
import { IconButton } from './Controls';

export function SubtitleDock(
  { selected, onSelect, profile, onToggleProfile }: {
    selected: string | null;
    onSelect: (caption: Caption) => void;
    profile: boolean;
    onToggleProfile: () => void;
  },
) {
  const s = useStore(runtime);
  const { document } = useStore(editor);
  const [height, setHeight] = useState(230),
    [collapsed, setCollapsed] = useState(false),
    [expanded, setExpanded] = useState(false);
  const [filterOpen, setFilterOpen] = useState(false),
    [picked, setPicked] = useState<string[]>([]),
    [following, setFollowing] = useState(true);
  const feed = useRef<HTMLDivElement>(null);
  const rows = s.captions;
  // Every text endpoint of the pipeline, grouped by producing node. Port names
  // are scoped by module, so labels pair each node with its ports. Observed
  // endpoints are merged in so a caption whose node left the draft stays filterable.
  const endpoints = new Map<string, string[]>();
  const addEndpoint = (node: string, port: string) => {
    const ports_ = endpoints.get(node) ?? [];
    if (!ports_.includes(port)) endpoints.set(node, [...ports_, port]);
  };
  for (const node of document.nodes) {
    if (node.enabled === false) continue;
    for (const [port, payload] of Object.entries(ports(node, 'source'))) {
      if (payload === 'text') addEndpoint(node.name, port);
    }
  }
  for (const row of rows) {
    if (row.port) addEndpoint(row.producer, row.port);
  }
  const groups = new Map<string, Caption[]>();
  for (const row of rows.filter((row) => picked.includes(`${row.producer}:${row.port ?? ''}`))) {
    const key = `${row.source}:${row.segment ?? row.start}`;
    groups.set(key, [...(groups.get(key) ?? []), row]);
  }
  useEffect(() => {
    if (following && feed.current) feed.current.scrollTop = feed.current.scrollHeight;
  }, [s.captions, following]);
  return (
    <section
      className={`subtitle-dock ${collapsed ? 'collapsed' : ''} ${expanded ? 'expanded' : ''}`}
      style={{ '--dock-height': `${height}px` } as CSSProperties}
      aria-label='Subtitle monitor'
    >
      <div
        className='dock-resizer'
        role='separator'
        aria-orientation='horizontal'
        aria-label='Resize subtitle monitor'
        aria-valuemin={160}
        aria-valuemax={1000}
        aria-valuenow={height}
        tabIndex={0}
        onKeyDown={(e) => {
          if (e.key === 'ArrowUp' || e.key === 'ArrowDown') {
            e.preventDefault();
            setHeight((h) =>
              Math.min(
                window.innerHeight * 0.75,
                Math.max(160, h + (e.key === 'ArrowUp' ? 24 : -24)),
              )
            );
          }
        }}
        onPointerDown={(e) => {
          e.currentTarget.setPointerCapture(e.pointerId);
          e.currentTarget.dataset.start = String(e.clientY);
          e.currentTarget.dataset.height = String(height);
        }}
        onPointerMove={(e) => {
          if (e.currentTarget.hasPointerCapture(e.pointerId)) {
            setCollapsed(false);
            setHeight(
              Math.max(
                160,
                Math.min(
                  window.innerHeight * 0.75,
                  Number(e.currentTarget.dataset.height) + Number(e.currentTarget.dataset.start) -
                    e.clientY,
                ),
              ),
            );
          }
        }}
      />
      <header className='dock-header'>
        <div>
          <Captions size={17} />
          <h2>Subtitle monitor</h2>
          <span className='count-badge'>{groups.size}</span>
          <span className='dock-subtitle'>
            {s.epoch === 0
              ? 'Sample transcript'
              : s.status === 'idle'
              ? 'Last run'
              : 'Live revisions'}
          </span>
        </div>
        <div>
          <button
            className='text-button profile-toggle'
            aria-label='Toggle profiler'
            aria-pressed={profile}
            onClick={onToggleProfile}
          >
            <Activity size={15} />
            <span>Profiler</span>
          </button>
          <button
            className='text-button'
            onClick={() => setFilterOpen(!filterOpen)}
            aria-expanded={filterOpen}
          >
            <SlidersHorizontal size={14} />
            <span>Endpoints</span>
          </button>
          <IconButton
            label={expanded ? 'Restore subtitle monitor' : 'Expand subtitle monitor'}
            onClick={() => {
              setExpanded(!expanded);
              setCollapsed(false);
            }}
          >
            {expanded ? <Minimize2 size={15} /> : <Maximize2 size={15} />}
          </IconButton>
          <IconButton
            label={collapsed ? 'Show subtitle monitor' : 'Collapse subtitle monitor'}
            onClick={() => {
              setCollapsed(!collapsed);
              setExpanded(false);
            }}
          >
            {collapsed ? <ChevronUp size={17} /> : <ChevronDown size={17} />}
          </IconButton>
        </div>
      </header>
      {filterOpen && (
        <div className='endpoint-picker'>
          {endpoints.size
            ? [...endpoints.entries()].map(([node, portNames]) => (
              <div className='endpoint-group' key={node}>
                <span className='endpoint-node'>{node}</span>
                {portNames.map((port) => {
                  const id = `${node}:${port}`;
                  return (
                    <label key={port}>
                      <input
                        type='checkbox'
                        checked={picked.includes(id)}
                        onChange={(e) =>
                          setPicked(
                            e.target.checked ? [...picked, id] : picked.filter((p) => p !== id),
                          )}
                      />
                      {port}
                    </label>
                  );
                })}
              </div>
            ))
            : <span>Text endpoints appear once a pipeline is loaded.</span>}
        </div>
      )}
      {!collapsed && (
        <div
          ref={feed}
          className='caption-feed'
          role='log'
          aria-label='Subtitle events'
          aria-live='off'
          onScroll={(e) => {
            const el = e.currentTarget;
            setFollowing(el.scrollHeight - el.scrollTop - el.clientHeight < 45);
          }}
        >
          {groups.size === 0 && (
            <div className='empty-captions'>
              <Captions size={25} />
              <span>
                {s.status === 'idle'
                  ? 'Your words will appear here.'
                  : 'Listening for the next segment…'}
              </span>
              <small>
                {picked.length
                  ? 'Original text, translations, and every revision.'
                  : 'Enable an endpoint to see its subtitles.'}
              </small>
            </div>
          )}
          {[...groups.entries()].map(([key, group], index) => (
            <article className='caption-group' key={key}>
              <div className='segment-label'>
                <span className='mono'>{String(index + 1).padStart(2, '0')}</span>
                <span>{timestamp(group[0].start)} — {timestamp(group[0].end)}</span>
                <span>{group[0].source}</span>
              </div>
              <div className='caption-lines'>
                {group.map((row) => (
                  <button
                    key={row.id}
                    onClick={() => onSelect(row)}
                    className={`caption-line ${selected === row.id ? 'selected' : ''}`}
                    title='Inspect timing for this subtitle'
                  >
                    <div className='caption-meta'>
                      <span
                        className={`language-chip ${row.language === 'English' ? 'original' : ''}`}
                      >
                        {row.language}
                      </span>
                      {row.topic && <span className='mono topic'>{row.topic}</span>}
                      <span className={`final-chip ${row.final ? '' : 'partial'}`}>
                        {row.final ? <Check size={11} /> : <i className='status-dot active' />}
                        {row.final ? 'Final' : 'Partial'}
                      </span>
                      <span className='revision'>r{row.revision}</span>
                      <span className='caption-latency'>
                        <b>{row.latency} ms</b> end to end
                      </span>
                    </div>
                    <p>
                      {row.text}
                      <span className='caption-cursor'>{row.final ? '' : '▍'}</span>
                    </p>
                    <div className='caption-detail'>
                      {row.producer} · stage {row.stage} ms · {Number.isFinite(row.start)
                        ? `${row.start.toFixed(2)}–${row.end.toFixed(2)} s`
                        : 'Live timestamps'}
                    </div>
                  </button>
                ))}
              </div>
            </article>
          ))}
        </div>
      )}
      {!following && !collapsed && (
        <button
          className='follow-button'
          onClick={() => setFollowing(true)}
        >
          <ArrowDown size={13} />Follow latest
        </button>
      )}
    </section>
  );
}
