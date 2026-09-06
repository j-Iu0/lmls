export function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key === 'class') node.className = value;
    else if (key === 'text') node.textContent = value;
    else if (key.startsWith('on')) node.addEventListener(key.slice(2), value);
    else if (key === 'dataset') Object.assign(node.dataset, value);
    else if (value !== false && value != null) node.setAttribute(key, value === true ? '' : value);
  }
  for (const child of children.flat()) if (child != null) node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  return node;
}
export const $ = selector => document.querySelector(selector);
export const ms = value => Number.isFinite(value) ? `${value.toFixed(1)} ms` : '—';
export const number = value => Number.isFinite(value) ? value.toLocaleString() : '—';
export function download(text, name, type = 'text/plain') {
  const url = URL.createObjectURL(new Blob([text], {type}));
  const a = el('a', {href: url, download: name}); a.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
}
export function jsonObject(text, label) {
  let value; try { value = JSON.parse(text); } catch (e) { throw new Error(`${label}: ${e.message}`); }
  if (!value || Array.isArray(value) || typeof value !== 'object') throw new Error(`${label} must be a JSON object.`);
  return value;
}
