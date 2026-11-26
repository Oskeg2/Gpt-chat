import os
import logging
import asyncio
import base64
import hashlib
import hmac
import json
import time

import psycopg2
from urllib.parse import parse_qsl
from collections import defaultdict, deque

import httpx
from fastapi import FastAPI, Header, HTTPException, Depends
from fastapi.responses import FileResponse, StreamingResponse
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
DAILY_TOKEN_LIMIT = int(os.getenv("DAILY_TOKEN_LIMIT", "8000"))
ADMIN_IDS = {int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()}
DATABASE_URL = os.environ["DATABASE_URL"]

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
GROQ_API = "https://api.groq.com/openai/v1/chat/completions"

app = FastAPI(title="Telegram Groq AI Bot")


def init_usage_db():
    with psycopg2.connect(DATABASE_URL) as db:
        with db.cursor() as cur:
            cur.execute("SET TIME ZONE 'UTC'")
            cur.execute("""CREATE TABLE IF NOT EXISTS daily_usage (
                user_id BIGINT NOT NULL,
                usage_date DATE NOT NULL DEFAULT CURRENT_DATE,
                request_count INTEGER NOT NULL DEFAULT 0,
                total_tokens INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (user_id, usage_date)
            )""")
            cur.execute("ALTER TABLE daily_usage ADD COLUMN IF NOT EXISTS total_tokens INTEGER NOT NULL DEFAULT 0")
            cur.execute("""CREATE TABLE IF NOT EXISTS user_accounts (
                user_id BIGINT PRIMARY KEY,
                bonus_tokens BIGINT NOT NULL DEFAULT 0,
                daily_limit INTEGER NOT NULL DEFAULT 8000,
                is_blocked BOOLEAN NOT NULL DEFAULT FALSE,
                is_unlimited BOOLEAN NOT NULL DEFAULT FALSE,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )""")
            cur.execute("""CREATE TABLE IF NOT EXISTS bot_users (
                user_id BIGINT PRIMARY KEY, username TEXT, first_name TEXT,
                last_seen TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )""")
            cur.execute("""CREATE TABLE IF NOT EXISTS admin_logs (
                id BIGSERIAL PRIMARY KEY, admin_id BIGINT NOT NULL, target_id BIGINT,
                action TEXT NOT NULL, amount BIGINT, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )""")


def get_daily_usage(user_id: int) -> int:
    with psycopg2.connect(DATABASE_URL) as db:
        with db.cursor() as cur:
            cur.execute("SET TIME ZONE 'UTC'")
            cur.execute(
                "SELECT total_tokens FROM daily_usage WHERE user_id = %s AND usage_date = CURRENT_DATE",
                (user_id,),
            )
            row = cur.fetchone()
    return int(row[0]) if row else 0


def account_info(user_id: int) -> dict:
    with psycopg2.connect(DATABASE_URL) as db:
        with db.cursor() as cur:
            cur.execute("INSERT INTO user_accounts (user_id) VALUES (%s) ON CONFLICT DO NOTHING", (user_id,))
            cur.execute("SELECT bonus_tokens, daily_limit, is_blocked, is_unlimited FROM user_accounts WHERE user_id=%s", (user_id,))
            row = cur.fetchone()
    return {"bonus": int(row[0]), "limit": int(row[1]), "blocked": bool(row[2]), "unlimited": bool(row[3])}

def can_make_request(user_id: int) -> bool:
    a = account_info(user_id)
    return not a["blocked"] and (a["unlimited"] or get_daily_usage(user_id) < a["limit"] + a["bonus"])

def remember_user(user_id: int, message: dict):
    u = message.get("from", {})
    with psycopg2.connect(DATABASE_URL) as db:
        with db.cursor() as cur:
            cur.execute("""INSERT INTO bot_users (user_id, username, first_name) VALUES (%s,%s,%s)
                ON CONFLICT (user_id) DO UPDATE SET username=EXCLUDED.username,
                first_name=EXCLUDED.first_name,last_seen=NOW()""",
                (user_id, u.get("username"), u.get("first_name")))

