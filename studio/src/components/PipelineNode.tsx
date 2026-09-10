import { memo, useLayoutEffect, useState } from 'react';
import type { CSSProperties } from 'react';
import { Handle, Position, useStore as useFlowStore, useUpdateNodeInternals } from '@xyflow/react';
import type { Node, NodeProps } from '@xyflow/react';
import {
  AudioLines,
  Captions,
  ChevronDown,
  Copy,
  Languages,
  LockKeyhole,
  MoreHorizontal,
  ScanLine,
  Trash2,
} from 'lucide-react';
import { catalog, payloadColors, ports } from '../domain/model';
import type { PipelineNode as Model } from '../domain/model';
import { nodeFields } from '../domain/backend';
import { commands } from '../state/editor';
import { useStore } from '../state/useStore';
import { runtime } from '../runtime';
import { FieldControl, IconButton } from './Controls';
import { MediaSource } from './MediaSource';

export const kindIcons = {
  source: AudioLines,
  segmenter: ScanLine,
  transcriber: Captions,
  translator: Languages,
  output: Captions,
};
export type FlowNode = Node<{ model: Model; locked: boolean; profile: boolean }, 'pipeline'>;

function LiveStats({ id, profile }: { id: string; profile: boolean }) {
  const s = useStore(runtime), m = s.metrics[id];
  if (!m) return null;
  return (
    <div title={m.detail} className={`node-stats ${m.queue ? 'pressure' : ''}`}>
      <div className='stat-line'>
        <span>
          <i className={`status-dot ${s.status === 'running' ? 'active' : ''}`} />
          {m.state ?? (s.status === 'idle'
            ? 'Last run'
            : s.status === 'paused' || s.media[id]?.paused
            ? 'Paused'
            : m.queue
            ? 'Queued'
            : 'Processing')}
        </span>
        <strong>
          {m.mean}
          <small>ms</small>
        </strong>
      </div>
      {m.level !== undefined && (
        <div className='stat-detail'>
          Audio level <b>{m.level.toFixed(1)} dBFS</b>
        </div>
      )}
      {profile && (
        <>
          <div className='stat-detail'>
            <span>
              p95 <b>{m.p95} ms</b>
            </span>
            <span>
              queue <b>{m.queue}</b>
            </span>
            <span>
              drops <b>{m.drops}</b>
            </span>
          </div>
        </>
      )}
    </div>
  );
}
export const PipelineNode = memo(function PipelineNode({ data, selected }: NodeProps<FlowNode>) {
  const { model, locked, profile } = data,
    definition = catalog[model.kind],
    Icon = kindIcons[(definition.role ?? model.kind) as keyof typeof kindIcons] ?? ScanLine;
  const fields = nodeFields(model);
  const inputs = Object.entries(ports(model, 'target')),
    outputs = Object.entries(ports(model, 'source'));
  const zoom = useFlowStore((state) => state.transform[2]);
  const updateNodeInternals = useUpdateNodeInternals();
  const [advanced, setAdvanced] = useState(false), [menu, setMenu] = useState(false);
  useLayoutEffect(() => updateNodeInternals(model.id), [model.id, updateNodeInternals]);
  return (
    <article
      className={`pipeline-node ${selected ? 'selected' : ''}`}
      style={{
        '--node-color': definition.color,
        '--port-hit-size': `${32 / Math.min(zoom, 1)}px`,
      } as CSSProperties}
      aria-label={`${model.name} settings`}
    >
      <header className='node-header'>
        <span className='node-icon'>
          <Icon size={18} />
        </span>
        <div>
          <h2>{model.name}</h2>
          <span className='implementation'>{definition.impl}</span>
        </div>
        <div className='node-actions nodrag'>
          <IconButton
            label={`Actions for ${model.name}`}
            aria-expanded={menu}
            onClick={() => setMenu(!menu)}
          >
            <MoreHorizontal size={18} />
          </IconButton>
        </div>
      </header>
      {menu && (
        <div className='node-menu nodrag'>
          <button
            disabled={locked}
            onClick={() => {
              commands.duplicate(model.id);
              setMenu(false);
            }}
          >
            <Copy size={14} />Duplicate
          </button>
          <button disabled={locked} onClick={() => commands.remove([model.id])}>
            <Trash2 size={14} />Delete
          </button>
          <button onClick={() => setMenu(false)}>Close</button>
        </div>
      )}
      <div className='node-body'>
        {['media', 'wav', 'ffmpeg'].includes(model.kind) && (
          <MediaSource model={model} locked={locked} />
        )}
        {fields.filter((f) => !f.advanced).map((f) => (
          <FieldControl
            key={f.key}
            field={f}
            value={model.options[f.key]}
            disabled={locked}
            onChange={(value) => commands.option(model.id, f.key, value)}
          />
        ))}
        {definition.role === 'output' && (
          <div className='output-note'>
            <span className='tiny-dot' /> Text streams also appear in the monitor
          </div>
        )}
        {(fields.some((f) => f.advanced) || definition.backend) && (
          <>
            <button
              className={`advanced-button nodrag ${advanced ? 'expanded' : ''}`}
              aria-expanded={advanced}
              onClick={() => setAdvanced(!advanced)}
            >
              <ChevronDown size={13} />Advanced{locked && <LockKeyhole size={12} />}
            </button>
            {advanced && (
              <div className='advanced-fields'>
                <FieldControl
                  field={{ key: 'enabled', label: 'Enabled', type: 'toggle', default: true }}
                  value={model.enabled ?? true}
                  disabled={locked}
                  onChange={(v) => commands.node(model.id, { enabled: Boolean(v) })}
                />
                <FieldControl
                  field={{
                    key: 'mode',
                    label: 'Queue policy',
                    type: 'select',
                    default: 'default',
                    choices: ['default', 'blocking', 'drop', 'catchup'],
                  }}
                  value={model.mode ?? 'default'}
                  disabled={locked}
                  onChange={(v) => commands.node(model.id, { mode: String(v) })}
                />
                {model.raw?.skip_if_finalized && (
                  <p className='output-note'>Skip if finalized: {model.raw.skip_if_finalized}</p>
                )}
                {definition.error && <p className='field-error'>{definition.error}</p>}

                {fields.filter((f) => f.advanced).map((f) => (
                  <FieldControl
                    key={f.key}
                    field={f}
                    value={model.options[f.key]}
                    disabled={locked}
                    onChange={(value) => commands.option(model.id, f.key, value)}
                  />
                ))}
              </div>
            )}
          </>
        )}
      </div>
      <div className='node-ports'>
        <div className='port-column'>
          {inputs.map(([port, payload]) => (
            <div className='port input' key={port}>
              <Handle
                type='target'
                position={Position.Left}
                id={port}
                isConnectable={!locked}
                aria-label={`${model.name} ${port} input`}
                title={`${payload} · drag to connect or add a node`}
                style={{ '--port-color': payloadColors[payload] ?? '#aab6c4' } as CSSProperties}
              />
              <span title={payload}>{port}</span>
            </div>
          ))}
        </div>
        <div className='port-column'>
          {outputs.map(([port, payload]) => (
            <div className='port output' key={port}>
              <span title={payload}>{port}</span>
              <Handle
                type='source'
                position={Position.Right}
                id={port}
                isConnectable={!locked}
                aria-label={`${model.name} ${port} output`}
                title={`${payload} · drag to connect or add a node`}
                style={{ '--port-color': payloadColors[payload] ?? '#aab6c4' } as CSSProperties}
              />
            </div>
          ))}
        </div>
      </div>
      <LiveStats id={model.id} profile={profile} />
    </article>
  );
});
