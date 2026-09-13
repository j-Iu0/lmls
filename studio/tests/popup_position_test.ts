import { placePopup } from '../src/canvas/popupPosition.ts';

function assertEquals(actual: unknown, expected: unknown) {
  if (actual !== expected) throw new Error(`Expected ${expected}, got ${actual}`);
}

Deno.test('node menu stays inside the canvas near its bottom-right edge', () => {
  const placement = placePopup(1195, 795, { left: 0, top: 0, width: 1200, height: 800 });
  assertEquals(placement.x, 904);
  assertEquals(placement.y, 358);
  assertEquals(placement.maxHeight, 430);
});

Deno.test('node menu uses canvas-relative coordinates and remaining height', () => {
  const placement = placePopup(760, 490, { left: 40, top: 50, width: 720, height: 500 });
  assertEquals(placement.x, 424);
  assertEquals(placement.y, 76);
  assertEquals(placement.maxHeight, 412);
});

Deno.test('node menu remains reachable in a narrow, short canvas', () => {
  const placement = placePopup(500, 500, { left: 0, top: 0, width: 240, height: 180 });
  assertEquals(placement.x, 12);
  assertEquals(placement.y, 76);
  assertEquals(placement.maxHeight, 92);
});
