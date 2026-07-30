"""Базовый класс плагина сырого роутинга.

Плагин перехватывает сообщения из своих source-каналов ДО основного пайплайна
(без DeepSeek / скоринга / дедупа) и складывает их в собственное хранилище.
Каналы плагина в личный фид НЕ попадают (bypass в on_new_message).

Новый фид = новый модуль в plugins/ с объектом PLUGIN. Нет модуля — нет
функционала, бот работает как раньше.
"""
from telethon import TelegramClient, utils


class FeedPlugin:
    name: str = ""
    # usernames каналов-источников (без @); резолвятся в channel_id при setup()
    source_channels: list[str] = []

    def __init__(self):
        # marked id'ы (форма -100...), какими они приходят в event.chat_id
        self.channel_ids: set[int] = set()

    async def setup(self, client: TelegramClient) -> None:
        """Резолвит usernames в channel_id и инициализирует хранилище.
        Вызывается один раз после старта userbot-клиента."""
        for uname in self.source_channels:
            ent = await client.get_entity(uname)
            self.channel_ids.add(utils.get_peer_id(ent))  # == event.chat_id
        await self.init_storage()

    async def init_storage(self) -> None:
        """Создать/мигрировать своё хранилище. Переопределяется плагином."""

    async def handle(self, event) -> None:
        """Обработать сообщение из source-канала. Переопределяется плагином."""
        raise NotImplementedError
