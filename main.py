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


async def download_telegram_file(file_id: str) -> tuple[bytes, str]:
    """Скачивает фото из Telegram и возвращает байты и MIME-тип."""
    file_info = await telegram_call("getFile", {"file_id": file_id})
    file_path = file_info["file_path"]
    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.get(f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{file_path}")
        response.raise_for_status()
        content_type = response.headers.get("content-type", "image/jpeg").split(";")[0]
        return response.content, content_type


async def ask_groq_about_image(user_id: int, user_text: str, image_bytes: bytes, mime_type: str) -> str:
    """Отправляет фотографию и вопрос vision-модели Groq."""
    import base64

    vision_model = os.getenv("GROQ_VISION_MODEL", "qwen/qwen3.8-27b")
    image_b64 = base64.b64encode(image_bytes).decode("ascii")
    prompt = user_text or (
        "Подробно рассмотри изображение. Если на нём задание, реши его "
        "пошагово и объясни ответ на русском языке."
    )

    history = histories[user_id]
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages.extend(list(history))
    messages.append({
        "role": "user",
        "content": [
            {"type": "text", "text": prompt},
            {
                "type": "image_url",
                "image_url": {"url": f"data:{mime_type};base64,{image_b64}"},
            },
        ],
    })

    async with httpx.AsyncClient(timeout=120) as client:
        response = await client.post(
            GROQ_API,
            headers={
                "Authorization": f"Bearer {GROQ_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": vision_model,
                "messages": messages,
                "stream": False,
                "max_completion_tokens": 2048,
            },
        )
        response.raise_for_status()
        data = response.json()

    answer = data["choices"][0]["message"]["content"].strip()
    history.append({
        "role": "user",
        "content": f"[Пользователь отправил изображение] {prompt}",
    })
    history.append({"role": "assistant", "content": answer})
    return answer


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
    if not message:
        return

    chat_id = message["chat"]["id"]
    user_id = message["from"]["id"]
    text = (message.get("text") or message.get("caption") or "").strip()
    photos = message.get("photo") or []
    document = message.get("document") or {}
    document_is_image = (document.get("mime_type") or "").startswith("image/")

    if not text and not photos and not document_is_image:
        return

    if text.startswith("/start"):
        await send_message(
            chat_id,
            "Привет! 👋 Я AI-ассистент на Groq.\n\n"
            "Можешь писать вопросы и отправлять фотографии с заданиями — "
            "я прочитаю их и постараюсь решить пошагово.\n\n"
            "/clear — очистить историю диалога\n"
            "/help — помощь"
        )
        return

    if text.startswith("/help"):
        await send_message(
            chat_id,
            "Я отвечаю на вопросы и умею анализировать фотографии: "
            "читать текст, разбирать схемы и решать задания.\n"
            "Команды:\n/start — начать\n/clear — очистить историю\n/help — помощь"
        )
        return

    if text.startswith("/clear"):
        histories.pop(user_id, None)
        await send_message(chat_id, "История диалога очищена 🧹")
        return

    if text.startswith("/") and not photos and not document_is_image:
        await send_message(chat_id, "Неизвестная команда. Напиши /help.")
        return

    await telegram_call("sendChatAction", {"chat_id": chat_id, "action": "typing"})
    try:
        if photos or document_is_image:
            file_id = photos[-1]["file_id"] if photos else document["file_id"]
            image_bytes, mime_type = await download_telegram_file(file_id)
            if len(image_bytes) > 20 * 1024 * 1024:
                await send_message(chat_id, "Фото слишком большое. Отправь изображение размером до 20 МБ.")
                return
            answer = await ask_groq_about_image(user_id, text, image_bytes, mime_type)
        else:
            answer = await ask_groq(user_id, text)
        await send_message(chat_id, answer)
    except httpx.HTTPStatusError as exc:
        log.exception("AI API returned an error: %s", exc.response.text[:1500])
        status = exc.response.status_code
        if status == 429:
            msg = "Достигнут лимит запросов Groq. Подожди немного и попробуй снова."
        elif status in (401, 403):
            msg = "Не удалось авторизоваться в Groq. Проверь GROQ_API_KEY в Render."
        elif status == 413:
            msg = "Изображение слишком большое для модели. Отправь фото меньшего размера."
        else:
            msg = "Не удалось обработать запрос или фото. Попробуй другое изображение чуть позже."
        await send_message(chat_id, msg)
    except Exception:
        log.exception("Unexpected error while processing message")
        await send_message(chat_id, "Произошла ошибка при обработке. Попробуй ещё раз.")


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
