"""Owner-only Telegram media companion. Run one replica with persistent DATA_DIR."""
import asyncio
import contextlib
import os
import secrets
import shutil
import signal
import tempfile
from pathlib import Path

from telethon import Button, TelegramClient, errors, events, utils
from telethon.sessions import SQLiteSession, StringSession

from core import Store, filename, message_link

HELP = ('Медиа\n\nПришли ссылку на сообщение — получишь вложение.\n'
        'Или напиши /media название чата — найду его среди твоих подписок.\n\n'
        '/media — открыть меню\n/media_status — очередь\n'
        '/media_cancel — отменить все задания\n/media_refresh — обновить список чатов')


class App:
    def __init__(self, user, bot, owner, store, temp_root, max_size):
        self.user, self.bot, self.owner, self.store = user, bot, owner, store
        self.temp_root, self.max_size = temp_root, max_size
        self.recipient = None
        self.searches = {}
        self.wait_search = False
        self.current = None
        self.refreshing = False
        self.shutting_down = False

    def allowed(self, event):
        return event.is_private and event.sender_id == self.owner

    async def say(self, text, buttons=None):
        if self.recipient:
            await self.bot.send_message(self.recipient, text, parse_mode=None, buttons=buttons)

    async def refresh(self):
        if self.refreshing:
            return
        self.refreshing = True
        try:
            ids = set()
            async for d in self.user.iter_dialogs():
                self.store.dialog(d.id, d.name or str(d.id), getattr(d.entity, 'username', None))
                ids.add(d.id)
            # Remove stale chats only after a fully successful refresh.
            for row in self.store.db.execute('SELECT id FROM dialogs').fetchall():
                if row['id'] not in ids:
                    self.store.db.execute('DELETE FROM dialogs WHERE id=?', (row['id'],))
            self.store.db.commit()
            await self.say(f'Список обновлён: {len(ids)} чатов. Напиши /media и название.')
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self.say(f'Не удалось обновить список: {type(exc).__name__}. Повтори /media_refresh позже.')
        finally:
            self.refreshing = False

    def start_refresh(self):
        if getattr(self, 'refresh_task', None) is None or self.refresh_task.done():
            self.refresh_task = asyncio.create_task(self.refresh())

    async def search(self, text):
        rows = self.store.search(text)
        if not rows:
            await self.say('Совпадений пока нет.' + (' Список ещё загружается.' if self.refreshing else '') +
                           ' Попробуй часть названия или ссылку на сообщение.')
            return
        if len(self.searches) >= 30:
            self.searches.pop(next(iter(self.searches)))
        token = secrets.token_hex(4)
        self.searches[token] = rows
        await self.page(token, 0)

    async def page(self, token, page):
        rows = self.searches.get(token)
        if rows is None or page < 0 or page * 8 >= len(rows):
            await self.say('Этот поиск устарел. Напиши /media и название ещё раз.')
            return
        keyboard = [[Button.inline(r['title'][:55], f'mc:{token}:{i}')]
                    for i, r in enumerate(rows[page*8:page*8+8], page*8)]
        nav = []
        if page:
            nav.append(Button.inline('←', f'mp:{token}:{page-1}'))
        if (page+1)*8 < len(rows):
            nav.append(Button.inline('→', f'mp:{token}:{page+1}'))
        if nav:
            keyboard.append(nav)
        await self.say(f'Найдено: {len(rows)} · страница {page+1}. Выбери чат:', keyboard)

    async def enqueue(self, payload):
        if self.shutting_down:
            await self.say('Сервер перезапускается. Повтори запрос через минуту.')
            return
        job = self.store.enqueue(payload)
        await self.say(f'Задание №{job} добавлено. /media_status — состояние, /media_cancel — отмена.')

    async def on_message(self, event):
        if not self.allowed(event):
            return
        self.recipient = await event.get_input_chat()
        text = event.raw_text.strip()
        first, _, arg = text.partition(' ')
        command = first.split('@')[0].lower()
        try:
            if command == '/media_cancel':
                self.store.cancel_queued()
                if self.current and not self.current.done():
                    self.current.cancel()
                await self.say('Отмена запрошена. Уже отправленные файлы останутся в чате.')
            elif command == '/media_status':
                labels = {'queued': 'ожидает', 'running': 'в работе', 'done': 'готово',
                          'cancelled': 'отменено', 'failed': 'ошибка'}
                rows = self.store.status()
                await self.say('\n'.join(f"№{r['id']} · {labels[r['state']]} · отправлено {r['sent']}, "
                                         f"пропущено {r['skipped']}, ошибок {r['errors']}" for r in rows)
                               or 'Очередь пуста.')
            elif command == '/media_refresh':
                self.start_refresh()
                await self.say('Обновляю список чатов в фоне. Ссылки на сообщения уже можно присылать.')
            elif command == '/media':
                if arg.strip():
                    await self.handle_query(arg.strip())
                else:
                    self.wait_search = True
                    await self.say(HELP)
            elif text.startswith(('https://t.me/', 'http://t.me/', 't.me/', 'https://telegram.me/')):
                await self.handle_query(text)
            elif self.wait_search and text and not text.startswith('/'):
                await self.handle_query(text)
        except ValueError as exc:
            await self.say(str(exc))
        except Exception as exc:
            await self.say(f'Не удалось обработать запрос: {type(exc).__name__}. Попробуй ещё раз.')

    async def handle_query(self, text):
        if 't.me/' in text or 'telegram.me/' in text:
            chat, msg = message_link(text)
            await self.enqueue({'chat': chat, 'message': msg})
        else:
            self.wait_search = True
            await self.search(text)

    async def on_callback(self, event):
        if not self.allowed(event):
            await event.answer('Доступ только владельцу.', alert=True)
            return
        self.recipient = await event.get_input_chat()
        try:
            action, token, raw = event.data.decode().split(':')
            if action not in ('mp', 'mc', 'md'):
                return
            await event.answer()
            rows = self.searches.get(token)
            if rows is None:
                await self.say('Кнопка устарела. Повтори поиск через /media.')
                return
            if action == 'mp':
                await self.page(token, int(raw))
                return
            index, _, count = raw.partition('_')
            row = rows[int(index)]
            if action == 'mc':
                await self.say(f"{row['title']}\nСкачать медиа из последних сообщений:",
                               [[Button.inline(str(n), f'md:{token}:{index}_{n}') for n in (10, 50, 100)]])
            elif count in ('10', '50', '100'):
                await self.enqueue({'chat': row['id'], 'limit': int(count)})
        except (ValueError, IndexError):
            await self.say('Кнопка устарела. Повтори поиск через /media.')
        except Exception as exc:
            await self.say(f'Ошибка кнопки: {type(exc).__name__}. Повтори запрос.')

    async def deliver(self, job, chat, msg):
        if not msg or not (msg.photo or msg.document):
            return
        if self.store.seen(chat, msg.id):
            self.store.count(job, 'skipped')
            return
        if (msg.file.size or 0) > self.max_size:
            self.store.count(job, 'errors')
            await self.say(f'Сообщение {msg.id}: файл больше лимита сервера ({self.max_size//1024**2} МБ).')
            return
        # Limit checks are repeated while downloading, including files with unknown size.
        def progress(received, total):
            if received > self.max_size:
                raise ValueError('Файл больше лимита сервера')
        for attempt in range(3):
            try:
                with tempfile.TemporaryDirectory(prefix='media_', dir=self.temp_root) as temp:
                    name = filename(msg.file.name, f'{msg.id}{msg.file.ext or ".bin"}')
                    path = Path(temp) / name
                    if shutil.disk_usage(temp).free < (msg.file.size or self.max_size) + 64*1024**2:
                        raise ValueError('Недостаточно свободного места для временного файла')
                    with path.open('wb') as stream:
                        result = await self.user.download_media(msg, file=stream, progress_callback=progress)
                    if result is None or not path.stat().st_size:
                        raise ValueError('Telegram не выдал файл')
                    if msg.file.size is not None and path.stat().st_size != msg.file.size:
                        raise ValueError('Скачан неполный файл')
                    sent = await self.bot.send_file(
                        self.recipient, str(path), parse_mode=None,
                        caption=f'Источник: {chat} · сообщение {msg.id}\n{msg.message or ""}'[:900],
                        force_document=bool(msg.document),
                        attributes=msg.document.attributes if msg.document else None,
                        mime_type=msg.file.mime_type)
                    self.store.delivered(chat, msg.id, sent.id, job)
                await asyncio.sleep(1.1)
                return
            except errors.FloodWaitError as exc:
                if attempt == 2:
                    raise
                await self.say(f'Telegram просит паузу: {exc.seconds} сек. Задание продолжится автоматически.')
                await asyncio.sleep(exc.seconds + 1)
            except (OSError, asyncio.TimeoutError):
                if attempt == 2:
                    raise
                await asyncio.sleep(2 ** attempt)

    async def job(self, job, payload):
        await self.say(f'Задание №{job}: начинаю.')
        entity = await self.user.get_entity(payload['chat'])
        chat_id = utils.get_peer_id(entity)
        found = 0
        if 'message' in payload:
            msg = await self.user.get_messages(entity, ids=payload['message'])
            if not msg or not (msg.photo or msg.document):
                raise ValueError('В сообщении нет доступного медиа. Укажи ссылку на само вложение.')
            await self.deliver(job, chat_id, msg)
            found = 1
        else:
            async for msg in self.user.iter_messages(entity, limit=payload['limit']):
                if not (msg.photo or msg.document):
                    continue
                found += 1
                try:
                    await self.deliver(job, chat_id, msg)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self.store.count(job, 'errors')
                    await self.say(f'Сообщение {msg.id} пропущено: {type(exc).__name__}.')
        self.store.state(job, 'done')
        row = self.store.db.execute('SELECT * FROM jobs WHERE id=?', (job,)).fetchone()
        await self.say(f"Задание №{job} завершено. Отправлено: {row['sent']}, "
                       f"уже было: {row['skipped']}, ошибок: {row['errors']}." +
                       (' В выбранных сообщениях медиа нет.' if not found else ''))

    async def worker(self):
        while not self.shutting_down:
            if not self.recipient:
                await asyncio.sleep(1)
                continue
            next_job = self.store.next()
            if not next_job:
                await asyncio.sleep(1)
                continue
            job, payload = next_job
            self.current = asyncio.create_task(self.job(job, payload))
            try:
                await self.current
            except asyncio.CancelledError:
                self.store.state(job, 'queued' if self.shutting_down else 'cancelled')
                if self.shutting_down:
                    return
            except Exception as exc:
                self.store.state(job, 'failed')
                self.store.count(job, 'errors')
                detail = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
                with contextlib.suppress(Exception):
                    await self.say(f'Задание №{job} остановлено: {detail}. Проверь доступ к исходному сообщению.')
            finally:
                self.current = None


