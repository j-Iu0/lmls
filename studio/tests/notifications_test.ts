import {
  dismissNotification,
  notifications,
  publishNotification,
  resolveNotification,
} from '../src/state/notifications.ts';

function assert(value: unknown, message = 'Assertion failed'): asserts value {
  if (!value) throw new Error(message);
}

Deno.test('notifications are dismissible events independent of runtime state', () => {
  for (const item of notifications.get()) dismissNotification(item.id);
  publishNotification('run failed', { level: 'error', persistent: true });
  const [notice] = notifications.get();
  assert(notice.message === 'run failed');
  assert(notice.persistent);
  dismissNotification(notice.id);
  assert(notifications.get().length === 0);
});

Deno.test('keyed state transitions update in place and resolve on recovery', () => {
  publishNotification('backend disconnected', {
    key: 'transport',
    level: 'error',
    persistent: true,
  });
  publishNotification('still reconnecting', {
    key: 'transport',
    level: 'error',
    persistent: true,
  });
  assert(notifications.get().length === 1);
  assert(notifications.get()[0].message === 'still reconnecting');
  resolveNotification('transport');
  assert(notifications.get().length === 0);
});
