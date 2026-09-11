import { useEffect, useRef, useState } from 'react';
import { FileAudio, FolderOpen, Pause, Play, RotateCcw, RotateCw } from 'lucide-react';
import { runtime, session, setDuration, timestamp } from '../runtime';
import { textEndpoints } from '../domain/backend';
import type { PipelineNode } from '../domain/model';
import { commands, editor } from '../state/editor';
import { useStore } from '../state/useStore';
import { FieldControl, IconButton } from './Controls';

export function MediaSource({ model, locked }: { model: PipelineNode; locked: boolean }) {
  const id = model.id, s = useStore(runtime), clock = s.media[id];
  const { document } = useStore(editor);
  // The overlay select shares the subtitle dock's endpoint list; picking an
  // unwired endpoint wires it (assigns its topic) as a side of the same commit.
  const endpoints = textEndpoints(document);
  const attached = model.overlay ? endpoints.find((e) => e.topic === model.overlay) : undefined;
  const input = useRef<HTMLInputElement>(null), video = useRef<HTMLVideoElement>(null);
  const [error, setError] = useState(''), [uploading, setUploading] = useState(false);
  const [duration, durationChanged] = useState(0),
    [draft, setDraft] = useState<number | null>(null);
  const filePath = String(model.options.url ?? model.options.path ?? model.options.source ?? '');
  const local = filePath && !/^(https?|rtsp|rtmp):/.test(filePath) && !model.options.device;
  const url = local ? `/api/media?path=${encodeURIComponent(filePath)}` : undefined;
  const playing = s.connected && s.status === 'running' && clock && !clock.paused &&
    !clock.autoPaused;
  const position = clock?.position ?? 0;
  const lastEpoch = useRef(-1);
  useEffect(() => {
    const el = video.current;
    if (!el || !url) return;
    if (lastEpoch.current !== s.epoch && Number.isFinite(el.duration)) {
      el.currentTime = position;
      lastEpoch.current = s.epoch;
      setDuration(id, el.duration);
    }
    if (playing && el.paused) {
      el.play().catch(() => {
        session.clock(id, el.currentTime, true);
        setError('Press Play source to allow playback.');
      });
    }
    if (!playing && !el.paused) el.pause();
  }, [playing, s.epoch, id, url, position, duration]);
  const seekable = s.connected && s.status === 'running' && !!clock &&
    duration > 0;
  async function seek(to: number) {
    if (!seekable) return;
    setDraft(null);
    await session.seek(id, Math.min(duration, Math.max(0, to)));
  }
  return (
    <div className='media-source nodrag nowheel'>
      <div className='media-preview'>
        {url
          ? (
            <video
              ref={video}
              src={url}
              playsInline
              preload='metadata'
              onError={() =>
                setError('Cannot preview media. Check the path and browser codec support.')}
              onLoadedMetadata={(e) => {
                durationChanged(e.currentTarget.duration);
                setDuration(id, e.currentTarget.duration);
                setError('');
              }}
              onTimeUpdate={(e) => {
                if (!e.currentTarget.seeking && playing) {
                  session.clock(
                    id,
                    e.currentTarget.currentTime,
                    false,
                  );
                }
              }}
              onEnded={(e) => session.clock(id, e.currentTarget.currentTime, true)}
            />
          )
          : (
            <div className='preview-heading'>
              <FileAudio size={16} />
              {model.options.device
                ? 'Live device'
                : filePath
                ? 'Live stream'
                : 'Choose an audio or video file'}
            </div>
          )}
      </div>
      <div className='filename'>
        <span title={filePath}>{filePath.split('/').at(-1) || 'No file selected'}</span>
        <IconButton
          label='Choose local media'
          disabled={locked || uploading || !s.connected}
          onClick={() => input.current?.click()}
        >
          <FolderOpen size={15} />
        </IconButton>
      </div>
      <input
        ref={input}
        hidden
        type='file'
        accept='audio/*,video/*'
        onChange={async (e) => {
          const file = e.target.files?.[0];
          e.target.value = '';
          if (!file || locked) return;
          setUploading(true);
          setError('');
          try {
            const response = await fetch(`/api/upload?name=${encodeURIComponent(file.name)}`, {
              method: 'POST',
              body: file,
            });
            const result = await response.json();
            if (!response.ok) throw new Error(result.error);
            commands.option(id, model.kind === 'wav' ? 'path' : 'url', result.path);
          } catch (err) {
            setError(err instanceof Error ? err.message : 'Upload failed.');
          } finally {
            setUploading(false);
          }
        }}
      />
      {local && (
        <>
          <input
            className='seek'
            aria-label='Seek media'
            type='range'
            min='0'
            max={duration || 1}
            step='0.1'
            value={draft ?? position}
            disabled={!seekable}
            onChange={(e) => setDraft(Number(e.target.value))}
            onPointerUp={(e) => void seek(Number(e.currentTarget.value))}
            onKeyUp={(e) => {
              if (
                ['ArrowLeft', 'ArrowRight', 'Home', 'End', 'PageUp', 'PageDown'].includes(e.key)
              ) void seek(Number(e.currentTarget.value));
            }}
          />
          <div className='transport'>
            <span className='mono'>{timestamp(draft ?? position)}</span>
            <div>
              <IconButton
                label='Back 5 seconds'
                disabled={!seekable}
                onClick={() => void seek(position - 5)}
              >
                <RotateCcw size={14} />
              </IconButton>
              <IconButton
                label={playing ? 'Pause source' : 'Play source'}
                disabled={!s.connected || s.status !== 'running' || !clock}
                onClick={() => {
                  setError('');
                  if (!playing) void video.current?.play().catch((err) => setError(String(err)));
                  session.clock(id, video.current?.currentTime ?? position, !!playing);
                }}
              >
                {playing ? <Pause size={16} /> : <Play size={16} />}
              </IconButton>
              <IconButton
                label='Forward 5 seconds'
                disabled={!seekable}
                onClick={() => void seek(position + 5)}
              >
                <RotateCw size={14} />
              </IconButton>
            </div>
            <span className='mono muted'>{duration ? timestamp(duration) : '—'}</span>
          </div>
          {clock?.autoPaused && <p className='output-note'>{clock.status}</p>}
          <label className='field field-select nodrag nowheel'>
            <span>Overlay subtitles</span>
            <select
              disabled={locked}
              value={model.overlay ? attached?.id ?? model.overlay : ''}
              onChange={(e) => {
                const value = e.target.value;
                if (value === (model.overlay ?? '')) return;
                const chosen = endpoints.find((x) => x.id === value) ?? null;
                commands.overlay(id, chosen ? { node: chosen.node, port: chosen.port } : null);
              }}
            >
              <option value=''>None</option>
              {model.overlay && !attached && (
                <option value={model.overlay}>{model.overlay} (missing)</option>
              )}
              {endpoints.map((e) => <option key={e.id} value={e.id}>{e.label}</option>)}
            </select>
          </label>
          <FieldControl
            field={{ key: 'auto_pause', label: 'Auto pause', type: 'toggle', default: false }}
            value={Boolean(model.autoPause)}
            disabled={locked || !model.overlay}
            onChange={(v) => commands.node(id, { autoPause: Boolean(v) })}
          />
          {!model.overlay && (
            <p className='output-note'>Auto pause waits for an attached subtitle endpoint.</p>
          )}
        </>
      )}
      {uploading && <p className='output-note'>Uploading to the local backend…</p>}
      {error && <p className='field-error' role='alert'>{error}</p>}
    </div>
  );
}
