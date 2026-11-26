import os
import logging
from collections import defaultdict, deque

import httpx
from fastapi import FastAPI, Header, HTTPException, Request, BackgroundTasks
from fastapi.responses import JSONResponse

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("telegram-groq-bot")

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
GROQ_API_KEY = os.environ["GROQ_API_KEY"]
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-20b")
SYSTEM_PROMPT = os.getenv(
    "SYSTEM_PROMPT",
    "Ты полезный AI-ассистент. Отвечай на русском языке, если пользователь не попросил другой язык."
)
MAX_HISTORY_MESSAGES = int(os.getenv("MAX_HISTORY_MESSAGES", "20"))

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
GROQ_API = "https://api.groq.com/openai/v1/chat/completions"

app = FastAPI(title="Telegram Groq AI Bot")

# История хранится в памяти процесса и очищается при перезапуске Render.
histories = defaultdict(lambda: deque(maxlen=MAX_HISTORY_MESSAGES))


async def telegram_call(method: str, payload: dict):
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(f"{TELEGRAM_API}/{method}", json=payload)
        response.raise_for_status()
        data = response.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram API error: {data}")
        return data.get("result")


async def send_message(chat_id: int, text: str):
    chunks = [text[i:i + 4000] for i in range(0, len(text), 4000)] or ["Пустой ответ."]
    for chunk in chunks:
        await telegram_call("sendMessage", {
            "chat_id": chat_id,
            "text": chunk,
            "disable_web_page_preview": True,
        })


async def ask_groq(user_id: int, user_text: str) -> str:
    history = histories[user_id]
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages.extend(list(history))
    messages.append({"role": "user", "content": user_text})

    async with httpx.AsyncClient(timeout=90) as client:
        response = await client.post(
            GROQ_API,
            headers={
                "Authorization": f"Bearer {GROQ_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": GROQ_MODEL,
                "messages": messages,
                "stream": False,
            },
        )
        response.raise_for_status()
        data = response.json()

    answer = data["choices"][0]["message"]["content"].strip()
    history.append({"role": "user", "content": user_text})
    history.append({"role": "assistant", "content": answer})
    return answer


async def handle_update(update: dict):
    message = update.get("message") or update.get("edited_message")
    if not message or not message.get("text"):
        return

    chat_id = message["chat"]["id"]
    user_id = message["from"]["id"]
    text = message["text"].strip()

    if text.startswith("/start"):
        await send_message(
            chat_id,
            "Привет! 👋 Я бесплатный AI-бот на базе модели GPT-OSS через Groq.\n\n"
            "Напиши вопрос — и я отвечу.\n"
            "/clear — очистить историю диалога\n"
            "/help — помощь"
        )
        return

    if text.startswith("/help"):
        await send_message(
            chat_id,
            "Я отвечаю на вопросы и учитываю контекст переписки.\n"
            "Команды:\n/start — начать\n/clear — очистить историю\n/help — помощь"
        )
        return

    if text.startswith("/clear"):
        histories.pop(user_id, None)
        await send_message(chat_id, "История диалога очищена 🧹")
        return

    if text.startswith("/"):
        await send_message(chat_id, "Неизвестная команда. Напиши /help.")
        return

    await telegram_call("sendChatAction", {"chat_id": chat_id, "action": "typing"})
    try:
        answer = await ask_groq(user_id, text)
        await send_message(chat_id, answer)
    except httpx.HTTPStatusError as exc:
        log.exception("Groq API returned an error: %s", exc.response.text[:1000])
        status = exc.response.status_code
        if status == 429:
            msg = "Сейчас достигнут бесплатный лимит запросов Groq. Подожди немного и попробуй снова."
        elif status in (401, 403):
            msg = "Не удалось авторизоваться в Groq. Проверь GROQ_API_KEY в Render."
        else:
            msg = "Groq временно не смог обработать запрос. Попробуй ещё раз чуть позже."
        await send_message(chat_id, msg)
    except Exception:
        log.exception("Unexpected error while processing message")
        await send_message(chat_id, "Произошла ошибка. Попробуй отправить сообщение ещё раз.")


@app.on_event("startup")
async def configure_webhook():
    if not RENDER_EXTERNAL_URL:
        log.warning("RENDER_EXTERNAL_URL is missing; webhook was not configured.")
        return

    payload = {"url": f"{RENDER_EXTERNAL_URL}/telegram/webhook"}
    if WEBHOOK_SECRET:
        payload["secret_token"] = WEBHOOK_SECRET

    try:
        await telegram_call("setWebhook", payload)
        log.info("Telegram webhook configured.")
    except Exception:
        log.exception("Could not configure Telegram webhook")


@app.get("/")
async def home():
    return {"status": "ok", "service": "Telegram Groq AI Bot"}


@app.get("/health")
async def health():
    return {"status": "healthy"}


@app.post("/telegram/webhook")
async def telegram_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
):
    if WEBHOOK_SECRET and x_telegram_bot_api_secret_token != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Invalid webhook secret")

    update = await request.json()
    background_tasks.add_task(handle_update, update)
    return JSONResponse({"ok": True})
