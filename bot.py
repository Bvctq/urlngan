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
LAZADA_AFF   = "https://c.lazada.vn/t/c.YParqP?url="

logging.basicConfig(level=logging.INFO)

# ========== REGEX ==========
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

# Link rút gọn Lazada (cần unshorten trước)
LAZADA_SHORT_REGEX = re.compile(
    r'(?:https?://)?(?:s\.lazada\.vn/s\.[^\s\n\r,<>"?]+|c\.lazada\.vn/t/c\.[^\s\n\r,<>"?]+)(?:\?[^\s\n\r,<>"]*)?',
    re.IGNORECASE
)

# Link sản phẩm Lazada trực tiếp (chỉ cần encode + thêm aff)
LAZADA_DIRECT_REGEX = re.compile(
    r'(?:https?://)?(?:www\.)?lazada\.vn/(?:products/[^\s\n\r,<>"]+|i\d+-s\d+[^\s\n\r,<>"]*)',
    re.IGNORECASE
)

# ========== HELPERS ==========
async def get_final_url(url: str) -> str:
    """Unshorten Shopee - follow redirects thông thường."""
    if not re.match(r'https?://', url, re.I):
        url = "https://" + url
    try:
        async with httpx.AsyncClient(
            follow_redirects=True, timeout=15,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        ) as c:
            r = await c.get(url)
            return str(r.url)
    except Exception:
        return url

async def unshorten_lazada(url: str) -> str:
    """
    Lazada chặn auto-follow → thủ công theo từng bước redirect.
    Trả về URL đích cuối cùng (dạng dài).
    """
    if not re.match(r'https?://', url, re.I):
        url = "https://" + url

    headers = {
        "User-Agent": "Mozilla/5.0 (Linux; Android 12; SM-G991B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "vi-VN,vi;q=0.9,en;q=0.8",
    }

    current_url = url
    max_hops = 10

    for i in range(max_hops):
        try:
            async with httpx.AsyncClient(
                follow_redirects=False,
                timeout=15,
                headers=headers
            ) as c:
                r = await c.get(current_url)
                location = r.headers.get("location", "").strip()

                logging.info(f"[Lazada hop {i+1}] {current_url} → status={r.status_code} location={location[:80] if location else 'none'}")

                if r.status_code in (301, 302, 303, 307, 308) and location:
                    # Nếu location là relative URL thì ghép lại
                    if location.startswith("/"):
                        parsed = urlparse(current_url)
                        location = f"{parsed.scheme}://{parsed.netloc}{location}"
                    current_url = location

                    # Nếu đã ra khỏi lazada short domain → đây là URL đích
                    if not re.search(r's\.lazada\.vn|c\.lazada\.vn/t/', current_url, re.I):
                        # Follow thêm 1 lần nữa nếu vẫn là lazada.vn thông thường
                        return current_url
                else:
                    # Không còn redirect → đây là URL cuối
                    return current_url
        except Exception as e:
            logging.error(f"[Lazada unshorten] Lỗi hop {i+1}: {e}")
            break

    return current_url

async def shorten(long_url: str) -> str:
    """Gọi api.php để rút gọn."""
    if not re.match(r'https?://', long_url, re.I):
        long_url = "https://" + long_url
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.post(API_URL, data={"long_url": long_url})
        d = r.json()
        if "short_url" in d:
            return d["short_url"]
        raise ValueError(d.get("error", "Lỗi API"))

def build_shopee_aff(real_url: str) -> str:
    if "an_redir" in real_url and "affiliate_id" in real_url:
        parsed = urlparse(real_url)
        qs = parse_qs(parsed.query, keep_blank_values=True)
        qs["affiliate_id"] = [AFFILIATE_ID]
        qs["sub_id"] = [SUB_ID]
        new_query = urlencode({k: v[0] for k, v in qs.items()})
        return urlunparse(parsed._replace(query=new_query))
    enc = quote(real_url, safe='')
    return f"https://s.shopee.vn/an_redir?origin_link={enc}&affiliate_id={AFFILIATE_ID}&sub_id={SUB_ID}"

def build_lazada_aff(real_url: str) -> str:
    """Encode URL rồi thêm prefix affiliate Lazada."""
    enc = quote(real_url, safe='')
    return f"{LAZADA_AFF}{enc}"

# ========== XỬ LÝ TEXT ==========
async def process_rut(text: str) -> str:
    """Rút gọn tất cả URL, giữ nguyên định dạng."""
    result = text
    matches = URL_REGEX.findall(text)
    seen = {}

    for raw in matches:
        clean = raw.rstrip(".,!? ")
        if not clean:
            continue
        if clean in seen:
            result = result.replace(raw, seen[clean], 1)
            continue
        url_to_shorten = clean if re.match(r'https?://', clean, re.I) else "https://" + clean
        try:
            short = await shorten(url_to_shorten)
            seen[clean] = short
            result = result.replace(raw, short, 1)
        except Exception:
            pass

    return result

