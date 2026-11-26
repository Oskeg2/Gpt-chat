import os
import logging
import asyncio
import base64
import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl
from collections import defaultdict, deque

import httpx
from fastapi import FastAPI, Header, HTTPException, Depends
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel


logging.basicConfig(level=logging.INFO)
log = logging.getLogger("telegram-groq-bot")

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
GROQ_API_KEY = os.environ["GROQ_API_KEY"]
RENDER_EXTERNAL_URL = (os.getenv("WEBHOOK_URL") or os.getenv("RENDER_EXTERNAL_URL", "")).rstrip("/")
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


app.mount("/static", StaticFiles(directory="static"), name="static")


def verify_telegram_init_data(init_data: str) -> int:
    if not init_data:
        raise HTTPException(status_code=401, detail="Открой приложение из Telegram.")
    values = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = values.pop("hash", None)
    if not received_hash:
        raise HTTPException(status_code=401, detail="Не удалось проверить Telegram.")
    try:
        if not values.get("auth_date") or abs(time.time() - int(values["auth_date"])) > 86400:
            raise HTTPException(status_code=401, detail="Сессия устарела. Открой приложение заново.")
    except ValueError:
        raise HTTPException(status_code=401, detail="Некорректная сессия Telegram.")
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(values.items()))
    secret_key = hmac.new(b"WebAppData", TELEGRAM_BOT_TOKEN.encode(), hashlib.sha256).digest()
    calculated = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calculated, received_hash):
        raise HTTPException(status_code=401, detail="Проверка Telegram не пройдена.")
    try:
        return int(json.loads(values["user"])["id"])
    except (KeyError, ValueError, TypeError):
        raise HTTPException(status_code=401, detail="Не удалось определить пользователя.")


def miniapp_user(x_telegram_init_data: str = Header(default="")) -> int:
    return verify_telegram_init_data(x_telegram_init_data)


class MiniChatRequest(BaseModel):
    text: str = ""
    image_base64: str | None = None
    mime_type: str = "image/jpeg"


@app.get("/app")
async def miniapp():
    return FileResponse("static/index.html")


@app.post("/api/miniapp/chat")
async def miniapp_chat(payload: MiniChatRequest, user_id: int = Depends(miniapp_user)):
    text = payload.text.strip()
    if payload.image_base64:
        try:
            image_bytes = base64.b64decode(payload.image_base64, validate=True)
        except Exception:
            raise HTTPException(status_code=400, detail="Не удалось прочитать изображение.")
        if len(image_bytes) > 10 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="Изображение должно быть меньше 10 МБ.")
        answer = await ask_groq_about_image(user_id, text, image_bytes, payload.mime_type)
        model = os.getenv("GROQ_VISION_MODEL", "qwen/qwen3.8-27b")
    else:
        if not text:
            raise HTTPException(status_code=400, detail="Напиши сообщение.")
        answer = await ask_groq(user_id, text)
        model = GROQ_MODEL
    return {"answer": answer, "limits": rate_limits.get(model, {})}


@app.post("/api/miniapp/limits")
async def miniapp_limits(user_id: int = Depends(miniapp_user)):
    return {"limits": rate_limits.get(GROQ_MODEL, {})}


@app.post("/api/miniapp/clear")
async def miniapp_clear(user_id: int = Depends(miniapp_user)):
    histories.pop(user_id, None)
    return {"ok": True}



# История хранится в памяти процесса и очищается при перезапуске Render.
histories = defaultdict(lambda: deque(maxlen=MAX_HISTORY_MESSAGES))
# Последние лимиты из HTTP-заголовков Groq, отдельно для каждой модели.
rate_limits = {}


def save_rate_limits(response: httpx.Response, model: str):
    names = ("limit-requests", "remaining-requests", "reset-requests",
             "limit-tokens", "remaining-tokens", "reset-tokens")
    values = {name: response.headers.get("x-ratelimit-" + name) for name in names}
    if any(value is not None for value in values.values()):
        rate_limits[model] = values


