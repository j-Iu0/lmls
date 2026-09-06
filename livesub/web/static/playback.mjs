// Browser-independent playback coordination. The native video is an EventTarget.
export class PlaybackController {
  constructor(hooks) { this.hooks = hooks; this.records = new Map(); this.seekBusy = false; }
  attachPlayback(record) {
    Object.assign(record, {intent: false, autoGate: false, expectedPauses: 0, seeking: false, seekVersion: 0, timer: null, disposed: false, resetting: false, pendingSeek: null, clockBlocked: false, anchor: 0});
    const video = record.video;
    video.addEventListener('play', () => {
      record.intent = true;
      if (record.autoGate || record.seeking || this.hooks.preparing?.()) this.pause(record);
      this.clock(record);
    });
    video.addEventListener('pause', () => {
      if (record.expectedPauses) record.expectedPauses--;
      else if (!record.seeking) record.intent = false;
      this.clock(record);
    });
    video.addEventListener('ended', () => { record.intent = false; this.clock(record); });
    video.addEventListener('seeking', () => {
      if (record.resetting) return;
      if (this.hooks.canSeek() || record.seeking) { this.beginSeek(record); clearTimeout(record.timer); }
      else if (this.hooks.active()) {
        this.pause(record); record.resetting = true; video.currentTime = record.anchor;
        this.error(record, 'Wait until the runtime is running before seeking.');
      }
    });
    video.addEventListener('seeked', () => {
      this.overlays?.();
      if (record.resetting) { record.resetting = false; return; }
      if (this.hooks.canSeek() || record.seeking) { if (!record.seeking) this.beginSeek(record); this.scheduleSeek(record); }
      else this.clock(record);
    });
  }
  pause(record) { if (!record.video.paused) { record.expectedPauses++; record.video.pause(); } }
  play(record) {
    if (!record.source || record.disposed) return;
    record.video.play().catch(e => { if (e.name !== 'AbortError') { record.intent = false; this.error(record, `Playback needs attention: ${e.message}`); } });
  }
  startFromGesture() {
    for (const record of this.records.values()) {
      this.pause(record); clearTimeout(record.timer); record.pendingSeek = null;
      record.intent = true; record.autoGate = true; record.seeking = false; record.clockBlocked = false; record.seekVersion++; record.anchor = 0;
      if (record.video.currentTime !== 0) { record.resetting = true; record.video.currentTime = 0; }
      // Exercise play inside the Run gesture, then hold at zero during model loading.
      this.play(record); this.pause(record);
    }
    this.seekingChanged();
  }
  stop() {
    for (const r of this.records.values()) { r.intent = false; r.autoGate = false; this.pause(r); clearTimeout(r.timer); r.seeking = false; r.pendingSeek = null; r.seekVersion++; }
    this.seekingChanged();
  }
  beginSeek(record) {
    if (!record.seeking) record.intent = record.intent || !record.video.paused;
    record.seeking = true; record.seekVersion++; this.pause(record); this.hooks.clearCaptions(); this.seekingChanged();
  }
  skip(record, seconds) {
    const video = record.video;
    if (!record.source || video.readyState < 1) { this.error(record, 'Wait for media metadata before seeking.'); return; }
    if (this.hooks.active() && !this.hooks.canSeek() && !record.seeking) { this.error(record, 'Wait until the runtime is running before seeking.'); return; }
    const target = Math.max(0, Math.min(Number.isFinite(video.duration) ? video.duration : Infinity, video.currentTime + seconds));
    if (target === video.currentTime) return;
    if (this.hooks.canSeek()) this.beginSeek(record);
    try { video.currentTime = target; } catch (e) { record.seeking = false; this.seekingChanged(); this.error(record, e.message); }
  }
  scheduleSeek(record) {
    clearTimeout(record.timer);
    record.timer = setTimeout(() => {
      record.pendingSeek = {position: record.video.currentTime, version: record.seekVersion};
      this.flushSeeks();
    }, 180);
  }
  async flushSeeks() {
    if (this.seekBusy || !this.hooks.canSeek()) return;
    const record = [...this.records.values()].find(r => r.pendingSeek && !r.disposed);
    if (!record) return;
    const {position, version} = record.pendingSeek; record.pendingSeek = null;
    if (version !== record.seekVersion) return;
    this.seekBusy = true; this.seekingChanged();
    try {
      await this.hooks.seek(record.name, position);
      if (version === record.seekVersion) { record.seeking = false; record.clockBlocked = false; record.anchor = position; }
    } catch (e) {
      if (version === record.seekVersion) { record.seeking = false; record.intent = false; record.clockBlocked = true; }
      this.error(record, `Seek failed: ${e.message}. Seek again or restart the run to resynchronize.`);
    } finally {
      this.seekBusy = false; this.seekingChanged(); this.flushSeeks();
      if (!record.seeking && record.intent && !record.autoGate && this.hooks.canSeek()) this.play(record);
    }
  }
  hasPendingSeek() { return this.seekBusy || [...this.records.values()].some(r => r.seeking || r.pendingSeek); }
  seekingChanged() { this.hooks.seekingChanged?.(this.hasPendingSeek()); }
  clock(record) {
    if (!this.hooks.active() || this.hasPendingSeek() || record.video.seeking || record.resetting || record.clockBlocked || !record.source || !this.hooks.clockAllowed()) return;
    this.hooks.send({type: 'clock', node: record.name, position: record.video.currentTime, paused: record.video.paused, epoch: this.hooks.epoch()});
  }
  clocks() { for (const r of this.records.values()) this.clock(r); }
  updatePlayback(snapshot) {
    const startup = ['starting', 'seeking', 'stopping'].includes(snapshot.state) || this.hooks.preparing?.();
    for (const [name, r] of this.records) {
      r.autoGate = Boolean(startup || snapshot.media?.[name]?.auto_paused);
      if (r.autoGate) this.pause(r);
      else if (r.intent && !r.seeking && ['running', 'completed'].includes(snapshot.state) && r.video.paused) { this.clock(r); this.play(r); }
    }
    this.flushSeeks();
  }
}
