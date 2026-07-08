import logging
from telethon import TelegramClient, events
import json, os
from telegram import Bot
import spacy

# Enable logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
                    level=logging.INFO)

logger = logging.getLogger(__name__)

api_id = '20543362'  # ваш API ID
api_hash = '05171c25fc162e0665a4c37a53e3842f'  # ваш API HASH
bot_token = '8181107355:AAHF_9rdZqpsz1MciSgIoVX5meQ2QBJlIcA'


logger.info('Bot version 1.1')

bot = Bot(token=bot_token)
file_path = os.path.join(os.path.abspath(os.path.dirname(__file__)), 'settings.json')


logger.info('Channels file path: %s',  file_path)
try:
    with open(file_path, 'r', encoding='utf-8') as file:
        data = json.load(file)
except Exception as e:
    logger.error('Failed to open file: %s', e)

logger.info('Channels loaded: %s', data)

all_channels=[]

for obj in data:
    all_channels.extend(obj["channels"])

all_channels = set(all_channels)

logger.info('All channels: %s', all_channels)

nlp = spacy.load("ru_core_news_lg")  # Для русского языка

client = TelegramClient('session', api_id, api_hash)

@client.on(events.NewMessage())

async def new_message(event):
    if event.is_channel and event.message.text:
        doc = nlp(event.message.text)
        logger.info(f"Message: {event.message.text}")
        allowed_pos = {"NOUN", "ADJ", "PROPN"}
        doc_json = {
            "id": event.message.id,
            "date": event.message.date.isoformat(),
            "chat_id": event.chat.id,
            "chat_title": event.chat.title,
            "text": doc.text,
            "tokens": ", ".join(
                token.lemma_
                for token in doc
                if token.pos_ in allowed_pos and not token.is_stop
            ),
            "entities": [
                {
                    "text": ent.text,
                    "label": ent.label_
                }
                for ent in doc.ents
            ]
        }
        logger.info(f"Doc as JSON: {json.dumps(doc_json, ensure_ascii=False)}")
    #keywords = [token.text for token in doc if token.is_alpha and not token.is_stop]
    #print("Ключевые слова:", set(keywords))

    
async def new_message_old(event):
    if event.is_channel:
        logger.debug('New message from channel %s: %s', event.chat.username, event.message.text)
        for element in data:
            if event.chat.username in element["channels"]:
                logger.info('Chat title matches: %s = %s', event.chat.username, element["channels"])
                for keyword in element["keywords"]:
                    logger.info('keyword %s = elements %s', keyword, element["keywords"])
                    if keyword.lower() in event.message.text.lower():
                        logger.info('Keyword found %s', keyword)
                        message=f"{event.chat.title}\n\n{event.message.text}" 
                        logger.debug('Message: %s', message)
                        #await bot.send_message(element["chat_id"], event.message.text)
                        break
    



async def main():
    try:
        await client.start()
        logger.info('Client started successfully')
    except Exception as e:
        logger.error('Failed to start client: %s', e)
    await client.run_until_disconnected()

if __name__ == '__main__':
    #asyncio.run(main())
    client.loop.run_until_complete(main())