def admin_action(admin_id: int, target_id: int | None, action: str, amount: int | None = None):
    with psycopg2.connect(DATABASE_URL) as db:
        with db.cursor() as cur:
            cur.execute("INSERT INTO admin_logs (admin_id,target_id,action,amount) VALUES (%s,%s,%s,%s)",
                        (admin_id,target_id,action,amount))

def require_admin(user_id: int):
    if user_id not in ADMIN_IDS:
        raise HTTPException(status_code=403, detail="Нет доступа.")

def admin_change(admin_id: int, target_id: int, action: str, amount: int | None = None):
    if target_id <= 0: raise ValueError("Некорректный Telegram ID")
    if action in ("give", "take") and (amount is None or amount <= 0 or amount > 1000000000):
        raise ValueError("Количество должно быть от 1 до 1 000 000 000")
    with psycopg2.connect(DATABASE_URL) as db:
        with db.cursor() as cur:
            cur.execute("INSERT INTO user_accounts (user_id) VALUES (%s) ON CONFLICT DO NOTHING",(target_id,))
            if action == "give":
                cur.execute("UPDATE user_accounts SET bonus_tokens=bonus_tokens+%s,updated_at=NOW() WHERE user_id=%s",(amount,target_id))
            elif action == "take":
                cur.execute("UPDATE user_accounts SET bonus_tokens=GREATEST(0,bonus_tokens-%s),updated_at=NOW() WHERE user_id=%s",(amount,target_id))
            elif action == "ban":
                cur.execute("UPDATE user_accounts SET is_blocked=TRUE,updated_at=NOW() WHERE user_id=%s",(target_id,))
            elif action == "unban":
                cur.execute("UPDATE user_accounts SET is_blocked=FALSE,updated_at=NOW() WHERE user_id=%s",(target_id,))
            elif action == "unlimited":
                cur.execute("UPDATE user_accounts SET is_unlimited=%s,updated_at=NOW() WHERE user_id=%s",(bool(amount),target_id))
            elif action == "limit":
                cur.execute("UPDATE user_accounts SET daily_limit=%s,updated_at=NOW() WHERE user_id=%s",(amount,target_id))
    admin_action(admin_id,target_id,action,amount)

def daily_usage_payload(user_id: int) -> dict:
    used = get_daily_usage(user_id)
    a = account_info(user_id)
    remaining = -1 if a["unlimited"] else max(0, a["limit"] - used) + a["bonus"]
    return {"limit": a["limit"], "used": used, "bonus": a["bonus"], "remaining": remaining,
            "blocked": a["blocked"], "unlimited": a["unlimited"], "unit": "tokens", "resets_at": "00:00 UTC"}


def add_daily_tokens(user_id: int, tokens: int) -> int:
    tokens = max(0, int(tokens or 0))
    with psycopg2.connect(DATABASE_URL) as db:
        with db.cursor() as cur:
            cur.execute("SET TIME ZONE 'UTC'")
            cur.execute(
                """INSERT INTO daily_usage (user_id, usage_date, request_count, total_tokens)
                   VALUES (%s, CURRENT_DATE, 1, %s)
                   ON CONFLICT (user_id, usage_date)
                   DO UPDATE SET request_count = daily_usage.request_count + 1,
                                 total_tokens = daily_usage.total_tokens + EXCLUDED.total_tokens
                   RETURNING total_tokens""",
                (user_id, tokens),
            )
            row = cur.fetchone()
    total = int(row[0])
    a = account_info(user_id)
    if not a["unlimited"]:
        daily_left_before = max(0, a["limit"] - (total - tokens))
        bonus_spent = max(0, tokens - daily_left_before)
        if bonus_spent:
            with psycopg2.connect(DATABASE_URL) as db2:
                with db2.cursor() as cur2:
                    cur2.execute("UPDATE user_accounts SET bonus_tokens=GREATEST(0,bonus_tokens-%s),updated_at=NOW() WHERE user_id=%s",(bonus_spent,user_id))
    return total


