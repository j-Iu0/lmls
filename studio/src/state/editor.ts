import { connectionError, example, makeNode } from '../domain/model.ts';
import type { Document, Kind, PipelineNode, Value } from '../domain/model.ts';
import { createStore } from './store.ts';

export const editor = createStore({
  document: { version: 1, name: 'Pipeline', nodes: [], edges: [] } as Document,
  past: [] as Document[],
  future: [] as Document[],
  locked: false,
});
function commit(document: Document) {
  const state = editor.get();
  if (state.locked) return false;
  editor.set({ ...state, document, past: [...state.past.slice(-49), state.document], future: [] });
  return true;
}
export const commands = {
  adopt(document: Document, locked: boolean) {
    editor.set({ document, locked, past: [], future: [] });
  },
  node(
    id: string,
    changes: Partial<Pick<PipelineNode, 'enabled' | 'mode' | 'overlay' | 'autoPause'>>,
  ) {
    const doc = editor.get().document;
    return commit({ ...doc, nodes: doc.nodes.map((n) => n.id === id ? { ...n, ...changes } : n) });
  },
  lock(locked: boolean) {
    editor.set({ ...editor.get(), locked });
  },
  replace(document: Document) {
    return commit(document);
  },
  option(id: string, key: string, value: Value) {
    const doc = editor.get().document;
    return commit({
      ...doc,
      nodes: doc.nodes.map((n) =>
        n.id === id ? { ...n, options: { ...n.options, [key]: value } } : n
      ),
    });
  },
  add(kind: Kind, position: PipelineNode['position']) {
    const doc = editor.get().document;
    return commit({ ...doc, nodes: [...doc.nodes, makeNode(kind, crypto.randomUUID(), position)] });
  },
  remove(ids: string[], edgeIds: string[] = []) {
    const doc = editor.get().document;
    return commit({
      ...doc,
      nodes: doc.nodes.filter((n) => !ids.includes(n.id)),
      edges: doc.edges.filter((e) =>
        !ids.includes(e.source) && !ids.includes(e.target) && !edgeIds.includes(e.id)
      ),
    });
  },
  duplicate(id: string) {
    const doc = editor.get().document, node = doc.nodes.find((n) => n.id === id);
    if (!node) return false;
    return commit({
      ...doc,
      nodes: [...doc.nodes, {
        ...structuredClone(node),
        id: crypto.randomUUID(),
        name: `${node.name}_${crypto.randomUUID().slice(0, 6)}`,
        raw: node.raw ? { ...node.raw, inputs: {}, outputs: {} } : undefined,
        unwiredInputs: {},
        position: { x: node.position.x + 45, y: node.position.y + 55 },
      }],
    });
  },
  connect(
    source: string,
    target: string,
    sourceHandle?: string,
    targetHandle?: string,
  ): string | null {
    const state = editor.get();
    if (state.locked) return 'Stop the run to change connections.';
    const error = connectionError(state.document, source, target, sourceHandle, targetHandle);
    if (error) return error;
    commit({
      ...state.document,
      edges: [...state.document.edges, {
        id: crypto.randomUUID(),
        source,
        target,
        sourceHandle,
        targetHandle,
      }],
    });
    return null;
  },
  move(positions: { id: string; position: PipelineNode['position'] }[]) {
    const state = editor.get();
    const document = {
      ...state.document,
      nodes: state.document.nodes.map((n) => {
        const p = positions.find((p) => p.id === n.id);
        return p ? { ...n, position: p.position } : n;
      }),
    };
    if (state.locked) editor.set({ ...state, document });
    else commit(document);
  },
  undo() {
    const s = editor.get();
    if (s.locked || !s.past.length) return;
    editor.set({
      ...s,
      document: s.past.at(-1)!,
      past: s.past.slice(0, -1),
      future: [s.document, ...s.future],
    });
  },
  redo() {
    const s = editor.get();
    if (s.locked || !s.future.length) return;
    editor.set({
      ...s,
      document: s.future[0],
      past: [...s.past, s.document],
      future: s.future.slice(1),
    });
  },
};
