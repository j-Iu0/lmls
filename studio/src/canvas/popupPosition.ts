const margin = 12;
const width = 284;
const topClearance = 76;
const preferredHeight = 430;

export type PopupBounds = {
  left: number;
  top: number;
  width: number;
  height: number;
};

export function placePopup(clientX: number, clientY: number, bounds: PopupBounds) {
  const availableWidth = Math.max(0, bounds.width - margin * 2);
  const renderedWidth = Math.min(width, availableWidth);
  const minTop = Math.min(topClearance, Math.max(margin, bounds.height - margin));
  const maxLeft = Math.max(margin, bounds.width - renderedWidth - margin);
  const maxTop = Math.max(minTop, bounds.height - preferredHeight - margin);
  const x = Math.max(margin, Math.min(clientX - bounds.left, maxLeft));
  const y = Math.max(minTop, Math.min(clientY - bounds.top, maxTop));

  return {
    x,
    y,
    maxHeight: Math.max(0, bounds.height - y - margin),
  };
}