async def main(*, handle_signals=True, on_ready=None):
    os.umask(0o077)
    api_id = int(os.environ['TG_API_ID'])
    api_hash = os.environ['TG_API_HASH']
    token = os.environ['TG_BOT_TOKEN']
    owner = int(os.environ['TG_OWNER_ID'])
    root = Path(os.environ.get('DATA_DIR', './data')).resolve()
    root.mkdir(parents=True, exist_ok=True)
    # Exactly one replica: sessions and queue live on one persistent disk.
    session = SQLiteSession(str(root / 'account'))
    if session.auth_key is None:
        seed = StringSession(os.environ['TG_USER_SESSION'])
        session.set_dc(seed.dc_id, seed.server_address, seed.port)
        session.auth_key = seed.auth_key
        session.save()
    user = TelegramClient(session, api_id, api_hash)
    bot_id = int(token.split(':', 1)[0])
    bot = TelegramClient(str(root / f'bot_{bot_id}'), api_id, api_hash, flood_sleep_threshold=0)
    app = None
    tasks = []
    stop = asyncio.Event()
    if handle_signals:
        for sig in (signal.SIGTERM, signal.SIGINT):
            with contextlib.suppress(NotImplementedError):
                asyncio.get_running_loop().add_signal_handler(sig, stop.set)
    try:
        await user.connect()
        if not await user.is_user_authorized():
            raise RuntimeError('User session is not authorized; run prepare_server.py locally')
        account = await user.get_me()
        if account.bot or account.id != owner:
            raise RuntimeError('Owner does not match session')
        await bot.start(bot_token=token)
        identity = await bot.get_me()
        if not identity.bot or identity.id != bot_id:
            raise RuntimeError('Bot does not match token')
        expected_username = os.environ.get('TG_BOT_USERNAME', 'frodgrambot').lstrip('@').lower()
        if (identity.username or '').lower() != expected_username:
            raise RuntimeError('Bot username does not match expected bot')
        store = Store(root / f'queue_{owner}_{bot_id}.sqlite3')
        temp_root = root / 'temp'
        temp_root.mkdir(exist_ok=True)
        # Clean only this application's leftovers from interrupted downloads.
        for p in temp_root.glob('media_*'):
            if p.is_dir():
                shutil.rmtree(p)
        app = App(user, bot, owner, store, str(temp_root),
                  int(os.environ.get('MAX_FILE_MB', '1900')) * 1024**2)
        bot.add_event_handler(app.on_message, events.NewMessage(incoming=True))
        bot.add_event_handler(app.on_callback, events.CallbackQuery(pattern=rb'^m[pcd]:'))
        # Resume queue automatically; this is a bot session-local input peer.
        try:
            app.recipient = await bot.get_input_entity(owner)
        except ValueError:
            # The first /media command supplies the owner peer after a fresh deployment.
            pass
        app.start_refresh()
        tasks.append(app.refresh_task)
        tasks.append(asyncio.create_task(app.worker()))
        tasks.append(asyncio.create_task(stop.wait()))
        print('Media companion ready. Owner can send /media to the bot.', flush=True)
        if on_ready:
            on_ready()
        await asyncio.wait([tasks[-1], bot.disconnected, user.disconnected], return_when=asyncio.FIRST_COMPLETED)
    finally:
        if app:
            app.shutting_down = True
            if app.current:
                app.current.cancel()
            if getattr(app, 'refresh_task', None) and app.refresh_task not in tasks:
                tasks.append(app.refresh_task)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await bot.disconnect()
        await user.disconnect()
        if app:
            app.store.db.close()


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except Exception as exc:
        # Never print environment, tokens, session strings or raw request bodies.
        print(f'Startup failed: {type(exc).__name__}. Check private server configuration.', flush=True)
        raise SystemExit(1)
