"""Loopback-only HTTP/WebSocket host for the local livesub workspace."""
from __future__ import annotations

import asyncio
import json
import logging
import secrets
import tempfile
from contextlib import suppress
from pathlib import Path
from urllib.parse import urlsplit

from aiohttp import WSMsgType, web

from ..core.config import load_config
from .configuration import catalog, default_config, export_document, import_document, validate_editor
from .session import ACTIVE, Session, SessionConflict

MEDIA_EXTENSIONS = {'.mp4', '.m4v', '.mov', '.webm', '.mkv', '.avi', '.wav', '.mp3', '.m4a', '.aac', '.ogg', '.flac', '.opus'}
STATIC = Path(__file__).with_name('static')
STUDIO = Path(__file__).with_name('studio')
MAX_UPLOAD = 2 * 1024 ** 3
SESSION = web.AppKey('session', Session)
CLIENTS = web.AppKey('clients', set)


class MediaLibrary:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.temp = tempfile.TemporaryDirectory(prefix='livesub-media-')
        self.uploads = Path(self.temp.name).resolve()

    def path(self, value: str) -> Path:
        path = Path(value)
        if not path.is_absolute():
            path = self.root / path
        path = path.resolve()
        if not (path.is_relative_to(self.root) or path.is_relative_to(self.uploads)):
            raise ValueError('media must be inside --media-root, or selected with Upload')
        if path.suffix.lower() not in MEDIA_EXTENSIONS:
            raise ValueError('unsupported media file extension')
        if not path.is_file():
            raise ValueError(f'media file not found: {value}')
        return path


@web.middleware
async def local_only(request, handler):
    # Reject DNS rebinding and cross-origin local-service requests. This is a local
    # device/model controller, not an unauthenticated LAN service.
    host = request.host.rsplit(':', 1)[0]
    if host not in {'127.0.0.1', 'localhost'}:
        return web.json_response({'error': 'local host required'}, status=403)
    origin = request.headers.get('Origin')
    if origin and origin != f'{request.scheme}://{request.host}':
        return web.json_response({'error': 'same-origin requests required'}, status=403)
    if request.headers.get('Sec-Fetch-Site') == 'cross-site':
        return web.json_response({'error': 'cross-site request rejected'}, status=403)
    if request.method == 'POST' and not request.path.startswith('/api/upload'):
        if request.content_type != 'application/json':
            return web.json_response({'error': 'application/json required'}, status=415)
    try:
        response = await handler(request)
    except SessionConflict as exc:
        return web.json_response({'error': str(exc)}, status=409)
    except (ValueError, KeyError, TypeError, ImportError, RuntimeError) as exc:
        return web.json_response({'error': str(exc)}, status=400)
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'no-referrer'
    response.headers['Cache-Control'] = 'no-store'
    response.headers['Content-Security-Policy'] = (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; media-src 'self' blob:; connect-src 'self'; "
        "object-src 'none'; frame-ancestors 'none'; base-uri 'none'"
    )
    return response


