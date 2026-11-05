"""Temporary owner-checked QR recovery. Disabled unless explicitly configured.

The browser can approve login but cannot export the session. A separate secret
lets the deployment operator persist the session in Render, then disable recovery.
No Telegram credentials, QR payloads, passwords, or sessions are logged.
"""
import asyncio
import contextlib
import hashlib
import hmac
import io
import json
import os
import time

import qrcode
from qrcode.image.svg import SvgPathImage
from telethon import TelegramClient, errors
from telethon.sessions import StringSession


def qr_svg(url):
    out = io.BytesIO()
    qrcode.make(url, image_factory=SvgPathImage, border=4).save(out)
    return out.getvalue().decode().split('?>', 1)[-1]


class Recovery:
    def __init__(self, config=None, client_factory=TelegramClient, renderer=qr_svg, clock=time.time):
        self.config = dict(os.environ if config is None else config)
        self.clock, self.client_factory, self.renderer = clock, client_factory, renderer
        self.state = 'idle'
        self.svg = ''
        self.session = ''
        self.task = None
        self.client = None
        self.password_result = None
        self.password_attempts = 0
        self.lock = asyncio.Lock()
        try:
            self.expires = int(self.config.get('TG_RECOVERY_EXPIRES', '0'))
        except ValueError:
            self.expires = 0

    def enabled(self):
        return (self.config.get('TG_MEDIA_PAUSED') == '1'
                and self.clock() < self.expires
                and all(self.config.get(k) for k in ('TG_API_ID', 'TG_API_HASH', 'TG_OWNER_ID'))
                and len(self.config.get('TG_RECOVERY_HASH', '')) == 64
                and len(self.config.get('TG_RECOVERY_EXPORT_HASH', '')) == 64)

    def authorized(self, authorization, export=False):
        if not self.enabled() or not authorization.startswith('Bearer '):
            return False
        key = 'TG_RECOVERY_EXPORT_HASH' if export else 'TG_RECOVERY_HASH'
        digest = hashlib.sha256(authorization[7:].encode()).hexdigest()
        return hmac.compare_digest(digest, self.config[key])

    def status(self):
        return {'state': self.state, 'svg': self.svg if self.state == 'qr' else ''}

    async def start(self):
        async with self.lock:
            if self.task is None:
                self.state = 'connecting'
                self.task = asyncio.create_task(self._run())
        return self.status()

    async def _run(self):
        candidate = ''
        try:
            timeout = min(1800, max(0, self.expires - self.clock()))
            async with asyncio.timeout(timeout):
                self.client = self.client_factory(StringSession(), int(self.config['TG_API_ID']),
                                                  self.config['TG_API_HASH'],
                                                  device_model='Render media recovery',
                                                  app_version='1.0', flood_sleep_threshold=0)
                await self.client.connect()
                while True:
                    qr = await self.client.qr_login()
                    self.svg = self.renderer(qr.url)
                    self.state = 'qr'
                    try:
                        # Begin waiting in this task before yielding to HTTP/browser requests.
                        await qr.wait()
                        break
                    except asyncio.TimeoutError:
                        continue
                    except errors.SessionPasswordNeededError:
                        self.svg = ''
                        self.password_result = asyncio.get_running_loop().create_future()
                        self.state = 'password'
                        await self.password_result
                        break
                account = await self.client.get_me()
                if account is None or account.bot or account.id != int(self.config['TG_OWNER_ID']):
                    self.state = 'wrong_owner'
                    await self.client.log_out()
                    return
                candidate = StringSession.save(self.client.session)
        except asyncio.CancelledError:
            self.state = 'stopped'
            raise
        except TimeoutError:
            self.state = 'expired'
        except Exception:
            self.state = 'failed'
        finally:
            self.svg = ''
            if self.client:
                with contextlib.suppress(Exception):
                    await self.client.disconnect()
        if candidate:
            self.session = candidate
            self.state = 'authorized'

    async def password(self, value):
        async with self.lock:
            if self.state not in ('password', 'incorrect_password') or not self.client:
                return {'state': self.state}
            if not isinstance(value, str) or not 0 < len(value) <= 1024:
                return {'state': 'invalid_password'}
            self.password_attempts += 1
            if self.password_attempts > 3:
                if self.task:
                    self.task.cancel()
                self.state = 'failed'
                return self.status()
            try:
                await self.client.sign_in(password=value)
                self.state = 'verifying'
                self.password_result.set_result(True)
            except errors.PasswordHashInvalidError:
                self.state = 'incorrect_password'
            except Exception:
                self.state = 'failed'
                if self.task:
                    self.task.cancel()
            return self.status()

    async def route(self, method, path, headers, payload):
        if not self.enabled():
            return '404 Not Found', 'application/json', b'{}'
        if path == '/reconnect' and method == 'GET':
            return '200 OK', 'text/html; charset=utf-8', PAGE.encode()
        export = path == '/reconnect/session'
        if not self.authorized(headers.get('authorization', ''), export=export):
            return '404 Not Found', 'application/json', b'{}'
        response = {}
        code = '200 OK'
        if method == 'POST' and path == '/reconnect/start':
            response = await self.start()
        elif method == 'GET' and path == '/reconnect/status':
            response = self.status()
        elif method == 'POST' and path == '/reconnect/password':
            try:
                response = await self.password(json.loads(payload).get('password'))
            except (ValueError, AttributeError):
                code = '400 Bad Request'
        elif method == 'GET' and export:
            if self.state == 'authorized' and self.session:
                response = {'session': self.session}
            else:
                code = '409 Conflict'
        else:
            code = '404 Not Found'
        return code, 'application/json', json.dumps(response).encode()

    async def close(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        self.session, self.svg = '', ''


PAGE = '''<!doctype html><html lang="ru"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Восстановление Telegram</title>
<style>body{margin:0;background:#101820;color:#eff7fa;font:17px/1.55 system-ui;min-height:100vh;display:grid;place-items:center}main{max-width:510px;padding:30px}h1{font-size:28px;line-height:1.2}button{background:#75dec4;color:#10201e;border:0;border-radius:12px;padding:14px 20px;font:700 17px system-ui;cursor:pointer}#qr{width:280px;max-width:100%;background:white;margin:24px 0;border-radius:10px}#qr svg{width:100%;height:auto;display:block}input{display:block;box-sizing:border-box;width:100%;font:17px system-ui;padding:14px;border-radius:10px;border:1px solid #789;margin:12px 0}small{color:#9ab0bb}form[hidden],#qr:empty{display:none}</style>
<main><h1>Восстановление Telegram</h1>
<p id="state" role="status">Готовлю подключение…</p>
<div id="qr" aria-label="QR-код входа в Telegram"></div>
<form id="password-form" hidden><label for="password">Пароль двухэтапной проверки Telegram</label><input id="password" name="password" type="password" autocomplete="current-password" required><button type="submit">Подтвердить вход</button></form>
<small>Бот приостановлен. В Telegram открой «Настройки → Устройства → Подключить устройство». Подтверди вход для восстановления своего бота. Данные аккаунта не отображаются на этой странице.</small>
<script>
const token=location.hash.slice(1); history.replaceState(null,'','/reconnect');
const state=document.getElementById('state'),qr=document.getElementById('qr'),form=document.getElementById('password-form');
let lastSvg='';
const labels={connecting:'Подключаю Telegram…',qr:'Подтверди вход в Telegram по QR-коду.',password:'Telegram запросил пароль двухэтапной проверки.',incorrect_password:'Пароль не подошёл. Проверь и повтори.',verifying:'Проверяю аккаунт…',authorized:'Вход подтверждён. Сохраняю подключение на сервере.',wrong_owner:'Этот аккаунт не совпадает с владельцем бота. Вход отклонён.',failed:'Не удалось завершить вход. Сообщи об этом в чате.',expired:'Время восстановления истекло.',stopped:'Восстановление остановлено.'};
async function api(path,method='GET',body){const r=await fetch('/reconnect/'+path,{method,headers:{Authorization:'Bearer '+token,'Content-Type':'application/json'},body:body===undefined?undefined:JSON.stringify(body),cache:'no-store'});if(!r.ok)throw Error('unavailable');return r.json()}
function show(s){state.textContent=labels[s.state]||'Ожидаю подключения…';form.hidden=!['password','incorrect_password'].includes(s.state);if(lastSvg!==s.svg){qr.innerHTML=s.svg||'';lastSvg=s.svg||'';}return !['authorized','wrong_owner','failed','expired','stopped'].includes(s.state)}
async function poll(){try{if(show(await api('status')))setTimeout(poll,2000)}catch{state.textContent='Ссылка восстановления недействительна или сервер недоступен.'}}
form.addEventListener('submit',async e=>{e.preventDefault();const field=document.getElementById('password'),password=field.value;field.value='';try{show(await api('password','POST',{password}))}catch{state.textContent='Не удалось подтвердить вход.'}});
if(token){api('start','POST',{}).then(s=>{show(s);poll()}).catch(()=>state.textContent='Ссылка восстановления недействительна.')}else{state.textContent='Открой защищённую ссылку восстановления из текущего сеанса.'}
</script></main></html>'''
