"""Exercise the actual local HTTP API and playback generation boundaries."""
from __future__ import annotations

import asyncio
import copy
from pathlib import Path

import pytest

pytest.importorskip('aiohttp')
pytest.importorskip('tomli_w')
from aiohttp.test_utils import TestClient, TestServer
from livesub.core.types import Lineage, TextFrame, Utterance
from livesub.web.configuration import default_config, to_editor
from livesub.web.server import SESSION, create_app
from livesub.web.session import Session, SessionConflict

ROOT = Path(__file__).resolve().parents[1]


async def wait_state(session, states):
    async with asyncio.timeout(5):
        while session.state not in states:
            await asyncio.sleep(.01)


@pytest.fixture
async def client():
    app = create_app(media_root=ROOT)
    async with TestClient(TestServer(app)) as client:
        yield client


async def test_bootstrap_roundtrip_and_range_media(client):
    response = await client.get('/api/bootstrap')
    assert response.status == 200
    data = await response.json()
    assert any(c['impl'] == 'media' and c['outputs'] == {'audio': 'AudioFrame'} for c in data['catalog'])
    for format in ('toml', 'json'):
        exported = await client.post('/api/config/export', json={'config': data['config'], 'format': format})
        assert exported.status == 200
        imported = await client.post('/api/config/import', json={'text': await exported.text(), 'format': format})
        assert (await imported.json())['config'] == data['config']
    media = await client.get('/api/media', params={'path': 'assets/lecture.wav'}, headers={'Range': 'bytes=0-31'})
    assert media.status == 206
    assert len(await media.read()) == 32
    assert media.headers['Content-Range'].startswith('bytes 0-31/')


async def test_server_rejects_outside_files_cross_origin_and_malformed_config(client):
    for path in ('../README.md', '/etc/passwd', 'pyproject.toml', 'does-not-exist.mp4'):
        response = await client.get('/api/media', params={'path': path})
        assert response.status == 400
    response = await client.post('/api/stop', json={}, headers={'Origin': 'https://evil.example'})
    assert response.status == 403
    response = await client.get('/api/bootstrap', headers={'Host': 'evil.example'})
    assert response.status == 403
    response = await client.post('/api/run', json={'config': {'nodes': 'bad'}})
    assert response.status == 400
    assert client.server.app[SESSION].state == 'idle'


async def test_run_locks_graph_streams_profiles_and_disconnect_stops(client):
    session = client.server.app[SESSION]
    ws = await client.ws_connect('/api/events')
    first = await ws.receive_json()
    assert first['type'] == 'snapshot'
    response = await client.post('/api/run', json={'config': session.config})
    assert response.status == 200
    await wait_state(session, {'running', 'failed'})
    assert session.state == 'running', session.error
    response = await client.post('/api/run', json={'config': session.config})
    assert response.status == 409
    response = await client.post('/api/config/import', json={'text': '', 'format': 'toml'})
    assert response.status == 409
    # Feed a real fixture through FFmpeg -> VAD -> mock ASR -> correction -> translation.
    for position in (0, 1, 2, 3, 4, 5, 6):
        await ws.send_json({'type': 'clock', 'node': 'media', 'position': position,
                            'paused': False, 'epoch': session.epoch})
        await asyncio.sleep(.2)
    async with asyncio.timeout(8):
        while not session.subtitles:
            await asyncio.sleep(.05)
    snap = session.snapshot()
    assert snap['metrics']['stages']['segment']['n'] > 0
    assert snap['nodes']['segment']['queues']
    assert snap['nodes']['media']['level_dbfs'] <= 0
    assert any(s['meta'].get('mock') for s in snap['subtitles'])
    await ws.close()
    await wait_state(session, {'idle'})
    assert session.task is None


