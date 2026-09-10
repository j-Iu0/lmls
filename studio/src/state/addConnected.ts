import { catalog, connectionError, makeNode, ports } from '../domain/model.ts';
import type { Document, Kind, PipelineNode } from '../domain/model.ts';
import { commands, editor } from './editor.ts';

export interface ConnectionOrigin {
  node: string;
  direction: 'source' | 'target';
  handle?: string;
}

export function compatibleKinds(document: Document, origin: ConnectionOrigin): Kind[] {
  const node = document.nodes.find((n) => n.id === origin.node);
  if (!node) return [];
  const own = ports(node, origin.direction);
  const payload = own[origin.handle ?? Object.keys(own)[0]];
  if (!payload) return [];
  return Object.values(catalog).filter((candidate) =>
    !candidate.error &&
    Object.values(
      ports(
        makeNode(candidate.kind, '', { x: 0, y: 0 }),
        origin.direction === 'source' ? 'target' : 'source',
      ),
    ).includes(payload)
  ).map((candidate) => candidate.kind);
}

/** Creation and wiring are one edit: cancel changes nothing, undo removes both. */
export function addConnected(
  kind: Kind,
  position: PipelineNode['position'],
  origin: ConnectionOrigin,
): string | null {
  const state = editor.get();
  if (state.locked) return 'Stop the run to change connections.';
  if (!compatibleKinds(state.document, origin).includes(kind)) {
    return 'Choose a node with a matching payload type.';
  }
  const node = makeNode(kind, crypto.randomUUID(), position);
  const next = { ...state.document, nodes: [...state.document.nodes, node] };
  const source = origin.direction === 'source' ? origin.node : node.id;
  const target = origin.direction === 'source' ? node.id : origin.node;
  const own = ports(state.document.nodes.find((n) => n.id === origin.node)!, origin.direction);
  const originHandle = origin.handle ?? Object.keys(own)[0];
  const other = ports(node, origin.direction === 'source' ? 'target' : 'source');
  const otherHandle = Object.keys(other).find((key) => other[key] === own[originHandle])!;
  const sourceHandle = origin.direction === 'source' ? originHandle : otherHandle;
  const targetHandle = origin.direction === 'source' ? otherHandle : originHandle;
  const error = connectionError(next, source, target, sourceHandle, targetHandle);
  if (error) return error;
  commands.replace({
    ...next,
    edges: [...next.edges, { id: crypto.randomUUID(), source, target, sourceHandle, targetHandle }],
  });
  return null;
}