def create_app(config_path: Path | None = None, media_root: Path | None = None) -> web.Application:
    library = MediaLibrary(media_root or Path.cwd())
    config = load_config(config_path) if config_path else default_config(library.root)
    session = Session(config, library.path)
    app = web.Application(middlewares=[local_only], client_max_size=MAX_UPLOAD)
    app[SESSION] = session
    app[CLIENTS] = set()

    async def read_json(request):
        # Graph documents are small, uploads have a separate streaming endpoint.
        if request.content_length and request.content_length > 2 * 1024 ** 2:
            raise ValueError('configuration request exceeds 2 MiB')
        data = await request.json()
        if not isinstance(data, dict):
            raise ValueError('request must be a JSON object')
        json.dumps(data, allow_nan=False)
        return data

    async def bootstrap(request):
        return web.json_response({'catalog': catalog(), 'config': session.config,
                                  'snapshot': session.snapshot()})

    async def import_config(request):
        data = await read_json(request)
        if session.state in ACTIVE:
            raise SessionConflict('stop the running graph before importing')
        return web.json_response({'config': import_document(data['text'], data.get('format', 'toml'))})

    async def export_config(request):
        data = await read_json(request)
        format = data.get('format', 'toml')
        text = export_document(data['config'], format)
        return web.Response(text=text, content_type='application/json' if format == 'json' else 'application/toml')

    async def validate_config(request):
        data = await read_json(request)
        _, warnings = validate_editor(data['config'])
        return web.json_response({'warnings': warnings})

    async def run(request):
        return web.json_response(await session.start((await read_json(request))['config']))

    async def stop(request):
        return web.json_response(await session.stop())

    async def seek(request):
        data = await read_json(request)
        return web.json_response(await session.seek(data['node'], data['position'], data['epoch']))

    async def playback(request):
        data = await read_json(request)
        session.clock(data['node'], data['position'], data.get('paused', False), data['epoch'])
        return web.json_response(session.snapshot())

    async def media(request):
        return web.FileResponse(library.path(request.query.get('path', '')))

    async def upload(request):
        if session.state in ACTIVE:
            raise SessionConflict('stop the running graph before uploading media')
        filename = Path(request.query.get('name', 'media')).name
        suffix = Path(filename).suffix.lower()
        if suffix not in MEDIA_EXTENSIONS:
            raise ValueError('choose a supported video or audio file')
        path = library.uploads / (secrets.token_hex(12) + suffix)
        size = 0
        try:
            with path.open('wb') as output:
                async for chunk in request.content.iter_chunked(1024 * 1024):
                    size += len(chunk)
                    if size > MAX_UPLOAD:
                        raise ValueError('media upload exceeds 2 GiB; use --media-root for larger files')
                    await asyncio.to_thread(output.write, chunk)
            if not size:
                raise ValueError('media file is empty')
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        return web.json_response({'path': str(path), 'url': '/api/media?path=' + str(path), 'name': filename})

    async def events(request):
        ws = web.WebSocketResponse(heartbeat=20, max_msg_size=16384)
        await ws.prepare(request)
        app[CLIENTS].add(ws)
        try:
            await ws.send_json({'type': 'snapshot', **session.snapshot()})
            async for message in ws:
                if message.type != WSMsgType.TEXT:
                    continue
                try:
                    data = json.loads(message.data)
                    if not isinstance(data, dict) or data.get('type') != 'clock':
                        raise ValueError('unknown WebSocket command')
                    session.clock(data['node'], data['position'], data.get('paused', False), data['epoch'])
                except (ValueError, KeyError, TypeError) as exc:
                    await ws.send_json({'type': 'command_error', 'message': str(exc)})
        finally:
            app[CLIENTS].discard(ws)
            # A disconnected browser must not leave microphone capture running.
            if not app[CLIENTS] and session.state in ACTIVE:
                await session.stop()
                session.log('warning', 'Last browser disconnected; graph stopped')
        return ws

    async def index(request):
        if not (STUDIO / 'index.html').is_file():
            return web.Response(status=503, text='Build the editor first: cd studio && deno task build')
        return web.FileResponse(STUDIO / 'index.html')

    async def studio_asset(request):
        name = request.match_info['name']
        if Path(name).name != name or Path(name).suffix not in {'.js', '.css', '.svg', '.woff2'}:
            raise web.HTTPNotFound()
        path = STUDIO / 'assets' / name
        if not path.is_file():
            raise web.HTTPNotFound()
        return web.FileResponse(path)

    async def asset(request):
        name = request.match_info['name']
        if Path(name).name != name or Path(name).suffix not in {'.js', '.css', '.mjs'}:
            raise web.HTTPNotFound()
        path = STATIC / name
        if not path.is_file():
            raise web.HTTPNotFound()
        return web.FileResponse(path)

    async def broadcast():
        while True:
            await asyncio.sleep(.3)
            if not app[CLIENTS]:
                continue
            message = {'type': 'snapshot', **session.snapshot()}
            async def send(client):
                try:
                    await asyncio.wait_for(client.send_json(message), 2)
                except (TimeoutError, ConnectionError, RuntimeError):
                    app[CLIENTS].discard(client)
                    await client.close()
            await asyncio.gather(*(send(c) for c in list(app[CLIENTS])))

    class RunLogHandler(logging.Handler):
        def emit(self, record):
            if session.state in ACTIVE:
                loop.call_soon_threadsafe(session.log, record.levelname.lower(), record.getMessage(), record.name)

    async def lifecycle(app):
        nonlocal loop
        loop = asyncio.get_running_loop()
        task = asyncio.create_task(broadcast(), name='web-snapshots')
        handler = RunLogHandler(level=logging.WARNING)
        logger = logging.getLogger('livesub')
        logger.addHandler(handler)
        try:
            yield
        finally:
            logger.removeHandler(handler)
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            await session.stop()
            for client in list(app[CLIENTS]):
                await client.close(code=1001, message=b'Server shutting down')
            library.temp.cleanup()

    loop = None
    async def shutdown(app):
        # Close upgraded connections before aiohttp waits for HTTP handlers to
        # finish; waiting until cleanup_ctx would leave live WebSockets open for
        # the full shutdown timeout.
        await session.stop()
        await asyncio.gather(*(client.close(code=1001, message=b'Server shutting down')
                               for client in list(app[CLIENTS])))

    app.on_shutdown.append(shutdown)
    app.cleanup_ctx.append(lifecycle)
    app.add_routes([
        web.get('/', index), web.get('/api/bootstrap', bootstrap),
        web.post('/api/config/import', import_config), web.post('/api/config/export', export_config),
        web.post('/api/validate', validate_config), web.post('/api/run', run), web.post('/api/stop', stop),
        web.post('/api/seek', seek), web.post('/api/playback', playback), web.get('/api/media', media),
        web.post('/api/upload', upload), web.get('/api/events', events),
        web.get('/assets/{name}', studio_asset),
        web.get('/static/{name}', asset), web.get('/{name}', asset),
    ])
    return app
