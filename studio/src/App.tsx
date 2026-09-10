import { useCallback, useEffect, useRef, useState } from 'react';
import type { MouseEvent as ReactMouseEvent } from 'react';
import type { Edge, OnConnectEnd } from '@xyflow/react';
import {
  Background,
  BackgroundVariant,
  ReactFlow,
  SelectionMode,
  useEdgesState,
  useNodesState,
  useReactFlow,
  useViewport,
} from '@xyflow/react';
import {
  ArrowUpRight,
  Check,
  Copy,
  Download,
  FilePlus2,
  HelpCircle,
  LockKeyhole,
  Maximize,
  Menu,
  Minus,
  Pause,
  Play,
  Plus,
  Redo2,
  Search,
  Square,
  Trash2,
  Undo2,
  Upload,
  X,
} from 'lucide-react';
import { catalog, connectionError, payloadColors, ports } from './domain/model';
import type { Kind } from './domain/model';
import { commands, editor } from './state/editor';
import { addConnected, compatibleKinds } from './state/addConnected';
import type { ConnectionOrigin } from './state/addConnected';
import { dismissNotification, notifications, publishNotification } from './state/notifications';
import { useStore } from './state/useStore';
import { runtime, session, timestamp } from './runtime';
import { activeState, api } from './runtime/backend';
import { fromBackend, toBackend } from './domain/backend';
import type { BackendConfig } from './domain/backend';
import type { Caption } from './runtime';
import { kindIcons, PipelineNode } from './components/PipelineNode';
import type { FlowNode } from './components/PipelineNode';
import { IconButton } from './components/Controls';
import { SubtitleDock } from './components/SubtitleDock';
import { Profiler } from './components/Profiler';
import { bindMiddlePan } from './canvas/middlePan';

const nodeTypes = { pipeline: PipelineNode };
type Popup = {
  x: number;
  y: number;
  flow: { x: number; y: number };
  node?: string;
  origin?: ConnectionOrigin;
};

function RunControls({ notify }: { notify: (message: string) => void }) {
  const s = useStore(runtime);
  const active = activeState(s.status), paused = Object.values(s.media).every((m) => m.paused);
  return (
    <div className='run-controls'>
      <span className='demo-indicator'>
        <i />
        {s.connected ? s.status : 'Offline'}
      </span>
      {!active
        ? (
          <button
            className='run-button'
            disabled={!s.connected}
            onClick={async () => {
              const errors = await session.start(editor.get().document);
              if (errors.length) notify(errors.join(' '));
            }}
          >
            <Play size={14} fill='currentColor' />Run pipeline
          </button>
        )
        : (
          <>
            <span className='run-clock mono'>{timestamp(s.time)}</span>
            <IconButton
              label={paused ? 'Resume file playback' : 'Pause file playback'}
              disabled={s.status !== 'running' || !Object.keys(s.media).length || !s.connected}
              onClick={() => session.toggle()}
            >
              {paused ? <Play size={16} /> : <Pause size={16} />}
            </IconButton>
            <button
              className='stop-button'
              disabled={!s.connected || s.status === 'stopping'}
              onClick={() => session.stop()}
            >
              <Square size={12} fill='currentColor' />Stop
            </button>
          </>
        )}
    </div>
  );
}
function ZoomControls() {
  const { zoom } = useViewport(), flow = useReactFlow();
  return (
    <div className='zoom-controls floating-panel'>
      <IconButton label='Zoom out' onClick={() => flow.zoomOut({ duration: 180 })}>
        <Minus size={15} />
      </IconButton>
      <button
        className='zoom-value mono'
        title='Reset zoom'
        onClick={() => flow.zoomTo(1, { duration: 180 })}
      >
        {Math.round(zoom * 100)}%
      </button>
      <IconButton label='Zoom in' onClick={() => flow.zoomIn({ duration: 180 })}>
        <Plus size={15} />
      </IconButton>
      <span className='divider' />
      <IconButton
        label='Fit graph (F)'
        onClick={() => flow.fitView({ padding: 0.12, duration: 300 })}
      >
        <Maximize size={15} />
      </IconButton>
    </div>
  );
}

