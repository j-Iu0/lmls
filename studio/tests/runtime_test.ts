import { example } from '../src/domain/model.ts';
import { commands, editor } from '../src/state/editor.ts';
import { advanceSimulation, demo, runtime } from '../src/runtime/demo.ts';

function assert(condition: unknown, message = 'Assertion failed'): asserts condition {
  if (!condition) throw new Error(message);
}

Deno.test('simulation pause, source transport, seek generations and stop have consistent semantics', () => {
  demo.stop();
  commands.replace(example());
  try {
    assert(demo.start(editor.get().document).length === 0);
    assert(editor.get().locked);
    assert(demo.start(editor.get().document).length > 0, 'An active run must not be replaced');
    for (let i = 0; i < 16; i++) advanceSimulation();
    const running = runtime.get();
    assert(running.captions.length === 2);
    assert(running.captions.every((row) => row.final && row.revision === 3));
    demo.toggle();
    advanceSimulation();
    assert(runtime.get().time === running.time, 'Pipeline pause freezes time');
    assert(editor.get().locked, 'Pause must not unlock the config');
    demo.seek('source', 50);
    assert(runtime.get().epoch === running.epoch + 1);
    assert(runtime.get().captions.length === 0, 'Seek clears previous-generation subtitles');
    assert(runtime.get().media.source.position === 50);
    demo.toggle();
    demo.pauseSource('source');
    advanceSimulation();
    assert(runtime.get().media.source.position === 50, 'Source pause freezes its clock');
    demo.pauseSource('source');
    advanceSimulation();
    assert(runtime.get().media.source.position > 50);
    const retained = runtime.get().captions;
    demo.stop();
    assert(!editor.get().locked);
    assert(runtime.get().captions === retained, 'Stop preserves the last trace');
  } finally {
    demo.stop();
  }
});

Deno.test('simulated runtime bounds retained telemetry and subtitle history', () => {
  demo.stop();
  try {
    demo.start(example());
    for (let i = 0; i < 2000; i++) advanceSimulation();
    assert(runtime.get().captions.length <= 160);
    assert(Object.values(runtime.get().metrics).every((metric) => metric.samples.length <= 80));
    assert(runtime.get().media.source.position <= runtime.get().media.source.duration);
  } finally {
    demo.stop();
  }
});