async def process_shopee_aff(text: str) -> str:
    """Shopee: unshorten → affiliate → rút gọn."""
    result = text
    matches = list(dict.fromkeys(SHOPEE_REGEX.findall(text)))

    for raw in matches:
        clean = raw.rstrip(".,!? ")
        url = clean if re.match(r'https?://', clean, re.I) else "https://" + clean
        real_url = await get_final_url(url)
        real_url = real_url.replace("thanhsansale.com", "").replace("thanhsansale", "")
        if re.search(r'shopee\.vn|shope\.ee', real_url, re.I):
            aff_link = build_shopee_aff(real_url)
            try:
                short = await shorten(aff_link)
                result = result.replace(raw, short, 1)
            except Exception:
                pass

    return result

async def process_lazada_short(text: str) -> str:
    """
    Lazada rút gọn (s.lazada.vn / c.lazada.vn/t/):
    unshorten → lấy URL đích dài → encode → thêm aff → rút gọn
    """
    result = text
    matches = list(dict.fromkeys(LAZADA_SHORT_REGEX.findall(text)))

    for raw in matches:
        clean = raw.rstrip(".,!? ")
        url = clean if re.match(r'https?://', clean, re.I) else "https://" + clean

        logging.info(f"[Lazada short] Đang xử lý: {url}")
        real_url = await unshorten_lazada(url)
        logging.info(f"[Lazada short] URL đích: {real_url[:100]}")

        if re.search(r'lazada\.vn', real_url, re.I):
            aff_link = build_lazada_aff(real_url)
            try:
                short = await shorten(aff_link)
                result = result.replace(raw, short, 1)
                logging.info(f"[Lazada short] Kết quả: {short}")
            except Exception as e:
                logging.error(f"[Lazada short] Lỗi rút gọn: {e}")

    return result

async def process_lazada_direct(text: str) -> str:
    """
    Lazada link trực tiếp (lazada.vn/products/ hoặc lazada.vn/iXXX):
    encode → thêm aff → rút gọn (không cần unshorten)
    """
    result = text
    matches = list(dict.fromkeys(LAZADA_DIRECT_REGEX.findall(text)))

    for raw in matches:
        clean = raw.rstrip(".,!? ")
        url = clean if re.match(r'https?://', clean, re.I) else "https://" + clean

        logging.info(f"[Lazada direct] Đang xử lý: {url}")
        aff_link = build_lazada_aff(url)
        try:
            short = await shorten(aff_link)
            result = result.replace(raw, short, 1)
            logging.info(f"[Lazada direct] Kết quả: {short}")
        except Exception as e:
            logging.error(f"[Lazada direct] Lỗi rút gọn: {e}")

    return result

# ========== SEND HELPER ==========
async def send_result(update: Update, result: str):
    escaped = html.escape(result)
    try:
        await update.message.reply_text(escaped, parse_mode="HTML")
    except Exception:
        await update.message.reply_text(result)

# ========== COMMAND HANDLERS ==========
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 Bot Rút Gọn Link\n\n"
        "🛒 Gửi link Shopee → tự thêm affiliate + rút gọn\n"
        "💙 Gửi link Lazada (rút gọn hoặc trực tiếp) → tự thêm affiliate + rút gọn\n"
        "✂️ /rut [link/đoạn văn] → chỉ rút gọn thuần, không thêm affiliate\n\n"
        "💡 /rut có thể reply vào tin nhắn bất kỳ!"
    )

async def cmd_rut(update: Update, context: ContextTypes.DEFAULT_TYPE):
    full_text = update.message.text or ""
    text = re.sub(r'^/rut\s*', '', full_text, flags=re.IGNORECASE).strip()

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
    """Tin nhắn thường → tự nhận diện loại link và xử lý."""
    text = update.message.text or ""

    has_shopee      = bool(SHOPEE_REGEX.search(text))
    has_lazada_short  = bool(LAZADA_SHORT_REGEX.search(text))
    has_lazada_direct = bool(LAZADA_DIRECT_REGEX.search(text))

    if not any([has_shopee, has_lazada_short, has_lazada_direct]):
        return  # Không có link liên quan → bỏ qua

    await update.message.reply_text("⏳ Đang xử lý...")

    result = text
    if has_shopee:
        result = await process_shopee_aff(result)
    if has_lazada_short:
        result = await process_lazada_short(result)
    if has_lazada_direct:
        result = await process_lazada_direct(result)

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