init_usage_db()


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
    answer_mode: str = "short"



class AdminActionRequest(BaseModel):
    user_id: int
    action: str
    amount: int | None = None

@app.post("/api/admin/overview")
async def admin_overview(user_id: int = Depends(miniapp_user)):
    require_admin(user_id)
    with psycopg2.connect(DATABASE_URL) as db:
        with db.cursor() as cur:
            cur.execute("""SELECT b.user_id,b.username,b.first_name,
                COALESCE(d.total_tokens,0),a.bonus_tokens,a.daily_limit,a.is_blocked,a.is_unlimited
                FROM bot_users b LEFT JOIN user_accounts a ON a.user_id=b.user_id
                LEFT JOIN daily_usage d ON d.user_id=b.user_id AND d.usage_date=CURRENT_DATE
                ORDER BY b.last_seen DESC LIMIT 100""")
            rows=cur.fetchall()
    return {"users":[{"user_id":r[0],"username":r[1],"first_name":r[2],"used":r[3],
        "bonus":r[4] or 0,"limit":r[5] or DAILY_TOKEN_LIMIT,"blocked":bool(r[6]),"unlimited":bool(r[7])} for r in rows]}

@app.post("/api/admin/action")
async def admin_api_action(payload: AdminActionRequest, user_id: int = Depends(miniapp_user)):
    require_admin(user_id)
    if payload.action not in ("give","take","ban","unban","limit","unlimited"):
        raise HTTPException(status_code=400, detail="Неизвестное действие.")
    try:
        admin_change(user_id,payload.user_id,payload.action,payload.amount)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok":True,"account":account_info(payload.user_id)}

@app.get("/app")
async def miniapp():
    return FileResponse("static/index.html")


@app.post("/api/miniapp/chat")
async def miniapp_chat(payload: MiniChatRequest, user_id: int = Depends(miniapp_user)):
    if not can_make_request(user_id):
        a = account_info(user_id)
        raise HTTPException(status_code=403 if a["blocked"] else 429, detail="Доступ заблокирован." if a["blocked"] else "Лимит токенов исчерпан.")
    text = payload.text.strip()
    if payload.image_base64:
        try:
            image_bytes = base64.b64decode(payload.image_base64, validate=True)
        except Exception:
            raise HTTPException(status_code=400, detail="Не удалось прочитать изображение.")
        if len(image_bytes) > 10 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="Изображение должно быть меньше 10 МБ.")
        answer, used_tokens = await ask_groq_about_image(user_id, text, image_bytes, payload.mime_type, payload.answer_mode)
        add_daily_tokens(user_id, used_tokens)
        model = os.getenv("GROQ_VISION_MODEL", "qwen/qwen3.8-27b")
        return {"answer": answer, "limits": rate_limits.get(model, {}), "daily_usage": daily_usage_payload(user_id), "is_admin": user_id in ADMIN_IDS}
    if not text:
        raise HTTPException(status_code=400, detail="Напиши сообщение.")

    async def stream_answer():
        history = histories[user_id]
        style_prompt = ("Отвечай кратко и по делу: сначала прямой ответ, затем только необходимое пояснение." if payload.answer_mode != "detailed" else "Дай подробный, структурированный ответ с объяснением шагов.")
        messages = [{"role": "system", "content": SYSTEM_PROMPT + "\n\n" + style_prompt}]
        messages.extend(list(history)[-8:])
        messages.append({"role": "user", "content": text})
        answer_parts = []
        used_tokens = 0
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(90.0, connect=10.0)) as client:
                async with client.stream("POST", GROQ_API, headers={
                    "Authorization": f"Bearer {GROQ_API_KEY}",
                    "Content-Type": "application/json",
                }, json={
                    "model": GROQ_MODEL,
                    "messages": messages,
                    "stream": True,
                    "stream_options": {"include_usage": True},
                    "max_completion_tokens": 1024,
                }) as response:
                    response.raise_for_status()
                    save_rate_limits(response, GROQ_MODEL)
                    async for line in response.aiter_lines():
                        if not line.startswith("data: "):
                            continue
                        raw = line[6:]
                        if raw == "[DONE]":
                            break
                        try:
                            chunk = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        usage = chunk.get("usage")
                        if usage:
                            used_tokens = int(usage.get("total_tokens", 0))
                        choices = chunk.get("choices") or []
                        if choices:
                            delta = choices[0].get("delta", {}).get("content")
                            if delta:
                                answer_parts.append(delta)
                                yield "data: " + json.dumps({"token": delta}, ensure_ascii=False) + "\n\n"
            answer = "".join(answer_parts).strip()
            if answer:
                history.append({"role": "user", "content": text})
                history.append({"role": "assistant", "content": answer})
            if used_tokens:
                add_daily_tokens(user_id, used_tokens)
            meta = {"done": True, "limits": rate_limits.get(GROQ_MODEL, {}), "daily_usage": daily_usage_payload(user_id), "is_admin": user_id in ADMIN_IDS}
            yield "data: " + json.dumps(meta, ensure_ascii=False) + "\n\n"
        except httpx.HTTPStatusError as exc:
            detail = exc.response.text[:1200]
            log.error("Groq streaming HTTP %s: %s", exc.response.status_code, detail)
            yield "data: " + json.dumps({"error": f"Groq HTTP {exc.response.status_code}: {detail}"}, ensure_ascii=False) + "\n\n"
        except Exception as exc:
            log.exception("Mini App streaming request failed")
            yield "data: " + json.dumps({"error": f"Ошибка потока: {type(exc).__name__}: {str(exc)[:500]}"}, ensure_ascii=False) + "\n\n"

    return StreamingResponse(stream_answer(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    })


