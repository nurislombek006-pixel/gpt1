"""Free Render web entrypoint. An explicit browser visit wakes the service.

No self-pings, no changes to the existing bot webhook, no public account data.
Configuration stays in Render Environment. Runtime files are ephemeral.
"""
import asyncio
import contextlib
import json
import os
import signal

from server import main as telegram_main

REQUIRED = ('TG_API_ID', 'TG_API_HASH', 'TG_BOT_TOKEN', 'TG_OWNER_ID', 'TG_USER_SESSION')


def status(task, configured):
    if not configured:
        return 'needs_configuration'
    if task is not None and task.done():
        return 'connection_stopped'
    return 'starting_or_running'


def page(state):
    messages = {
        'needs_configuration': 'Сервер работает. Владелец должен один раз заполнить приватные переменные Telegram в Render.',
        'connection_stopped': 'Сервер работает, но подключение Telegram остановилось. Владелец должен проверить настройки и логи Render.',
        'starting_or_running': 'Загрузчик запущен. Открой @frodgrambot и отправь /media. Первое подключение и поиск чатов могут занять время.',
    }
    return ('<!doctype html><html lang="ru"><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>Медиа · @frodgrambot</title><style>'
            'body{margin:0;background:#101820;color:#eff7fa;font:17px/1.6 system-ui;'
            'min-height:100vh;display:grid;place-items:center}main{max-width:540px;padding:32px}'
            'h1{font-size:32px;line-height:1.2}a{display:inline-block;background:#75dec4;'
            'color:#10201e;padding:12px 20px;border-radius:14px;text-decoration:none;font-weight:700}'
            'small{display:block;color:#9ab0bb;margin-top:26px}</style><main>'
            '<h1>Медиа в Telegram</h1><p>' + messages[state] + '</p>'
            '<a href="https://t.me/frodgrambot">Открыть бота</a>'
            '<small>Бесплатный сервер засыпает после простоя. В следующий раз открой эту страницу снова. '
            'Доступ к медиа — только у владельца. На странице нет списка чатов или файлов.</small>'
            '</main></html>').encode()


async def run():
    configured = all(os.environ.get(k) for k in REQUIRED)
    # Keep startup inexpensive: the HTTP port opens without credentials.
    telegram = asyncio.create_task(telegram_main()) if configured else None
    stop = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            asyncio.get_running_loop().add_signal_handler(sig, stop.set)

    async def handle(reader, writer):
        try:
            raw = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), timeout=10)
            if len(raw) > 16384:
                return
            first = raw.split(b'\r\n', 1)[0].decode('ascii', errors='replace').split()
            method, path = first[:2] if len(first) >= 2 else ('', '')
            state = status(telegram, configured)
            code, mime = '200 OK', 'text/html; charset=utf-8'
            body = page(state)
            if method not in ('GET', 'HEAD'):
                code, body = '405 Method Not Allowed', b'Method not allowed'
            elif path.split('?', 1)[0] == '/healthz':
                mime = 'application/json'
                body = json.dumps({'http': 'ok', 'telegram': state}).encode()
            elif path.split('?', 1)[0] != '/':
                code, body = '404 Not Found', b'Not found'
            headers = (f'HTTP/1.1 {code}\r\nContent-Type: {mime}\r\n'
                       f'Content-Length: {len(body)}\r\nConnection: close\r\n'
                       'Cache-Control: no-store\r\nX-Content-Type-Options: nosniff\r\n'
                       'Referrer-Policy: no-referrer\r\n\r\n').encode()
            writer.write(headers + (b'' if method == 'HEAD' else body))
            await writer.drain()
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, ConnectionError):
            pass
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()

    server = await asyncio.start_server(handle, '0.0.0.0', int(os.environ.get('PORT', '10000')), limit=16384)
    print('Free web service ready; Telegram ' + ('configured' if configured else 'needs configuration'), flush=True)
    stopped = asyncio.create_task(stop.wait())
    try:
        async with server:
            await asyncio.wait([stopped] + ([telegram] if telegram else []), return_when=asyncio.FIRST_COMPLETED)
            if telegram and telegram.done():
                # Report only an exception class; keep HTTP available for diagnostics.
                if not telegram.cancelled():
                    exc = telegram.exception()
                    print('Telegram stopped: ' + (type(exc).__name__ if exc else 'disconnected'), flush=True)
                await stopped
    finally:
        stopped.cancel()
        if telegram:
            telegram.cancel()
            await asyncio.gather(telegram, return_exceptions=True)


if __name__ == '__main__':
    asyncio.run(run())
