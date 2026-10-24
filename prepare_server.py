"""Run locally next to the old script. Writes a private .env, never prints secrets."""
import asyncio
import getpass
import os
from pathlib import Path

from telethon import TelegramClient
from telethon.sessions import StringSession


async def main():
    os.umask(0o077)
    here = Path(__file__).resolve().parent
    print('Однократная подготовка серверного входа. Секреты не выводятся на экран.')
    print('Сначала закрой старый telegram_media.py (Ctrl+C).')
    api_id = int(input('API ID: ').strip())
    api_hash = getpass.getpass('API HASH: ').strip()
    token = getpass.getpass('Токен того же бота: ').strip()
    if '\n' in api_hash + token or '\r' in api_hash + token:
        raise ValueError('Invalid credentials')
    if not token.split(':', 1)[0].isdigit():
        raise ValueError('Invalid bot token')
    # Verify token identity without sending any message or changing webhooks.
    bot = TelegramClient(StringSession(), api_id, api_hash)
    try:
        await bot.start(bot_token=token)
        bot_me = await bot.get_me()
        if not bot_me.bot or (bot_me.username or '').lower() != 'frodgrambot':
            print('Этот токен не принадлежит @frodgrambot. Возьми токен нужного бота в BotFather.')
            return
    finally:
        await bot.disconnect()
    # Reuse the user's existing local account session if present. No code is
    # sent to an external site. Missing session falls back to Telegram sign-in.
    candidates = [here / 'private' / 'account.session',
                  here.parent / 'private' / 'account.session']
    old = next((p for p in candidates if p.is_file()), None)
    client = TelegramClient(str(old) if old else StringSession(), api_id, api_hash)
    try:
        await client.start(phone=lambda: input('Телефон: ').strip(),
                           code_callback=lambda: input('Код Telegram: ').strip(),
                           password=lambda: getpass.getpass('Пароль 2FA: '))
        me = await client.get_me()
        if me.bot:
            raise ValueError('Use a personal Telegram account')
        session = StringSession.save(client.session)
        target = here / '.env'
        if target.exists():
            if input('Файл .env уже существует. Заменить? да/нет: ').strip().lower() != 'да':
                return
        content = (f'TG_API_ID={api_id}\nTG_API_HASH={api_hash}\nTG_BOT_TOKEN={token}\n'
                   f'TG_OWNER_ID={me.id}\nTG_USER_SESSION={session}\n'
                   'TG_BOT_USERNAME=frodgrambot\nDATA_DIR=/data\nMAX_FILE_MB=1900\n')
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as output:
            output.write(content)
        print('Готово: .env рядом с этим скриптом. Не отправляй его в чат и не загружай в GitHub.')
        print('На Render перенеси значения в Environment сервиса. Для VPS используй docker compose.')
        print('После настройки сервера удали локальную копию .env, если она больше не нужна.')
    finally:
        await client.disconnect()


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print('Остановлено.')
    except Exception as exc:
        print(f'Не удалось подготовить вход: {type(exc).__name__}. Проверь данные и интернет.')
