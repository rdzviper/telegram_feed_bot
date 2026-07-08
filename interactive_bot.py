import logging
from telethon import TelegramClient, events
import json, os
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
import spacy
from telegram.ext import Application, CallbackQueryHandler, ContextTypes


# Enable logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
                    level=logging.INFO)

logger = logging.getLogger(__name__)

api_id = '20543362'  # ваш API ID
api_hash = '05171c25fc162e0665a4c37a53e3842f'  # ваш API HASH
bot_token = '8181107355:AAHF_9rdZqpsz1MciSgIoVX5meQ2QBJlIcA'
notify_chat_id = '-1002877298810'  # ID чата для уведомлений
allowed_pos = {"NOUN", "ADJ", "PROPN"}
alpha = 0.2
current_score = 0

logger.info('Bot version 1.3')

messages_data=[]
messages_data_filter=[]
token_ratios = {}
entity_ratios = {}

bot = Bot(token=bot_token)
messages_data_path = os.path.join(os.path.abspath(os.path.dirname(__file__)), 'messages_data.json')


def load_messages_data():
    global messages_data
    try:
        with open(messages_data_path, 'r', encoding='utf-8') as file:
            messages_data = json.load(file)
            logger.info('DB loaded successfully')
    except Exception as e:
        logger.error('Failed to open file: %s', e)


def filter_data():
    global messages_data_filter
    messages_data_filter = [msg for msg in messages_data if "user_choice" in msg]

def save_messages_data():
    with open(messages_data_path, "w", encoding="utf-8") as file:
        json.dump(messages_data_filter, file, ensure_ascii=False, indent=2)

def calculate_like_dislike_ratio():

    token_likes = {}
    token_dislikes = {}
    entity_likes = {}
    entity_dislikes = {}
    
    for message in messages_data_filter:
        # Определяем, лайк или дизлайк
        is_like = message['user_choice'] == 'like'
        
        # Обрабатываем токены
        tokens = [t.strip() for t in message['doc_json']['tokens'].split(',') if len(t.strip()) > 1]
        for token in tokens:
            if is_like:
                token_likes[token] = token_likes.get(token, 0) + 1
            else:
                token_dislikes[token] = token_dislikes.get(token, 0) + 1
        
        # Обрабатываем сущности
        for entity in message['doc_json']['entities']:
            if len(entity['text'].strip()) > 1:
                entity_key = f"{entity['text']}_{entity['label']}"
                if is_like:
                    entity_likes[entity_key] = entity_likes.get(entity_key, 0) + 1
                else:
                    entity_dislikes[entity_key] = entity_dislikes.get(entity_key, 0) + 1
    
    # Вычисляем соотношения для токенов
    global token_ratios 
    token_ratios = {}
    all_tokens = set(token_likes.keys()).union(set(token_dislikes.keys()))
    for token in all_tokens:
        likes = token_likes.get(token, 0)
        dislikes = token_dislikes.get(token, 0)
        token_ratios[token] = likes / (dislikes + 1)  # +1 чтобы избежать деления на 0
    
    # Вычисляем соотношения для сущностей
    global entity_ratios
    entity_ratios = {}
    all_entities = set(entity_likes.keys()).union(set(entity_dislikes.keys()))
    for entity in all_entities:
        likes = entity_likes.get(entity, 0)
        dislikes = entity_dislikes.get(entity, 0)
        entity_ratios[entity] = likes / (dislikes + 1)
    logger.info('Weight calculated: %s %s', token_ratios,  entity_ratios)

nlp = spacy.load("ru_core_news_lg")  # Для русского языка

client = TelegramClient('session', api_id, api_hash)

@client.on(events.NewMessage())


async def new_message(event):
    #if str(event.chat_id) !="-1002350259395": #отладка для akekstest
    #    return
    global current_score
    global alpha
    text = event.message.text
    doc_json = await symantic_analyzer(text)
    logger.info('Parsing tokens: %s and entities %s ', doc_json['tokens'], doc_json['entities'])

    if event.is_channel and str(event.chat_id) != notify_chat_id and text:
        score = 0
        for token in [t.strip() for t in doc_json['tokens'].split(',') if t.strip()]:
            logger.info('Parsing: %s', token)
            if token in token_ratios:
                score += token_ratios[token]
        logger.info('Tokens got score: %s',score)
        for entity in doc_json['entities']:
            entity_key = f"{entity['text']}_{entity['label']}"
            if entity_key in entity_ratios:
                score += entity_ratios[entity_key]
        logger.info('Entities got score: %s',score)

        logger.info('Score: %s Current Score: %s', score, current_score)

        if score > current_score or score == 0:
            current_score = alpha * current_score + (1 - alpha) * score
            
            await send_message_with_buttons(notify_chat_id, event, doc_json)

async def symantic_analyzer(text):
    doc = nlp(text)
    doc_json = {
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
    return doc_json


async def send_message_with_buttons(chat_id, event, doc_json):
    text = event.message.text
    # Add link to original message as a separate row
    if event.is_channel and event.chat and event.id:
        channel_username = event.chat.username
        if channel_username:
            message_link = f"https://t.me/{channel_username}/{event.id}"
    else:
        message_link = "Link unavailable"
    #text = f"{text}\n\n({message_link})"
    keyboard = [
        [
            InlineKeyboardButton("👍 Like", callback_data="like"),
            InlineKeyboardButton("👎 Dislike", callback_data="dislike")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    # Get original chat title if available
    chat_title = event.chat.title if event.chat and hasattr(event.chat, 'title') else "Unknown Channel"
    # Add message_link as a clickable hyperlink
    if message_link != "Link unavailable":
        text_with_header = f"<b>{chat_title}</b>\n\n{text}\n\n<a href=\"{message_link}\">Открыть сообщение</a>"
    else:
        text_with_header = f"<b>{chat_title}</b>\n\n{text}"
    sent_message = await bot.send_message(chat_id=chat_id, text=text_with_header, reply_markup=reply_markup, parse_mode="HTML", disable_web_page_preview=True)
    messages_data.append({
        "message_id": sent_message.message_id,
        "chat_id": chat_id,
        "date": event.message.date.isoformat(),
        "text": text,
        "doc_json": doc_json
    })
    #logger.info('Messaages data: %s', messages_data)444

async def button_callback(update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_choice = query.data  # Extract the user's choice from the callback data
    for msg in messages_data:
        if  msg["message_id"] == query.message.message_id:
            msg["user_choice"] = user_choice  # Add user_choice as new JSON element
            #logger.info(f'Updated message data: {msg}')
            break
    filter_data()
    save_messages_data()    
    calculate_like_dislike_ratio()




def setup_application():
    application = Application.builder().token(bot_token).build()
    application.add_handler(CallbackQueryHandler(button_callback))
    return application

async def main():
    load_messages_data()
    filter_data()
    calculate_like_dislike_ratio()
    try:
        await client.start()
        logger.info('Client started successfully')
    except Exception as e:
        logger.error('Failed to start client: %s', e)

    application = setup_application()
    await application.initialize()
    await application.start()
    await application.updater.start_polling()
    await client.run_until_disconnected()

if __name__ == '__main__':
    #asyncio.run(main())
    client.loop.run_until_complete(main())