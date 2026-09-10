/// <reference lib="dom" />
import { bindMiddlePan } from '../src/canvas/middlePan.ts';

function assert(value: unknown, message = 'Assertion failed'): asserts value {
  if (!value) throw new Error(message);
}

function fixture(zoom = 0.5) {
  // Exercise the actual event binding without requiring a browser or another dependency.
  class Surface extends EventTarget {
    ownerDocument = { defaultView: new EventTarget() };
    captured = new Set<number>();
    attributes = new Map<string, string>();
    setPointerCapture(id: number) {
      this.captured.add(id);
    }
    hasPointerCapture(id: number) {
      return this.captured.has(id);
    }
    releasePointerCapture(id: number) {
      this.captured.delete(id);
    }
    setAttribute(name: string, value: string) {
      this.attributes.set(name, value);
    }
    removeAttribute(name: string) {
      this.attributes.delete(name);
    }
  }
  const surface = new Surface();
  let viewport = { x: 100, y: -20, zoom };
  const dispose = bindMiddlePan(surface as unknown as HTMLElement, {
    getViewport: () => viewport,
    setViewport: (next) => {
      viewport = next;
    },
  });
  function send(type: string, overrides: Record<string, number | string> = {}) {
    const event = Object.assign(new Event(type, { cancelable: true }), {
      button: 1,
      buttons: 4,
      pointerType: 'mouse',
      pointerId: 1,
      clientX: 200,
      clientY: 100,
      ...overrides,
    });
    surface.dispatchEvent(event);
    return event;
  }
  return { surface, send, dispose, viewport: () => viewport };
}

Deno.test('middle drag pans in screen pixels at every zoom and suppresses browser defaults', () => {
  for (const zoom of [0.25, 1, 1.7]) {
    const f = fixture(zoom);
    assert(f.send('pointerdown').defaultPrevented);
    assert(f.surface.hasPointerCapture(1));
    f.send('pointermove', { clientX: 250, clientY: 70 });
    assert(f.viewport().x === 150 && f.viewport().y === -50 && f.viewport().zoom === zoom);
    f.send('pointermove', { clientX: 270, clientY: 80 });
    assert(f.viewport().x === 170 && f.viewport().y === -40);
    f.send('pointerup', { buttons: 0 });
    assert(!f.surface.hasPointerCapture(1));
    assert(!f.surface.attributes.has('data-panning'));
    f.send('pointermove', { clientX: 900 });
    assert(f.viewport().x === 170);
    assert(f.send('auxclick').defaultPrevented);
    f.dispose();
  }
});

Deno.test('middle pan leaves left/right drags and other pointers alone', () => {
  const f = fixture();
  for (const button of [0, 2]) {
    assert(!f.send('pointerdown', { button, buttons: button === 0 ? 1 : 2 }).defaultPrevented);
    f.send('pointermove', { clientX: 800 });
    assert(f.viewport().x === 100);
  }
  assert(!f.send('pointerdown', { buttons: 5 }).defaultPrevented);
  f.send('pointerdown');
  f.send('pointermove', { pointerId: 2, clientX: 800 });
  f.send('pointerup', { pointerId: 2 });
  assert(f.viewport().x === 100 && f.surface.hasPointerCapture(1));
  f.dispose();
});

Deno.test('cancel, lost capture, missing button, blur and disposal end panning', () => {
  for (const end of ['pointercancel', 'lostpointercapture', 'missing-button', 'blur', 'dispose']) {
    const f = fixture();
    f.send('pointerdown');
    if (end === 'blur') f.surface.ownerDocument.defaultView.dispatchEvent(new Event('blur'));
    else if (end === 'dispose') f.dispose();
    else if (end === 'missing-button') f.send('pointermove', { buttons: 0 });
    else f.send(end);
    f.send('pointermove', { clientX: 900 });
    assert(f.viewport().x === 100 && !f.surface.hasPointerCapture(1), end);
    assert(!f.surface.attributes.has('data-panning'), end);
    f.dispose();
  }
});
