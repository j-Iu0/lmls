type Viewport = { x: number; y: number; zoom: number };
type ViewportControl = {
  getViewport(): Viewport;
  setViewport(viewport: Viewport): unknown;
};

/** Capture middle drags before node controls and selection can consume them. */
export function bindMiddlePan(surface: HTMLElement, flow: ViewportControl) {
  let drag: { pointer: number; x: number; y: number; viewport: Viewport } | null = null;
  const controller = new AbortController();
  const options = { capture: true, signal: controller.signal };
  function stop() {
    const pointer = drag?.pointer;
    drag = null;
    surface.removeAttribute('data-panning');
    if (pointer !== undefined && surface.hasPointerCapture(pointer)) {
      surface.releasePointerCapture(pointer);
    }
  }
  surface.addEventListener('pointerdown', (event) => {
    if (event.button !== 1 || event.buttons !== 4 || event.pointerType !== 'mouse') return;
    event.preventDefault(); // Suppress browser autoscroll and compatibility mousedown.
    event.stopPropagation();
    drag = {
      pointer: event.pointerId,
      x: event.clientX,
      y: event.clientY,
      viewport: flow.getViewport(),
    };
    surface.setPointerCapture(event.pointerId);
    surface.setAttribute('data-panning', 'true');
  }, options);
  surface.addEventListener('pointermove', (event) => {
    if (!drag || event.pointerId !== drag.pointer) return;
    if (!(event.buttons & 4)) {
      stop();
      return;
    }
    event.preventDefault();
    event.stopPropagation();
    // Viewport translation is in screen pixels, regardless of graph zoom.
    void flow.setViewport({
      ...drag.viewport,
      x: drag.viewport.x + event.clientX - drag.x,
      y: drag.viewport.y + event.clientY - drag.y,
    });
  }, options);
  for (const type of ['pointerup', 'pointercancel', 'lostpointercapture'] as const) {
    surface.addEventListener(type, (event) => {
      if (event.pointerId === drag?.pointer) stop();
    }, options);
  }
  surface.addEventListener('auxclick', (event) => {
    if (event.button === 1) event.preventDefault();
  }, options);
  surface.ownerDocument.defaultView?.addEventListener('blur', stop, options);
  return () => {
    stop();
    controller.abort();
  };
}