async def test_seek_discards_captions_and_rejects_stale_generation(client):
    session = client.server.app[SESSION]
    ws = await client.ws_connect('/api/events')
    await client.post('/api/run', json={'config': session.config})
    await wait_state(session, {'running', 'failed'})
    old_epoch = session.epoch
    frame = TextFrame('old text', lineage=Lineage(segment_id='u0001'))
    session._event(old_epoch, {'kind': 'output', 'node': 'transcribe', 'topic': 'text.raw', 'payload': frame})
    assert session.subtitles
    response = await client.post('/api/seek', json={'node': 'media', 'position': .25, 'epoch': old_epoch})
    assert response.status == 200, await response.text()
    assert session.epoch > old_epoch
    assert not session.subtitles
    session._event(old_epoch, {'kind': 'output', 'node': 'transcribe', 'topic': 'text.raw', 'payload': frame})
    assert not session.subtitles
    response = await client.post('/api/playback', json={'node': 'media', 'position': 1,
                                                       'paused': False, 'epoch': old_epoch})
    assert response.status == 409
    await ws.close()


async def test_subtitle_revisions_and_auto_pause_release(client):
    import numpy as np
    session = client.server.app[SESSION]
    ws = await client.ws_connect('/api/events')
    await client.post('/api/run', json={'config': session.config})
    await wait_state(session, {'running', 'failed'})
    def emit(node, topic, payload):
        session._event(session.epoch, {'kind': 'output', 'node': node, 'topic': topic, 'payload': payload})
    utterance = Utterance('s', np.zeros(16000, dtype=np.float32), 0, 1, True)
    emit('segment', 'utterance.speech', utterance)
    assert session.snapshot()['media']['media']['auto_paused']
    emit('translate', 'text.translated', TextFrame('new', lineage=Lineage('s', 2)))
    assert not session.snapshot()['media']['media']['auto_paused']
    emit('translate', 'text.translated', TextFrame('old', lineage=Lineage('s', 1)))
    assert session.snapshot()['subtitles'][-1]['text'] == 'new'
    await ws.close()


async def test_invalid_connection_rejected_without_starting(client):
    doc = copy.deepcopy(client.server.app[SESSION].config)
    doc['nodes'][2]['inputs'] = {'audio': 'audio.raw'}
    response = await client.post('/api/run', json={'config': doc})
    assert response.status == 400
    assert 'expects Utterance' in (await response.json())['error']
    assert client.server.app[SESSION].state == 'idle'


async def test_upload_is_scoped_and_empty_upload_is_rejected(client):
    response = await client.post('/api/upload?name=sample.wav', data=(ROOT/'assets/lecture.wav').read_bytes())
    assert response.status == 200
    uploaded = await response.json()
    media = await client.get('/api/media', params={'path': uploaded['path']}, headers={'Range': 'bytes=0-3'})
    assert await media.read() == b'RIFF'
    response = await client.post('/api/upload?name=sample.html', data=b'bad')
    assert response.status == 400
    response = await client.post('/api/upload?name=empty.mp4', data=b'')
    assert response.status == 400


async def test_fast_final_before_upstream_observer_does_not_stall_auto_pause(client):
    import numpy as np
    session = client.server.app[SESSION]
    ws = await client.ws_connect('/api/events')
    await client.post('/api/run', json={'config': session.config})
    await wait_state(session, {'running', 'failed'})
    # Bus backpressure can yield to consumers before successful upstream delivery
    # returns and sends its observer notification.
    session._event(session.epoch, {'kind': 'output', 'node': 'translate', 'topic': 'text.translated',
                                  'payload': TextFrame('ready', lineage=Lineage('fast', 1))})
    session._event(session.epoch, {'kind': 'output', 'node': 'segment', 'topic': 'utterance.speech',
                                  'payload': Utterance('fast', np.zeros(16000, dtype=np.float32), 0, 1, True)})
    snap = session.snapshot()
    assert not snap['media']['media']['auto_paused']
    assert snap['subtitles'][-1]['source'] == 'media'
    await ws.close()