@app.post("/api/miniapp/limits")
async def miniapp_limits(user_id: int = Depends(miniapp_user)):
    return {"limits": rate_limits.get(GROQ_MODEL, {}), "daily_usage": daily_usage_payload(user_id), "is_admin": user_id in ADMIN_IDS}


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


async def ask_groq_about_image(user_id: int, user_text: str, image_bytes: bytes, mime_type: str, answer_mode: str = "short") -> tuple[str, int]:
    """Отправляет фотографию и вопрос vision-модели Groq."""
    import base64

    vision_model = os.getenv("GROQ_VISION_MODEL", "qwen/qwen3.8-27b")
    image_b64 = base64.b64encode(image_bytes).decode("ascii")
    prompt = user_text or (
        "Подробно рассмотри изображение. Если на нём задание, реши его "
        "пошагово и объясни ответ на русском языке."
    )

    history = histories[user_id]
    style_prompt = ("Отвечай кратко и по делу: сначала прямой ответ, затем только необходимое пояснение. Не теряй важные условия и точность." if answer_mode != "detailed" else "Дай подробный, структурированный ответ с объяснением шагов, примерами при необходимости и важными оговорками.")
    messages = [{"role": "system", "content": SYSTEM_PROMPT + "\\n\\n" + style_prompt}]
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
    used_tokens = int((data.get("usage") or {}).get("total_tokens", 0))
    history.append({
        "role": "user",
        "content": f"[Пользователь отправил изображение] {prompt}",
    })
    history.append({"role": "assistant", "content": answer})
    return answer, used_tokens


