import { createStore } from './store.ts';

export interface Notification {
  id: number;
  key?: string;
  message: string;
  level: 'info' | 'error';
  persistent: boolean;
}

export const notifications = createStore<Notification[]>([]);
let nextId = 1;

export function publishNotification(
  message: string,
  options: { key?: string; level?: Notification['level']; persistent?: boolean } = {},
) {
  const normalized = message.trim();
  if (!normalized) return;
  const current = notifications.get();
  const level = options.level ?? 'info';
  const existing = options.key && current.find((item) => item.key === options.key);
  if (existing) {
    notifications.set(
      current.map((item) =>
        item.id === existing.id
          ? { ...item, message: normalized, level, persistent: options.persistent ?? false }
          : item
      ),
    );
    return;
  }
  notifications.set([...current, {
    id: nextId++,
    key: options.key,
    message: normalized,
    level,
    persistent: options.persistent ?? false,
  }].slice(-4));
}

export function dismissNotification(id: number) {
  notifications.set(notifications.get().filter((item) => item.id !== id));
}

export function resolveNotification(key: string) {
  notifications.set(notifications.get().filter((item) => item.key !== key));
}
