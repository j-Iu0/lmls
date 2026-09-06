import test from 'node:test';
import assert from 'node:assert/strict';
import {PlaybackController} from '../../livesub/web/static/playback.mjs';
import {isActive} from '../../livesub/web/static/model.mjs';

class FakeVideo extends EventTarget {
  paused = true; seeking = false; readyState = 4; duration = 20; position = 0; plays = 0;
  get currentTime() { return this.position; }
  set currentTime(value) { this.position = value; this.seeking = true; this.dispatchEvent(new Event('seeking')); queueMicrotask(() => { this.seeking = false; this.dispatchEvent(new Event('seeked')); }); }
  play() { this.plays++; this.paused = false; this.dispatchEvent(new Event('play')); return Promise.resolve(); }
  pause() { if (!this.paused) { this.paused = true; this.dispatchEvent(new Event('pause')); } }
}
function fixture() {
  const state = {value: 'idle', epoch: 1, preparing: false}, sent = [], seeks = [], errors = [];
  const hooks = {active: () => isActive(state.value), canSeek: () => ['running', 'completed'].includes(state.value), preparing: () => state.preparing,
    clockAllowed: () => state.value === 'running' && !state.preparing, epoch: () => state.epoch, send: value => sent.push(value), clearCaptions() {},
    seek: async (name, position) => { seeks.push({name, position, epoch: state.epoch}); state.value = 'seeking'; state.epoch++; controller.updatePlayback({state: state.value, media: {}}); },
  };
  const controller = new PlaybackController(hooks); controller.error = (_, message) => errors.push(message);
  const video = new FakeVideo(), record = {name: 'video', video, source: '/file.wav'};
  controller.records.set('video', record); controller.attachPlayback(record);
  const update = (value, gate = false) => { state.value = value; controller.updatePlayback({state: value, media: {video: {auto_paused: gate}}}); };
  return {controller, video, record, state, sent, seeks, errors, hooks, update};
}
const tick = () => new Promise(resolve => setTimeout(resolve, 210));

test('page startup never autoplays, including reconnect to a running session', () => {
  const f = fixture(); f.update('running'); assert.equal(f.video.plays, 0); assert.equal(f.video.paused, true);
});
test('Run resets preview to zero, primes play in the gesture, and holds through startup', async () => {
  const f = fixture(); f.video.position = 8; f.state.preparing = true;
  f.controller.startFromGesture(); await Promise.resolve();
  assert.equal(f.video.currentTime, 0); assert.equal(f.video.paused, true); assert.equal(f.record.intent, true); assert.equal(f.video.plays, 1);
  f.state.preparing = false; f.update('starting', false); assert.equal(f.video.paused, true); f.controller.clocks(); assert.equal(f.sent.length, 0);
  f.update('running'); assert.equal(f.video.paused, false); assert.ok(f.sent.some(c => c.position === 0 && c.paused === true));
});
test('actual paused clocks keep the server lookahead moving while auto-gated', () => {
  const f = fixture(); f.update('running'); f.video.play(); f.update('running', true);
  assert.equal(f.video.paused, true); assert.equal(f.record.intent, true); f.sent.length = 0; f.controller.clocks();
  assert.equal(f.sent.length, 1); assert.equal(f.sent[0].paused, true); f.update('running', false); assert.equal(f.video.paused, false);
});
test('user pause remains paused when the server gate clears', () => {
  const f = fixture(); f.update('running'); f.video.play(); f.video.pause(); assert.equal(f.record.intent, false);
  f.update('running', true); f.update('running', false); assert.equal(f.video.paused, true);
});
test('completed releases the gate and permits the final lookahead tail to play', () => {
  const f = fixture(); f.update('running'); f.video.play(); f.video.position = 19.5; f.update('running', true);
  f.update('completed'); assert.equal(f.video.paused, false); assert.equal(f.record.intent, true);
});
test('native seeking from completed debounces one API request and suppresses jump clocks', async () => {
  const f = fixture(); f.update('completed'); f.video.play(); f.video.currentTime = 7; await Promise.resolve();
  f.video.currentTime = 9; await Promise.resolve(); assert.equal(f.video.paused, true);
  f.controller.clocks(); assert.equal(f.sent.length, 0); await tick();
  assert.deepEqual(f.seeks, [{name: 'video', position: 9, epoch: 1}]); assert.equal(f.video.paused, true);
  f.update('running'); assert.equal(f.video.paused, false); assert.ok(f.sent.every(c => c.epoch === 2));
});
test('plus/minus controls use the seek API and hold until the new generation runs', async () => {
  const f = fixture(); f.update('running'); f.video.play(); f.sent.length = 0;
  f.controller.skip(f.record, 5); await Promise.resolve(); f.controller.clocks(); assert.equal(f.sent.length, 0); await tick();
  assert.equal(f.seeks[0].position, 5); assert.equal(f.video.paused, true); f.update('running'); assert.equal(f.video.paused, false);
});
test('failed seek blocks invalid clocks and retains an explicit recovery error', async () => {
  const f = fixture(); f.update('running'); f.hooks.seek = async () => { throw new Error('conflict'); };
  f.video.currentTime = 12; await Promise.resolve(); await tick();
  f.sent.length = 0; f.controller.clocks(); assert.equal(f.sent.length, 0); assert.equal(f.record.clockBlocked, true); assert.match(f.errors[0], /conflict/);
});
