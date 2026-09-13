const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

// Run the same plain JavaScript imported by QML, without a second implementation.
const api = vm.createContext({});
vm.runInContext(fs.readFileSync(path.join(__dirname, '../Subtitles.js'), 'utf8'), api);
const options = {sourceLanguage: 'en', targetLanguage: 'vi',
    sourceTopics: ['text.corrected', 'text.raw'], translationTopics: [],
    timeoutMs: 8000, historyLimit: 20};
const event = (overrides = {}) => ({type: 'subtitle', segment_id: 'u1',
    topic: 'text.raw', lang: 'en', revision: 0, text: 'raw English',
    t_audio_end: 1, t_emit: 1, ...overrides});

test('an unselected language cannot hide or prolong the current subtitle', () => {
    const store = api.createStore(options);
    store.receive(event(), 1000);
    store.receive(event({segment_id: 'u2', lang: 'zh', topic: 'text.zh',
        t_audio_end: 2, t_emit: 8, text: '中文'}), 8000);
    assert.equal(store.view(8000).source, 'raw English');
    assert.equal(store.view(9000).source, '');
});

test('history stays bounded and reset accepts restarted segment IDs', () => {
    const store = api.createStore({...options, historyLimit: 2});
    for (let i = 1; i <= 10; i++)
        store.receive(event({segment_id: 'u' + i, t_audio_end: i, t_emit: i}), i * 1000);
    assert.equal(store.size(), 2);
    store.receive(event({text: 'Evicted old segment'}), 11000);
    assert.equal(store.size(), 2);
    assert.equal(store.view(11000).segmentId, 'u10');
    store.reset();
    assert.equal(store.view(11000).source, '');
    store.receive(event({text: 'New session', t_emit: 12}), 12000);
    assert.equal(store.view(12000).source, 'New session');
});

test('invalid messages and missing topic metadata cannot corrupt displayed captions', () => {
    const store = api.createStore(options);
    store.receive(event(), 1000);
    for (const invalid of [null, [], {}, event({type: 'status'}), event({topic: undefined}),
        event({revision: -1}), event({text: 42}), event({t_emit: 'bad'}),
        event({lang: ''}), event({segment_id: ''})]) {
        assert.equal(store.receive(invalid, 1100), false);
    }
    assert.equal(store.view(1100).source, 'raw English');
});

test('captions expire; stale replay and late old corrections do not revive them', () => {
    const store = api.createStore(options);
    store.receive(event(), 1000);
    assert.equal(store.view(8999).source, 'raw English');
    assert.equal(store.view(9000).source, '');
    store.receive(event({segment_id: 'u0', t_audio_end: 0.5, t_emit: 10}), 10000);
    assert.equal(store.view(10000).source, '');
    const reconnected = api.createStore(options);
    reconnected.receive(event(), 20000);
    assert.equal(reconnected.view(20000).source, '');
});

test('late events cannot replace the newest utterance, even when first seen late', () => {
    const store = api.createStore(options);
    store.receive(event(), 1000);
    store.receive(event({segment_id: 'u3', t_audio_end: 3, text: 'Current'}), 3000);
    store.receive(event({topic: 'text.corrected', text: 'Old correction'}), 3100);
    store.receive(event({segment_id: 'u2', t_audio_end: 2, text: 'Delayed segment'}), 3200);
    assert.equal(store.view(3200).segmentId, 'u3');
    assert.equal(store.view(3200).source, 'Current');
});

test('corrected source wins over a later raw revision; translation stays paired', () => {
    const store = api.createStore(options);
    store.receive(event({topic: 'text.corrected', text: 'Correct English'}), 1000);
    store.receive(event({revision: 10}), 1100);
    store.receive(event({topic: 'text.out', lang: 'vi', text: 'Tiếng Việt'}), 1200);
    const view = store.view(1200);
    assert.equal(view.source, 'Correct English');
    assert.equal(view.translation, 'Tiếng Việt');
});

test('revisions are checked independently for each topic and language', () => {
    const store = api.createStore(options);
    store.receive(event({revision: 3, text: 'Newest source'}), 1000);
    store.receive(event({revision: 2, text: 'Stale source'}), 1100);
    store.receive(event({topic: 'text.out', lang: 'vi', revision: 4, text: 'Mới'}), 1200);
    store.receive(event({topic: 'text.out', lang: 'vi', revision: 3, text: 'Cũ'}), 1300);
    store.receive(event({topic: 'text.out', lang: 'zh', revision: 20, text: '中文'}), 1400);
    assert.equal(store.view(1400).source, 'Newest source');
    assert.equal(store.view(1400).translation, 'Mới');
    store.receive(event({topic: 'text.out', lang: 'vi', revision: 4, text: 'Cập nhật'}), 1500);
    assert.equal(store.view(1500).translation, 'Cập nhật');
});


test('history retains expired captions in speech order and receives late corrections', () => {
    const store = api.createStore(options);
    store.receive(event({segment_id: 'u2', t_audio_end: 2, text: 'Second'}), 2000);
    store.receive(event({text: 'First'}), 2100);
    assert.deepEqual(JSON.parse(JSON.stringify(store.history())).map(row => row.source), ['First', 'Second']);
    store.receive(event({topic: 'text.corrected', text: 'First corrected'}), 2200);
    assert.equal(store.history()[0].source, 'First corrected');
    assert.equal(store.view(20000).source, '');
    assert.equal(store.history().length, 2);
});
