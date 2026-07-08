"""
Запускать ОДИН РАЗ для авторизации Telegram-аккаунта.
Создаёт session-файл в DATA_DIR.

Использование:
  docker run -it --rm \
    -e TG_API_ID=... -e TG_API_HASH=... \
    -e BOT_TOKEN=stub -e NOTIFY_CHAT_ID=0 -e DEEPSEEK_API_KEY=stub \
    -v $(pwd)/data:/app/data \
    tgfeed python init_session.py
"""
import asyncio
import os
import config
from telethon import TelegramClient


async def main():
    os.makedirs(config.DATA_DIR, exist_ok=True)
    client = TelegramClient(config.SESSION_PATH, config.TG_API_ID, config.TG_API_HASH)
    await client.start()
    me = await client.get_me()
    print(f"\nАвторизован: {me.first_name} (@{me.username})")
    print(f"Session сохранён: {config.SESSION_PATH}.session\n")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