export function App() {
  const live = useStore(runtime);
  const state = useStore(editor), { document, locked } = state;
  const [nodes, setNodes, onNodesChange] = useNodesState<FlowNode>([]);
  const [edges, setEdges, onEdgesChange] = useEdgesState<Edge>([]);
  const [profile, setProfile] = useState(false),
    [caption, setCaption] = useState<Caption | null>(null);
  const [popup, setPopup] = useState<Popup | null>(null), [search, setSearch] = useState('');
  const [fileMenu, setFileMenu] = useState(false),
    [help, setHelp] = useState(false);
  const notice = useStore(notifications)[0];
  const fileInput = useRef<HTMLInputElement>(null), helpDialog = useRef<HTMLDialogElement>(null);
  const flow = useReactFlow<FlowNode>();
  const canvas = useRef<HTMLElement>(null);
  const connectionStart = useRef<{ x: number; y: number } | null>(null);
  const notify = useCallback((message: string) => publishNotification(message), []);

  useEffect(() => {
    if (canvas.current) return bindMiddlePan(canvas.current, flow);
  }, [flow]);

  useEffect(() => {
    setNodes((previous) =>
      document.nodes.map((model) => ({
        ...previous.find((n) => n.id === model.id),
        id: model.id,
        type: 'pipeline',
        position: model.position,
        dragHandle: '.node-header',
        data: { model, locked, profile },
      }))
    );
    setEdges((previous) =>
      document.edges.map((wire) => ({
        ...previous.find((e) => e.id === wire.id),
        ...wire,
        sourceHandle: wire.sourceHandle ?? 'out',
        targetHandle: wire.targetHandle ?? 'in',
        type: 'smoothstep',
        animated: locked,
        style: {
          stroke: payloadColors[
            ports(
              document.nodes.find((n) => n.id === wire.source)!,
              'source',
            )[wire.sourceHandle ?? 'out']
          ],
          strokeWidth: 1.5,
        },
      }))
    );
  }, [document, locked, profile, setNodes, setEdges]);
  useEffect(() => {
    if (!notice || notice.persistent) return;
    const timer = setTimeout(() => dismissNotification(notice.id), 6000);
    return () => clearTimeout(timer);
  }, [notice]);
  useEffect(() => {
    if (help) helpDialog.current?.showModal();
    else helpDialog.current?.close();
  }, [help]);
  useEffect(() => {
    let epoch = runtime.get().epoch;
    return runtime.subscribe(() => {
      if (runtime.get().epoch !== epoch) {
        epoch = runtime.get().epoch;
        setCaption(null);
      }
    });
  }, []);

  const showPopup = useCallback(
    (x: number, y: number, node?: string, origin?: ConnectionOrigin) => {
      setFileMenu(false);
      setSearch('');
      setPopup({
        x: Math.max(12, Math.min(x, window.innerWidth - 300)),
        y: Math.max(76, Math.min(y, window.innerHeight - 430)),
        flow: flow.screenToFlowPosition({ x, y }),
        node,
        origin,
      });
    },
    [flow],
  );

  const onConnectEnd: OnConnectEnd = (event, connection) => {
    const start = connectionStart.current;
    connectionStart.current = null;
    if (locked || !start || connection.isValid || !connection.fromNode || !connection.fromHandle) {
      return;
    }
    const point = 'changedTouches' in event ? event.changedTouches[0] : event;
    if (!point || Math.hypot(point.clientX - start.x, point.clientY - start.y) < 8) return;
    // Pointer capture can leave event.target on the socket after an empty-canvas drop.
    const target = window.document.elementFromPoint(point.clientX, point.clientY);
    if (!target?.classList.contains('react-flow__pane')) return;
    showPopup(point.clientX, point.clientY, undefined, {
      node: connection.fromNode.id,
      direction: connection.fromHandle.type,
      handle: connection.fromHandle.id ?? undefined,
    });
  };

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        connectionStart.current = null;
        setPopup(null);
        setFileMenu(false);
        setHelp(false);
        return;
      }
      if (
        (e.target as HTMLElement).closest('input,select,textarea,[contenteditable=true],dialog')
      ) return;
      if (e.key.toLowerCase() === 'f') {
        e.preventDefault();
        flow.fitView({ padding: 0.12, duration: 300 });
      }
      if (e.key.toLowerCase() === 'a' && !e.metaKey && !e.ctrlKey && !locked) {
        showPopup(window.innerWidth / 2 - 140, 160);
      }
      if (e.key === '?') setHelp(true);
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'z') {
        e.preventDefault();
        e.shiftKey ? commands.redo() : commands.undo();
      }
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'd') {
        e.preventDefault();
        const n = flow.getNodes().find((n) => n.selected);
        if (n) commands.duplicate(n.id);
      }
      if (e.key === 'Delete' || e.key === 'Backspace') {
        e.preventDefault();
        commands.remove(
          flow.getNodes().filter((n) => n.selected).map((n) => n.id),
          flow.getEdges().filter((e) => e.selected).map((e) => e.id),
        );
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [flow, locked, showPopup]);

  async function download(format: 'toml' | 'json' = 'toml') {
    try {
      const response = await fetch('/api/config/export', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ config: toBackend(document), format }),
      });
      if (!response.ok) throw new Error((await response.json()).error);
      const url = URL.createObjectURL(await response.blob());
      const a = window.document.createElement('a');
      a.href = url;
      a.download = `pipeline.${format}`;
      a.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      setFileMenu(false);
    } catch (error) {
      notify(error instanceof Error ? error.message : String(error));
    }
  }
  function context(e: MouseEvent | ReactMouseEvent, node?: string) {
    e.preventDefault();
    showPopup(e.clientX, e.clientY, node);
  }

  return (
    <main className='studio'>
      <section ref={canvas} className='canvas-area' aria-label='Pipeline workspace'>
        <div className='graph-surface'>
          <ReactFlow
            nodes={nodes}
            edges={edges}
            nodeTypes={nodeTypes}
            onNodesChange={onNodesChange}
            onEdgesChange={onEdgesChange}
            onNodeDragStop={(_, __, moved) =>
              commands.move(moved.map((n) => ({ id: n.id, position: n.position })))}
            onConnect={(connection) => {
              const error = commands.connect(
                connection.source,
                connection.target,
                connection.sourceHandle ?? undefined,
                connection.targetHandle ?? undefined,
              );
              if (error) notify(error);
            }}
            onConnectStart={(event) => {
              setPopup(null);
              const point = 'touches' in event ? event.touches[0] : event;
              connectionStart.current = point ? { x: point.clientX, y: point.clientY } : null;
            }}
            onConnectEnd={onConnectEnd}
            isValidConnection={(connection) =>
              !locked &&
              !connectionError(
                document,
                connection.source,
                connection.target,
                connection.sourceHandle ?? undefined,
                connection.targetHandle ?? undefined,
              )}
            onPaneContextMenu={context}
            onNodeContextMenu={(e, node) => context(e, node.id)}
            onPaneClick={() => {
              setPopup(null);
              setFileMenu(false);
            }}
            fitView
            fitViewOptions={{ padding: 0.14, maxZoom: 0.95 }}
            minZoom={0.25}
            maxZoom={1.7}
            panOnDrag={false}
            panOnScroll
            panOnScrollSpeed={1}
            zoomOnScroll={false}
            zoomOnPinch
            selectionOnDrag
            selectionMode={SelectionMode.Partial}
            panActivationKeyCode='Space'
            selectionKeyCode='Shift'
            deleteKeyCode={null}
            nodesConnectable={!locked}
            edgesReconnectable={false}
            colorMode='dark'
            connectionRadius={40}
            snapToGrid
            snapGrid={[10, 10]}
            onlyRenderVisibleElements={false}
          >
            <Background variant={BackgroundVariant.Dots} gap={22} size={1} color='#3b3f43' />
          </ReactFlow>
        </div>
        <div className='document-bar'>
          <button
            className='file-menu-button floating-panel'
            aria-label='File menu'
            aria-expanded={fileMenu}
            onClick={() => {
              setFileMenu(!fileMenu);
              setPopup(null);
            }}
          >
            <Menu size={20} />
          </button>
          <span className='document-name'>{document.name}</span>
          {locked && (
            <span className='edit-lock'>
              <LockKeyhole size={12} />Stop to edit
            </span>
          )}
        </div>
        <RunControls notify={notify} />
        <div className='canvas-bottom-left'>
          <ZoomControls />
          <div className='history-controls floating-panel'>
            <IconButton
              label='Undo (⌘Z)'
              disabled={locked || !state.past.length}
              onClick={commands.undo}
            >
              <Undo2 size={16} />
            </IconButton>
            <IconButton
              label='Redo (⌘⇧Z)'
              disabled={locked || !state.future.length}
              onClick={commands.redo}
            >
              <Redo2 size={16} />
            </IconButton>
          </div>
        </div>
        <div className='canvas-bottom-right'>
          <span>
            {document.nodes.length} nodes<span className='small-separator'>·</span>
            {document.edges.length} connections
          </span>
          <IconButton label='Keyboard shortcuts and demo information' onClick={() => setHelp(true)}>
            <HelpCircle size={17} />
          </IconButton>
        </div>
        {nodes.length === 0 && (
          <div className='empty-canvas'>
            <h1>A little space to build.</h1>
            <p>Add a media source to start your pipeline.</p>
            <button onClick={() => showPopup(window.innerWidth / 2 - 140, 150)}>
              <Plus size={16} />Add a node
            </button>
          </div>
        )}
        {fileMenu && (
          <div className='file-menu floating-panel'>
            <div className='menu-heading'>
              <Menu size={15} />Pipeline document
            </div>
            <button
              disabled={locked || !document.backend}
              onClick={() => {
                commands.replace({
                  version: 1,
                  name: 'Untitled pipeline',
                  nodes: [],
                  edges: [],
                  backend: { nodes: [], settings: document.backend?.settings ?? {}, editor: {} },
                });
                setFileMenu(false);
                setTimeout(() => flow.fitView({ padding: 0.14 }), 80);
              }}
            >
              <FilePlus2 size={15} />New pipeline
            </button>
            <button disabled={locked} onClick={() => fileInput.current?.click()}>
              <Upload size={15} />Open pipeline
            </button>
            <button onClick={() => void download('toml')}>
              <Download size={15} />Save pipeline TOML
            </button>
            <button onClick={() => void download('json')}>
              <Download size={15} />Save pipeline JSON
            </button>
            <button
              disabled={locked || !live.connected}
              onClick={async () => {
                try {
                  const result = await api<{ warnings: string[] }>('validate', {
                    config: toBackend(document),
                  });
                  notify(result.warnings.join(' ') || 'Pipeline is valid.');
                } catch (err) {
                  notify(String(err));
                }
              }}
            >
              <Check size={15} />Validate pipeline
            </button>
            <p>
              Executable config, including node layout. Uploaded media lasts until the server exits.
            </p>
          </div>
        )}
        <input
          ref={fileInput}
          type='file'
          accept='.json,.toml'
          hidden
          onChange={async (e) => {
            const file = e.target.files?.[0];
            e.target.value = '';
            if (!file) return;
            try {
              if (file.size > 2_000_000) throw new Error('Document is too large.');
              const imported = await api<{ config: BackendConfig }>('config/import', {
                text: await file.text(),
                format: file.name.endsWith('.toml') ? 'toml' : 'json',
              });
              const doc = fromBackend(imported.config);
              doc.name = file.name.replace(/\.(toml|json)$/, '');
              if (commands.replace(doc)) {
                setFileMenu(false);
                notify('Pipeline opened.');
                setTimeout(() => flow.fitView({ padding: 0.14 }), 80);
              }
            } catch (error) {
              notify(error instanceof Error ? error.message : 'Could not open document.');
            }
          }}
        />
        {popup && (
          <div
            className='node-library floating-panel'
            style={{ left: popup.x, top: popup.y }}
            role='dialog'
            aria-label='Add node or edit selection'
          >
            <div className='library-search'>
              <Search size={16} />
              <input
                autoFocus
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                placeholder='Find a node…'
                aria-label='Find a node'
              />
              <IconButton label='Close node menu' onClick={() => setPopup(null)}>
                <X size={14} />
              </IconButton>
            </div>
            {popup.node && (
              <div className='context-actions'>
                <button
                  disabled={locked}
                  onClick={() => {
                    commands.duplicate(popup.node!);
                    setPopup(null);
                  }}
                >
                  <Copy size={14} />Duplicate node<kbd>⌘D</kbd>
                </button>
                <button
                  disabled={locked}
                  onClick={() => {
                    commands.remove([popup.node!]);
                    setPopup(null);
                  }}
                >
                  <Trash2 size={14} />Delete node<kbd>⌫</kbd>
                </button>
              </div>
            )}
            <div className='section-label'>
              {popup.origin ? 'ADD & CONNECT' : 'ADD TO PIPELINE'}
            </div>
            {popup.origin && (
              <p className='connection-menu-hint'>
                Compatible {popup.origin.direction === 'source' ? 'inputs' : 'outputs'} for{' '}
                {document.nodes.find((n) => n.id === popup.origin!.node)?.name}
              </p>
            )}
            {Object.values(catalog).filter((m) =>
              `${m.title} ${m.impl}`.toLowerCase().includes(search.toLowerCase()) &&
              (!popup.origin || compatibleKinds(document, popup.origin).includes(m.kind))
            ).map((m) => {
              const Icon = kindIcons[(m.role ?? m.kind) as keyof typeof kindIcons] ?? Search;
              return (
                <button
                  key={m.kind}
                  className='library-item'
                  title={m.error ?? m.subtitle}
                  disabled={locked || !!m.error}
                  onClick={() => {
                    if (popup.origin) {
                      const error = addConnected(m.kind, popup.flow, popup.origin);
                      if (error) {
                        notify(error);
                        return;
                      }
                    } else commands.add(m.kind as Kind, popup.flow);
                    setPopup(null);
                  }}
                >
                  <span className='library-icon' style={{ color: m.color }}>
                    <Icon size={19} />
                  </span>
                  <span>
                    <b>{m.title}</b>
                    <small>
                      {m.impl} · {m.input ?? 'Media'}
                      <span>→</span>
                      {m.output ?? 'Output'}
                    </small>
                  </span>
                  <Plus size={14} />
                </button>
              );
            })}
            <div className='library-footer'>
              {locked
                ? 'Stop the run to change the graph.'
                : popup.origin
                ? 'Choose a node to create and connect it. Esc cancels.'
                : 'Drag a socket into empty space to add and connect.'}
            </div>
          </div>
        )}
        {profile && (
          <Profiler
            caption={caption}
            onClose={() => setProfile(false)}
            clearCaption={() => setCaption(null)}
          />
        )}
        {notice && (
          <div className={`notice ${notice.level}`} role='status'>
            <span>{notice.message}</span>
            <IconButton
              label='Dismiss notification'
              onClick={() => dismissNotification(notice.id)}
            >
              <X size={14} />
            </IconButton>
          </div>
        )}
      </section>
      <SubtitleDock
        profile={profile}
        onToggleProfile={() => setProfile(!profile)}
        selected={caption?.id ?? null}
        onSelect={(row) => {
          setCaption(row);
          setProfile(true);
        }}
      />
      <dialog
        className='help-dialog'
        ref={helpDialog}
        onClose={() => setHelp(false)}
        onClick={(e) => {
          if (e.target === e.currentTarget) setHelp(false);
        }}
      >
        <header>
          <h2>A canvas for live subtitles.</h2>
          <IconButton label='Close help' onClick={() => setHelp(false)}>
            <X size={18} />
          </IconButton>
        </header>
        <p>
          Configure on the nodes, connect matching sockets, then run the pipeline on the local
          backend. File sources start paused; use Play source to begin. Mock modules are explicitly
          named.
        </p>
        <div className='shortcut-grid'>
          {[
            ['Select / move nodes', 'Left mouse'],
            ['Box select', 'Left drag on canvas'],
            ['Pan', 'Two-finger scroll / middle drag / Space + drag'],
            ['Zoom', 'Trackpad pinch / ⌘ or Ctrl + scroll'],
            ['Add connected node', 'Drag socket → empty canvas'],
            ['Add a node', 'A / right click'],
            ['Fit graph', 'F'],
            ['Undo / redo', '⌘Z / ⌘⇧Z'],
            ['Duplicate selection', '⌘D'],
            ['Delete selection', '⌫'],
            ['Close menus', 'Esc'],
          ].map(([action, key]) => (
            <div key={action}>
              <span>{action}</span>
              <kbd>{key}</kbd>
            </div>
          ))}
        </div>
        <p className='help-note'>
          <Check size={15} />During playback, configuration is locked. Move nodes, inspect
          subtitles, pause and seek freely.
        </p>
        <button className='run-button' onClick={() => setHelp(false)}>
          Back to the canvas<ArrowUpRight size={15} />
        </button>
      </dialog>
    </main>
  );
}
