import os, re, httpx, logging
from fastapi import FastAPI, Request
from telegram import Update, Bot
from telegram.ext import Application, MessageHandler, filters, ContextTypes

# ==================== CẤU HÌNH ====================
BOT_TOKEN  = os.environ.get("BOT_TOKEN")          # Set trong Render Environment
API_URL    = os.environ.get("API_URL", "https://s.allvn.top/api.php")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL")       # https://ten-app.onrender.com/webhook
PORT       = int(os.environ.get("PORT", 8000))
# ===================================================

logging.basicConfig(level=logging.INFO)

URL_REGEX = re.compile(
    r'(https?://[^\s]+|[a-zA-Z0-9.-]+\.[a-zA-Z]{2,6}/[^\s]+)',
    re.IGNORECASE
)

# Khởi tạo app Telegram
ptb_app = Application.builder().token(BOT_TOKEN).updater(None).build()

async def shorten(long_url: str) -> str:
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(API_URL, data={"long_url": long_url})
        result = resp.json()
        if "short_url" in result:
            return result["short_url"]
        raise ValueError(result.get("error", "Lỗi không xác định"))

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or ""
    urls = URL_REGEX.findall(text)

    if not urls:
        await update.message.reply_text("⚠️ Không tìm thấy link nào trong tin nhắn.")
        return

    lines = []
    for url in urls:
        clean = url.rstrip(".,")
        try:
            short = await shorten(clean)
            lines.append(f"🔗 `{short}`")
        except Exception as e:
            lines.append(f"❌ Lỗi: {e}")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

ptb_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

# ==================== FASTAPI ====================
fastapi_app = FastAPI()

@fastapi_app.on_event("startup")
async def startup():
    await ptb_app.initialize()
    await ptb_app.bot.set_webhook(WEBHOOK_URL)
    await ptb_app.start()
    print(f"✅ Webhook đã đăng ký: {WEBHOOK_URL}")

@fastapi_app.on_event("shutdown")
async def shutdown():
    await ptb_app.stop()

@fastapi_app.post("/webhook")
async def webhook(request: Request):
    data = await request.json()
    update = Update.de_json(data, ptb_app.bot)
    await ptb_app.process_update(update)
    return {"ok": True}

@fastapi_app.get("/")
async def root():
    return {"status": "Bot đang chạy 🤖"}

# ==================== CHẠY ====================
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(fastapi_app, host="0.0.0.0", port=PORT)
```

**`requirements.txt`**
```
python-telegram-bot==21.6
fastapi==0.111.0
uvicorn==0.30.1
httpx==0.27.0
