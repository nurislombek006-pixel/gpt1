"""Free Render web entrypoint. An explicit browser visit wakes the service.

No self-pings, no changes to the existing bot webhook, no public account data.
Configuration stays in Render Environment. Runtime files are ephemeral.
"""
import asyncio
import contextlib
import json
import os
import signal

from recovery import Recovery
from server import main as telegram_main

REQUIRED = ('TG_API_ID', 'TG_API_HASH', 'TG_BOT_TOKEN', 'TG_OWNER_ID', 'TG_USER_SESSION')


def startup_diagnostic(exc):
    """Log only known static checks; never log arbitrary exception text."""
    checks = {
        'User session is not authorized; run prepare_server.py locally': 'user_session_not_authorized',
        'Owner does not match session': 'owner_session_mismatch',
        'Bot does not match token': 'bot_token_mismatch',
        'Bot username does not match expected bot': 'bot_username_mismatch',
    }
    if type(exc) is RuntimeError:
        return checks.get(str(exc), 'runtime_error')
    return type(exc).__name__


def status(task, configured):
    if not configured:
        return 'needs_configuration'
    if task is not None and task.done():
        return 'connection_stopped'
    return 'starting_or_running'


class TelegramRuntime:
    """One connection lifecycle; the web process exclusively owns OS signals."""
    def __init__(self, runner=telegram_main, retry_delay=5):
        self.runner = runner
        self.retry_delay = retry_delay
        self.state = 'connecting'

    def ready(self):
        self.state = 'ready'

    async def run(self):
        delay = self.retry_delay
        while True:
            try:
                await self.runner(handle_signals=False, on_ready=self.ready)
                reason = 'disconnected'
            except asyncio.CancelledError:
                self.state = 'stopped'
                raise
            except Exception as exc:
                reason = type(exc).__name__
                # Revoked/duplicated authorization needs the owner, not a retry loop.
                if reason in {'AuthKeyDuplicatedError', 'AuthKeyUnregisteredError',
                              'SessionRevokedError', 'UserDeactivatedBanError',
                              'AccessTokenInvalidError', 'AccessTokenExpiredError',
                              'KeyError', 'ValueError', 'RuntimeError'}:
                    self.state = 'connection_failed'
                    print('Telegram requires owner attention: ' + startup_diagnostic(exc), flush=True)
                    return
            self.state = 'reconnecting'
            print(f'Telegram reconnecting after {reason}; retry in {delay}s', flush=True)
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                self.state = 'stopped'
                raise
            self.state = 'connecting'
            delay = min(max(delay * 2, self.retry_delay), 60)


def page(state):
    messages = {
        'paused': 'Подключение бота приостановлено владельцем для восстановления входа в Telegram.',
        'needs_configuration': 'Сервер работает. Владелец должен один раз заполнить приватные переменные Telegram в Render.',
        'connection_stopped': 'Сервер работает, но подключение Telegram остановилось. Владелец должен проверить настройки и логи Render.',
        'starting_or_running': 'Загрузчик запущен. Открой @frodgrambot и отправь /media. Первое подключение и поиск чатов могут занять время.',
        'connecting': 'Подключаю Telegram. Обнови страницу через несколько секунд, затем отправь боту /media.',
        'ready': 'Telegram подключён. Открой @frodgrambot, отправь /media или ссылку на медиа. Поиск чатов при первом запуске может занять время.',
        'reconnecting': 'Восстанавливаю соединение с Telegram. Обнови страницу через несколько секунд.',
        'connection_failed': 'Не удалось подключить Telegram. Владелец должен проверить сессию аккаунта и настройки Render.',
        'stopped': 'Сервер завершает работу. Открой эту страницу снова через минуту.',
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
    paused = os.environ.get('TG_MEDIA_PAUSED') == '1'
    recovery = Recovery()
    # Keep startup inexpensive: the HTTP port opens without credentials.
    runtime = TelegramRuntime() if configured and not paused else None
    telegram = asyncio.create_task(runtime.run()) if runtime else None
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
            fields = raw.split(b'\r\n')[1:-2]
            request_headers = {}
            for field in fields:
                name, sep, value = field.partition(b':')
                name = name.decode('ascii').lower().strip()
                if not sep or name in request_headers:
                    return
                request_headers[name] = value.decode('ascii').strip()
            if 'transfer-encoding' in request_headers:
                return
            try:
                length = int(request_headers.get('content-length', '0'))
            except ValueError:
                return
            if not 0 <= length <= 8192:
                return
            payload = await asyncio.wait_for(reader.readexactly(length), timeout=10) if length else b''
            state = 'paused' if paused else runtime.state if runtime else 'needs_configuration'
            code, mime = '200 OK', 'text/html; charset=utf-8'
            body = page(state)
            if path == '/reconnect' or path.startswith('/reconnect/'):
                code, mime, body = await recovery.route(method, path, request_headers, payload)
            elif method not in ('GET', 'HEAD'):
                code, body = '405 Method Not Allowed', b'Method not allowed'
            elif path.split('?', 1)[0] == '/healthz':
                mime = 'application/json'
                body = json.dumps({'http': 'ok', 'telegram': state}).encode()
            elif path.split('?', 1)[0] != '/':
                code, body = '404 Not Found', b'Not found'
            headers = (f'HTTP/1.1 {code}\r\nContent-Type: {mime}\r\n'
                       f'Content-Length: {len(body)}\r\nConnection: close\r\n'
                       'Cache-Control: no-store\r\nX-Content-Type-Options: nosniff\r\n'
                       "Content-Security-Policy: default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'\r\n"
                       'Referrer-Policy: no-referrer\r\n\r\n').encode()
            writer.write(headers + (b'' if method == 'HEAD' else body))
            await writer.drain()
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, ConnectionError, UnicodeError):
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
            await stopped
    finally:
        stopped.cancel()
        await recovery.close()
        if telegram:
            telegram.cancel()
            await asyncio.gather(telegram, return_exceptions=True)


if __name__ == '__main__':
    asyncio.run(run())

