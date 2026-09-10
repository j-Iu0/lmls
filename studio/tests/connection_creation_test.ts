import { example } from '../src/domain/model.ts';
import { commands, editor } from '../src/state/editor.ts';
import { addConnected, compatibleKinds } from '../src/state/addConnected.ts';

function assert(value: unknown, message = 'Assertion failed'): asserts value {
  if (!value) throw new Error(message);
}

Deno.test('connection menu filters by both payload and direction', () => {
  const doc = example();
  assert(compatibleKinds(doc, { node: 'source', direction: 'source' }).join() === 'segmenter');
  assert(compatibleKinds(doc, { node: 'asr', direction: 'target' }).join() === 'segmenter');
  assert(compatibleKinds(doc, { node: 'asr', direction: 'source' }).join() === 'translator,output');
  assert(compatibleKinds(doc, { node: 'missing', direction: 'source' }).length === 0);
});

Deno.test('creating a connected node is atomic, preserves the drop position and undoes in one step', () => {
  commands.lock(false);
  commands.replace(example());
  const before = editor.get().document;
  assert(
    addConnected('transcriber', { x: 813, y: 225 }, { node: 'vad', direction: 'source' }) === null,
  );
  const after = editor.get().document;
  const added = after.nodes.at(-1)!;
  assert(added.position.x === 813 && added.position.y === 225);
  assert(after.edges.at(-1)?.source === 'vad' && after.edges.at(-1)?.target === added.id);
  commands.undo();
  assert(editor.get().document === before);
  commands.redo();
  assert(editor.get().document === after);
});

Deno.test('input-origin creation wires backwards; incompatible or locked edits change nothing', () => {
  commands.lock(false);
  commands.replace(example());
  assert(
    addConnected('segmenter', { x: 20, y: 30 }, { node: 'asr', direction: 'target' }) === null,
  );
  assert(editor.get().document.edges.at(-1)?.target === 'asr');
  const before = editor.get().document;
  assert(addConnected('source', { x: 0, y: 0 }, { node: 'asr', direction: 'target' }) !== null);
  assert(editor.get().document === before);
  commands.lock(true);
  assert(addConnected('segmenter', { x: 0, y: 0 }, { node: 'asr', direction: 'target' }) !== null);
  assert(editor.get().document === before);
  commands.lock(false);
});
