# Telegram Feed Bot

A personal Telegram news-feed filter. It reads dozens of subscribed channels on your behalf,
scores each post against your interests, deduplicates near-identical news across channels, and
forwards only what's actually worth reading into a private chat — with inline voting to keep
tuning itself over time.

## Why

Following 50-100 Telegram channels means endless noise: reposts of the same event from five
sources, native ads disguised as news, and a feed that never ends. This bot sits between your
channels and your attention, doing the filtering a human would do manually, except all day.

## How it works

```
subscribed channels (userbot)
        │
        ▼
 relay channel ("HOP1")  ── Telethon forwards raw posts here to avoid polluting
        │                    the unread counters on the original channels
        ▼
   analysis pipeline
   ├─ LLM pass (DeepSeek): extract concepts, topics, is_ad flag, summary
   ├─ interest scoring: match concepts against a learned interest matrix
   ├─ dedup layer 1: embedding cosine similarity on canonical summaries
   ├─ dedup layer 2: perceptual hash on media (catches re-uploaded images/video)
   └─ native-ad / giveaway detection (structural + prompt-based)
        │
        ▼
 private chat with the bot ("HOP2") ── only non-duplicate, non-ad, scored-relevant
        │                               posts arrive here, with inline vote buttons
        ▼
   your 👍/👎 on individual topics feeds back into the interest matrix
```

**Two-hop design:** the userbot forwards everything to an intermediate relay channel first, then
the bot re-forwards the filtered result to a private chat. This keeps the unread/notification
noise out of the real source channels while still letting the pipeline inspect every message.

### Pipeline stages

1. **Ingestion** (`bot.py`) — Telethon userbot listens to subscribed channels, handles grouped
   media (albums) with a debounce buffer so multi-photo posts stay together.
2. **Analysis** (`analyzer.py`) — sends the post to an LLM (DeepSeek by default, Timeweb Cloud AI
   as a drop-in alternative) which extracts: topic concepts, a canonical summary (for dedup), and
   an `is_ad` flag that also catches native/sponsored content dressed up as news.
3. **Scoring & dedup** (`pipeline.py`) — concepts are scored against a per-user interest matrix;
   candidate duplicates within a rolling time window are compared via embedding cosine similarity
   on the summary (`embeddings.py`) and, for media posts, perceptual hashing (`media_hash.py`).
   Native Telegram giveaways are filtered deterministically (structural detection, not prompt-based).
4. **Delivery & feedback** (`handlers.py`) — forwards survivors to a private chat with per-topic
   vote buttons; votes adjust interest weights, and `/stats` / `/channels` give visibility and
   per-channel on/off control.
5. **Storage** (`database.py`) — SQLite with scheduled retention cleanup (embeddings/messages
   expire after the dedup window is no longer relevant).

## Stack

Python · Telethon (MTProto userbot) · python-telegram-bot (Bot API) · SQLite · DeepSeek /
OpenAI-compatible LLM APIs · Docker

## Running it

```bash
cp .env.example .env     # fill in your own API IDs/tokens
docker compose up -d --build
```

Required `.env` values (see `.env.example`):

| Variable | Purpose |
|---|---|
| `TG_API_ID` / `TG_API_HASH` | Telegram API credentials (my.telegram.org) for the userbot |
| `BOT_TOKEN` | Bot API token from @BotFather |
| `NOTIFY_CHAT_ID` | Relay channel the userbot forwards into (HOP1) |
| `DM_CHAT_ID` | Private chat where filtered posts + voting arrive (HOP2) |
| `ALLOWED_USER_ID` | Your Telegram user ID — the only one allowed to drive the bot |
| `DEEPSEEK_API_KEY` | LLM key for analysis (or configure `AI_PROVIDER=timeweb`) |
| `EMBEDDING_API_KEY` | OpenAI key for dedup embeddings (optional, falls back to no semantic dedup) |

First run needs an interactive login to create the userbot session — see `init_session.py`.

## Status

Actively used and iterated on daily. See `SPEC.md` for the full design rationale, calibration
notes, and the history of tuning decisions (dedup thresholds, ad-detection prompt tuning, etc).

## Not included

This repo intentionally does not include: the live SQLite database, Telethon session files, or
any `.env` — all covered by `.gitignore`. Nothing in the committed code hardcodes a token, chat
ID, or API key; everything sensitive is read from environment variables.