async def ask_groq(user_id: int, user_text: str, answer_mode: str = "short") -> tuple[str, int]:
    history = histories[user_id]
    style_prompt = ("Отвечай кратко и по делу: сначала прямой ответ, затем только необходимое пояснение. Не теряй важные условия и точность." if answer_mode != "detailed" else "Дай подробный, структурированный ответ с объяснением шагов, примерами при необходимости и важными оговорками.")
    messages = [{"role": "system", "content": SYSTEM_PROMPT + "\\n\\n" + style_prompt}]
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
    used_tokens = int((data.get("usage") or {}).get("total_tokens", 0))
    history.append({"role": "user", "content": user_text})
    history.append({"role": "assistant", "content": answer})
    return answer, used_tokens


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

    # Показываем «печатает» до любых синхронных запросов к PostgreSQL.
    if not text.startswith("/"):
        try:
            await telegram_call("sendChatAction", {"chat_id": chat_id, "action": "typing"})
        except Exception:
            log.exception("Could not send initial typing status")

    remember_user(user_id, message)

    if text.startswith("/") and user_id in ADMIN_IDS:
        parts = text.split()
        cmd = parts[0].split("@")[0].lower()
        try:
            if cmd == "/admin":
                await send_message(chat_id, "Админ-команды:\n/users — последние пользователи\n/user ID — карточка\n/give ID TOKENS\n/take ID TOKENS\n/ban ID\n/unban ID\n/setlimit ID TOKENS\n/unlimited ID on|off")
                return
            if cmd == "/users":
                with psycopg2.connect(DATABASE_URL) as db:
                    with db.cursor() as cur:
                        cur.execute("SELECT user_id,COALESCE(username,''),COALESCE(first_name,'') FROM bot_users ORDER BY last_seen DESC LIMIT 30")
                        rows=cur.fetchall()
                await send_message(chat_id, "Последние пользователи:\n" + ("\n".join(f"{r[0]} · {r[2]} @{r[1]}" for r in rows) or "Пока пусто"))
                return
            if cmd == "/user" and len(parts)==2:
                target=int(parts[1]); a=account_info(target); used=get_daily_usage(target)
                await send_message(chat_id,f"ID: {target}\nСегодня: {used}/{a['limit']}\nБонус: {a['bonus']}\nЗаблокирован: {a['blocked']}\nБезлимит: {a['unlimited']}")
                return
            if cmd in ("/give","/take","/setlimit") and len(parts)==3:
                target, amount=int(parts[1]),int(parts[2])
                admin_change(user_id,target,{"give":"give","take":"take","setlimit":"limit"}[cmd[1:]],amount)
                await send_message(chat_id,"Готово.")
                return
            if cmd in ("/ban","/unban") and len(parts)==2:
                admin_change(user_id,int(parts[1]),cmd[1:])
                await send_message(chat_id,"Готово.")
                return
            if cmd == "/unlimited" and len(parts)==3 and parts[2].lower() in ("on","off"):
                admin_change(user_id,int(parts[1]),"unlimited",1 if parts[2].lower()=="on" else 0)
                await send_message(chat_id,"Готово.")
                return
        except Exception as exc:
            await send_message(chat_id,f"Ошибка команды: {exc}")
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
        usage = daily_usage_payload(user_id)
        await send_message(chat_id,
            f"🪙 Твои токены за сегодня: {usage['used']} / {usage['limit']}\\n"
            f"Осталось: {usage['remaining']} токенов\\n"
            f"Сброс: 00:00 UTC\\n\\n" + format_rate_limits(GROQ_MODEL))
        return

    if text.startswith("/") and not photos and not document_is_image:
        await send_message(chat_id, "Неизвестная команда. Напиши /help.")
        return

    if not can_make_request(user_id):
        if account_info(user_id)["blocked"]:
            await send_message(chat_id, "Твой доступ к боту заблокирован администратором.")
            return
        await send_message(chat_id, f"Дневной лимит {DAILY_TOKEN_LIMIT} токенов исчерпан. Он обновится в 00:00 UTC. 🌙")
        return

    try:
        if photos or document_is_image:
            file_id = photos[-1]["file_id"] if photos else document["file_id"]
            image_bytes, mime_type = await download_telegram_file(file_id)
            if len(image_bytes) > 20 * 1024 * 1024:
                await send_message(chat_id, "Фото слишком большое. Отправь изображение размером до 20 МБ.")
                return
            answer, used_tokens = await ask_groq_about_image(user_id, text, image_bytes, mime_type)
        else:
            answer, used_tokens = await ask_groq(user_id, text)
        add_daily_tokens(user_id, used_tokens)
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


