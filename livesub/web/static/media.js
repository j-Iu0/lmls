import {el} from './dom.js';
import {PlaybackController} from './playback.mjs';
import {mediaSource, overlayCaption, textTopics} from './model.mjs';

export class MediaManager extends PlaybackController {
  constructor(hooks) {
    super(hooks);
    this.clockTimer = setInterval(() => this.clocks(), 200);
    this.overlayTimer = setInterval(() => this.overlays(), 120);
  }
  mount(node, host) {
    const source = mediaSource(node);
    if (source === null) { this.remove(node.name); host.replaceChildren(); return; }
    let record = this.records.get(node.name);
    if (!record) {
      const video = el('video', {controls: true, preload: 'metadata', playsinline: true, 'aria-label': `${node.name} media preview`});
      const overlay = el('div', {class: 'video-overlay', 'aria-live': 'off', hidden: true});
      const stage = el('div', {class: 'media-stage'}, video, overlay);
      const status = el('div', {class: 'media-status mono'}, 'Local media preview');
      const error = el('div', {class: 'media-error', role: 'status', hidden: true});
      const select = el('select', {'aria-label': 'Overlay text topic', 'data-config-control': true});
      const auto = el('input', {type: 'checkbox', 'data-config-control': true});
      const file = el('input', {type: 'file', accept: 'audio/*,video/*', hidden: true});
      const upload = el('button', {class: 'upload-button', title: 'Upload a local audio or video file', 'data-config-control': true}, '↥ File');
      record = {name: node.name, video, overlay, stage, status, error, select, auto, file, upload, source: null, intent: false, autoGate: false, expectedPauses: 0, seeking: false, requestInFlight: false, seekVersion: 0, timer: null, disposed: false};
      this.records.set(node.name, record);
      const minus = el('button', {title: 'Seek backward 5 seconds', onclick: () => this.skip(record, -5)}, '−5s');
      const plus = el('button', {title: 'Seek forward 5 seconds', onclick: () => this.skip(record, 5)}, '+5s');
      const fullscreen = el('button', {title: 'Fullscreen video and subtitles', onclick: () => {
        if (document.fullscreenElement === stage) document.exitFullscreen().catch(e => this.error(record, e.message));
        else if (stage.requestFullscreen) stage.requestFullscreen().catch(e => this.error(record, `Fullscreen unavailable: ${e.message}`));
        else this.error(record, 'This browser does not support fullscreen containers.');
      }}, '⛶');
      host.append(stage, el('div', {class: 'media-controls'}, el('span', {}, minus, plus), upload, fullscreen), status, error,
        el('label', {class: 'overlay-setting'}, el('span', {}, 'TEXT OVERLAY'), select),
        el('label', {class: 'check-label'}, auto, 'Pause for final overlay caption'), file);
      upload.addEventListener('click', () => { if (!this.hooks.locked()) file.click(); });
      file.addEventListener('change', async () => {
        const chosen = file.files?.[0]; file.value = ''; if (!chosen || this.hooks.locked()) return;
        try { await this.hooks.upload(node.name, chosen); } catch (e) { this.error(record, e.message); }
      });
      select.addEventListener('change', () => {
        if (this.hooks.locked()) { this.updateControls(record); return; }
        const config = this.hooks.config(); if (select.value) config.editor.overlays[record.name] = select.value; else delete config.editor.overlays[record.name]; this.hooks.changed(); this.overlays();
      });
      auto.addEventListener('change', () => {
        if (this.hooks.locked()) { this.updateControls(record); return; }
        this.hooks.config().editor.auto_pause[record.name] = auto.checked; this.hooks.changed();
      });
      this.attachPlayback(record);
      video.addEventListener('error', () => {
        const codes = {1: 'Media loading was aborted.', 2: 'Media could not be fetched. Check the source path and server.', 3: 'Media decoding failed.', 4: 'Unsupported media source or codec. Browser support depends on the local file codec.'};
        this.error(record, `${codes[video.error?.code] || 'Media playback failed.'}${video.error?.message ? ` ${video.error.message}` : ''}`);
      });
      video.addEventListener('loadedmetadata', () => { record.error.hidden = true; status.textContent = `Ready · ${Number.isFinite(video.duration) ? this.duration(video.duration) : 'stream'}`; });
      video.addEventListener('volumechange', () => { /* Native controls retain their own state. */ });
    }
    if (record.source !== source) {
      this.pause(record); record.intent = false; record.source = source; record.error.hidden = true;
      if (source) record.video.src = `/api/media?path=${encodeURIComponent(source)}`;
      else { record.video.removeAttribute('src'); record.video.load(); record.status.textContent = 'Choose a source file or set a media URL.'; }
    }
    this.updateControls(record);
  }
  duration(seconds) { return `${Math.floor(seconds / 60)}:${Math.floor(seconds % 60).toString().padStart(2, '0')}`; }
  updateControls(record) {
    const config = this.hooks.config(), selected = config.editor.overlays[record.name] || '';
    const topics = textTopics(config, this.hooks.catalog()); if (selected && !topics.includes(selected)) topics.push(selected);
    const signature = JSON.stringify(topics);
    if (record.topicSignature !== signature) { record.select.replaceChildren(el('option', {value: ''}, 'No overlay'), ...topics.map(t => el('option', {value: t}, t))); record.topicSignature = signature; }
    record.select.value = selected; record.auto.checked = Boolean(config.editor.auto_pause[record.name]);
    for (const control of [record.select, record.auto, record.upload]) control.disabled = this.hooks.locked();
  }
  setLocked() { for (const r of this.records.values()) this.updateControls(r); }
  snapshot(snapshot) {
    this.updatePlayback(snapshot);
    for (const [name, record] of this.records) {
      const media = snapshot.media?.[name];
      const gate = record.autoGate;
      if (media) {
        record.status.textContent = [['starting', 'seeking'].includes(snapshot.state) ? 'Preparing runtime · paused' : media.status || 'media', media.auto_paused ? 'waiting for final caption' : '', Number.isFinite(media.decoded_s) ? `${media.decoded_s.toFixed(1)}s decoded` : '', media.pending != null ? `${media.pending} pending` : ''].filter(Boolean).join(' · ');
        record.status.classList.toggle('warning-text', gate);
      }
    }
    this.overlays();
  }
  overlays() {
    const config = this.hooks.config(); if (!config) return;
    const rows = this.hooks.captions();
    for (const record of this.records.values()) {
      const topic = config.editor.overlays[record.name];
      const row = topic && overlayCaption(rows, topic, record.name, record.video.currentTime);
      const text = row?.text || ''; if (record.overlay.textContent !== text) record.overlay.textContent = text;
      record.overlay.hidden = !text;
    }
  }
  error(record, message) { record.error.textContent = message; record.error.hidden = false; this.hooks.log('error', record.name, message); }
  remove(name) {
    const r = this.records.get(name); if (!r) return;
    r.disposed = true; clearTimeout(r.timer); this.pause(r); r.video.removeAttribute('src'); r.video.load(); this.records.delete(name); this.seekingChanged();
  }
}
