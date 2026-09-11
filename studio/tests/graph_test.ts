import {
  connectionError,
  example,
  makeNode,
  parseDocument,
  validate,
} from '../src/domain/model.ts';
import { commands, editor } from '../src/state/editor.ts';

function assert(condition: unknown, message = 'Assertion failed'): asserts condition {
  if (!condition) throw new Error(message);
}
function rejects(action: () => void) {
  let rejected = false;
  try {
    action();
  } catch {
    rejected = true;
  }
  assert(rejected, 'Expected invalid document to be rejected');
}

Deno.test('typed connections permit text fan-in but reject wrong types, duplicates and cycles', () => {
  const doc = example();
  assert(validate(doc).length === 0);
  doc.nodes.push(makeNode('output', 'output', { x: 0, y: 0 }));
  assert(connectionError(doc, 'source', 'asr') !== null);
  assert(connectionError(doc, 'source', 'vad') !== null);
  assert(connectionError(doc, 'asr', 'asr') !== null);
  const extra = makeNode('translator', 'second', { x: 0, y: 0 });
  doc.nodes.push(extra);
  assert(connectionError(doc, 'translate', 'second') === null);
  doc.edges.push({ id: 'translation-chain', source: 'translate', target: 'second' });
  assert(connectionError(doc, 'second', 'translate') !== null);
  assert(
    connectionError(doc, 'second', 'output') === null,
    'Output must allow multiple text publishers',
  );
});

Deno.test('document roundtrip preserves field values and positions; malformed documents fail closed', () => {
  const doc = example();
  doc.nodes[1].options.threshold = 0.7;
  assert(JSON.stringify(parseDocument(JSON.stringify(doc))) === JSON.stringify(doc));
  rejects(() => parseDocument('{"node":[]}'));
  const duplicate = structuredClone(doc);
  duplicate.nodes.push(duplicate.nodes[0]);
  rejects(() => parseDocument(JSON.stringify(duplicate)));
  const invalid = structuredClone(doc);
  invalid.nodes[1].options.threshold = 'not a number';
  rejects(() => parseDocument(JSON.stringify(invalid)));
  const cycle = structuredClone(doc);
  cycle.edges.push({ id: 'bad', source: 'output', target: 'source' });
  rejects(() => parseDocument(JSON.stringify(cycle)));
});

Deno.test('deleting a publisher releases the overlay and auto pause attached to it', () => {
  commands.lock(false);
  commands.replace(example());
  commands.overlay('source', { node: 'Translation', port: 'out' });
  commands.node('source', { autoPause: true });
  assert(editor.get().document.nodes.find((n) => n.id === 'source')!.overlay === 'Translation.out');
  assert(commands.remove(['translate']) === true);
  const source = editor.get().document.nodes.find((n) => n.id === 'source')!;
  assert(source.overlay === '' && source.autoPause === false);
  // Undo restores the node together with the overlay it powered.
  commands.undo();
  const restored = editor.get().document.nodes.find((n) => n.id === 'source')!;
  assert(restored.overlay === 'Translation.out' && restored.autoPause === true);
  // Edge-only removals keep topics publishing, so overlays survive them.
  commands.redo();
  const orphan = editor.get().document.nodes.find((n) => n.id === 'source')!;
  assert(orphan.overlay === '' && orphan.autoPause === false);
  commands.overlay('source', { node: 'Transcription', port: 'out' });
  commands.remove([], ['vad-asr']);
  assert(
    editor.get().document.nodes.find((n) => n.id === 'source')!.overlay ===
      'Transcription.out',
  );
});

Deno.test('runtime lock rejects all graph mutations, including undo, but permits layout changes', () => {
  commands.lock(false);
  commands.replace(example());
  commands.option('asr', 'model', 'large-v3');
  commands.lock(true);
  const before = editor.get().document;
  assert(commands.add('source', { x: 2, y: 3 }) === false);
  assert(commands.remove(['asr']) === false);
  assert(commands.duplicate('asr') === false);
  assert(commands.option('asr', 'model', 'tiny.en') === false);
  assert(commands.replace(example()) === false);
  assert(commands.connect('source', 'output') !== null);
  commands.undo();
  commands.redo();
  assert(editor.get().document === before);
  commands.move([{ id: 'asr', position: { x: 25, y: 45 } }]);
  assert(editor.get().document.nodes.find((n) => n.id === 'asr')!.position.x === 25);
  commands.lock(false);
  commands.option('asr', 'model', 'tiny.en');
  commands.undo();
  assert(editor.get().document.nodes.find((n) => n.id === 'asr')!.options.model === 'large-v3');
  commands.redo();
  assert(editor.get().document.nodes.find((n) => n.id === 'asr')!.options.model === 'tiny.en');
});
