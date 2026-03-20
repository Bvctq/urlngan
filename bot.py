import os, re, html, httpx, logging
from urllib.parse import quote, urlparse, parse_qs, urlencode, urlunparse
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from telegram import Update
from telegram.ext import Application, MessageHandler, CommandHandler, filters, ContextTypes

# ========== CẤU HÌNH ==========
BOT_TOKEN    = os.environ.get("BOT_TOKEN")
API_URL      = os.environ.get("API_URL", "https://s.allvn.top/api.php")
WEBHOOK_URL  = os.environ.get("WEBHOOK_URL")

AFFILIATE_ID = "17350890105"
SUB_ID       = "----CR--"

logging.basicConfig(level=logging.INFO)

# ========== REGEX ==========
# Bắt cả link có và không có https://
URL_REGEX = re.compile(
    r'https?://[^\s,<>"\)\]]+|(?<!\w)[a-zA-Z0-9][a-zA-Z0-9.-]*\.[a-zA-Z]{2,6}/[^\s,<>"\)\]]+',
    re.IGNORECASE
)

SHOPEE_REGEX = re.compile(
    r'(?:https?://)?(?:[a-z0-9.-]*)'
    r'(?:shopee\.vn|shope\.ee|sandeal\.co|hoisansale\.pro|'
    r'nghien\.co|thanhsansale\.online|app\.shopeepay\.vn|s\.5anm\.net|shp\.ee)'
    r'[^\s\n\r,<>"]*',
    re.IGNORECASE
)

# ========== HELPERS ==========
async def get_final_url(url: str) -> str:
    """Unshorten / follow redirects để lấy link gốc."""
    if not re.match(r'https?://', url, re.I):
        url = "https://" + url
    try:
        async with httpx.AsyncClient(
            follow_redirects=True, timeout=10,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        ) as c:
            r = await c.get(url)
            return str(r.url)
    except Exception:
        return url

async def shorten(long_url: str) -> str:
    """Gọi api.php để rút gọn link."""
    if not re.match(r'https?://', long_url, re.I):
        long_url = "https://" + long_url
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.post(API_URL, data={"long_url": long_url})
        d = r.json()
        if "short_url" in d:
            return d["short_url"]
        raise ValueError(d.get("error", "Lỗi API"))

def build_aff_link(real_url: str) -> str:
    """Tạo link affiliate Shopee."""
    if "an_redir" in real_url and "affiliate_id" in real_url:
        parsed = urlparse(real_url)
        qs = parse_qs(parsed.query, keep_blank_values=True)
        qs["affiliate_id"] = [AFFILIATE_ID]
        qs["sub_id"] = [SUB_ID]
        new_query = urlencode({k: v[0] for k, v in qs.items()})
        return urlunparse(parsed._replace(query=new_query))
    enc = quote(real_url, safe='')
    return f"https://s.shopee.vn/an_redir?origin_link={enc}&affiliate_id={AFFILIATE_ID}&sub_id={SUB_ID}"

# ========== XỬ LÝ TEXT ==========
async def process_rut(text: str) -> str:
    """Tìm tất cả URL → tự thêm https nếu thiếu → rút gọn, giữ nguyên định dạng."""
    result = text
    matches = URL_REGEX.findall(text)
    seen = {}

    for raw in matches:
        clean = raw.rstrip(".,!? ")
        if not clean:
            continue

        # Dùng clean làm key để tránh rút gọn trùng
        if clean in seen:
            result = result.replace(raw, seen[clean], 1)
            continue

        # Tự thêm https:// nếu thiếu
        url_to_shorten = clean
        if not re.match(r'https?://', clean, re.I):
            url_to_shorten = "https://" + clean

        try:
            short = await shorten(url_to_shorten)
            seen[clean] = short
            result = result.replace(raw, short, 1)
        except Exception:
            pass

    return result

async def process_shopee_aff(text: str) -> str:
    """Shopee: unshorten → thêm affiliate → rút gọn, giữ nguyên định dạng."""
    result = text
    matches = list(dict.fromkeys(SHOPEE_REGEX.findall(text)))

    for raw in matches:
        clean = raw.rstrip(".,!? ")
        url = clean if re.match(r'https?://', clean, re.I) else "https://" + clean

        real_url = await get_final_url(url)
        real_url = real_url.replace("thanhsansale.com", "").replace("thanhsansale", "")

        if re.search(r'shopee\.vn|shope\.ee', real_url, re.I):
            aff_link = build_aff_link(real_url)
            try:
                short = await shorten(aff_link)
                result = result.replace(raw, short, 1)
            except Exception:
                pass

    return result

# ========== SEND HELPER ==========
async def send_result(update: Update, result: str):
    """Gửi kết quả, giữ nguyên xuống dòng."""
    escaped = html.escape(result)
    try:
        await update.message.reply_text(escaped, parse_mode="HTML")
    except Exception:
        await update.message.reply_text(result)

# ========== COMMAND HANDLERS ==========
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 Bot Rút Gọn Link\n\n"
        "📌 Gửi link/đoạn văn Shopee → tự động thêm affiliate + rút gọn\n"
        "✂️ /rut [link hoặc đoạn văn] → chỉ rút gọn, không thêm affiliate\n\n"
        "💡 Tip: /rut có thể reply vào tin nhắn bất kỳ!"
    )

async def cmd_rut(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Lấy toàn bộ text sau /rut, giữ nguyên newline
    full_text = update.message.text or ""
    text = re.sub(r'^/rut\s*', '', full_text, flags=re.IGNORECASE).strip()

    # Nếu không có text sau lệnh → thử lấy từ reply
    if not text:
        if update.message.reply_to_message and update.message.reply_to_message.text:
            text = update.message.reply_to_message.text
        else:
            await update.message.reply_text(
                "Cách dùng:\n"
                "• /rut https://link-dai.com\n"
                "• Paste cả đoạn văn sau lệnh /rut\n"
                "• Hoặc reply vào tin nhắn rồi gõ /rut"
            )
            return

    if not URL_REGEX.search(text):
        await update.message.reply_text("⚠️ Không tìm thấy link nào.")
        return

    result = await process_rut(text)
    await send_result(update, result)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Tin nhắn thường (không phải lệnh) → tự động xử lý Shopee affiliate."""
    text = update.message.text or ""

    if not SHOPEE_REGEX.search(text):
        return  # Không có link Shopee → bỏ qua

    await update.message.reply_text("⏳ Đang xử lý...")
    result = await process_shopee_aff(text)
    await send_result(update, result)

# ========== SETUP BOT ==========
ptb_app = Application.builder().token(BOT_TOKEN).updater(None).build()
ptb_app.add_handler(CommandHandler("start", cmd_start))
ptb_app.add_handler(CommandHandler("rut", cmd_rut))
ptb_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

# ========== FASTAPI ==========
@asynccontextmanager
async def lifespan(app: FastAPI):
    await ptb_app.initialize()
    await ptb_app.bot.set_webhook(WEBHOOK_URL)
    await ptb_app.start()
    print(f"✅ Webhook: {WEBHOOK_URL}")
    yield
    await ptb_app.stop()

fastapi_app = FastAPI(lifespan=lifespan)

@fastapi_app.post("/webhook")
async def webhook(request: Request):
    data = await request.json()
    update = Update.de_json(data, ptb_app.bot)
    await ptb_app.process_update(update)
    return {"ok": True}

@fastapi_app.get("/")
async def root():
    return {"status": "🤖 Bot đang chạy"}