def format_rate_limits(model: str) -> str:
    stats = rate_limits.get(model)
    if not stats:
        return f"Пока нет данных по модели {model}. Отправь любой запрос, а затем снова спроси про лимит."
    lines = [f"📊 Лимиты модели {model} (по последнему ответу API):"]
    if stats.get("remaining-requests") is not None:
        lines.append(f"• Запросов на день: {stats['remaining-requests']} из {stats.get('limit-requests', '?')} осталось")
    if stats.get("reset-requests"):
        lines.append(f"• Сброс дневного лимита: через {stats['reset-requests']}")
    if stats.get("remaining-tokens") is not None:
        lines.append(f"• Токенов на минуту: {stats['remaining-tokens']} из {stats.get('limit-tokens', '?')} осталось")
    if stats.get("reset-tokens"):
        lines.append(f"• Обновление минутного лимита: через {stats['reset-tokens']}")
    lines.append("Это лимиты API для этой модели, а не денежный баланс. Данные обновляются после запросов.")
    return "\n".join(lines)



async def telegram_call(method: str, payload: dict, timeout: float = 30):
    async with httpx.AsyncClient(timeout=timeout) as client:
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
        save_rate_limits(response, vision_model)
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
        save_rate_limits(response, GROQ_MODEL)
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
        await telegram_call("setMyCommands", {
            "commands": [
                {"command": "start", "description": "Запустить бота"},
                {"command": "help", "description": "Что умеет бот"},
                {"command": "clear", "description": "Очистить историю диалога"},
                {"command": "limit", "description": "Показать остаток лимитов API"},
            ]
        })
        await telegram_call("setChatMenuButton", {
            "menu_button": {
                "type": "web_app",
                "text": "Открыть AI",
                "web_app": {"url": "https://telegram-groq-ai-bot.onrender.com/app"},
            }
        })
        await send_message(
            chat_id,
            "Привет! 👋 Я твой AI-помощник.\n\n"
            "Можешь задать вопрос обычным сообщением или отправить фото — "
            "я помогу разобраться с текстом, заданиями и изображениями.\n\n"
            "Доступные команды:\n"
            "/start — запустить бота\n"
            "/help — помощь и возможности\n"
            "/clear — очистить историю диалога\n"
            "/limit — показать остаток лимитов\n\n"
            "Просто напиши, чем помочь! 😊"
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

    if text.startswith("/limit") or any(
        phrase in text.lower()
        for phrase in ("сколько осталось лимита", "сколько лимита осталось", "остаток лимита", "сколько осталось запросов")
    ):
        await send_message(chat_id, format_rate_limits(GROQ_MODEL))
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


polling_task: asyncio.Task | None = None


async def poll_telegram():
    """Получает обновления Telegram через Long Polling."""
    offset = None
    log.info("Telegram long polling started.")
    while True:
        try:
            payload = {"timeout": 25, "allowed_updates": ["message", "edited_message"]}
            if offset is not None:
                payload["offset"] = offset
            updates = await telegram_call("getUpdates", payload, timeout=35)
            for update in updates or []:
                try:
                    await handle_update(update)
                except Exception:
                    log.exception("Failed to process Telegram update %s", update.get("update_id"))
                offset = update["update_id"] + 1
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Telegram polling failed; retrying in 5 seconds.")
            await asyncio.sleep(5)


@app.on_event("startup")
async def start_polling():
    global polling_task
    try:
        # Long Polling нельзя использовать, пока у бота установлен Webhook.
        await telegram_call("deleteWebhook", {"drop_pending_updates": False})
        log.info("Telegram webhook removed; switching to long polling.")
        polling_task = asyncio.create_task(poll_telegram())
    except Exception:
        log.exception("Could not initialize Telegram long polling.")


@app.on_event("shutdown")
async def stop_polling():
    if polling_task:
        polling_task.cancel()
        try:
            await polling_task
        except asyncio.CancelledError:
            pass


@app.get("/")
async def home():
    return {"status": "ok", "service": "Telegram Groq AI Bot"}


@app.get("/health")
async def health():
    return {"status": "healthy"}


