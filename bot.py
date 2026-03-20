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

# Link rút gọn Lazada — cần unshorten qua JS parse
LAZADA_SHORT_REGEX = re.compile(
    r'(?:https?://)?'
    r'(?:s\.lazada\.vn/s\.[^\s\n\r,<>"]+|c\.lazada\.vn/t/c\.[^\s\n\r,<>"]+)',
    re.IGNORECASE
)

# Link sản phẩm Lazada trực tiếp — encode + aff luôn
LAZADA_DIRECT_REGEX = re.compile(
    r'(?:https?://)?(?:www\.)?lazada\.vn/'
    r'(?:products/[^\s\n\r,<>"]+|i\d+-s\d+[^\s\n\r,<>"]*)',
    re.IGNORECASE
)

# ========== LAZADA UNSHORTEN (parse HTML + JS) ==========
LAZADA_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) "
        "Version/16.0 Mobile/15E148 Safari/604.1"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
}

def extract_url_from_html(body: str, base_url: str) -> str | None:
    """Tìm URL redirect trong HTML/JS body."""

    # 1. window.location.href = "..."
    m = re.search(r'window\.location(?:\.href)?\s*=\s*["\']([^"\']{10,})["\']', body)
    if m:
        return m.group(1)

    # 2. window.location.replace("...")
    m = re.search(r'window\.location\.replace\s*\(\s*["\']([^"\']{10,})["\']', body)
    if m:
        return m.group(1)

    # 3. location.href = "..."
    m = re.search(r'location\.href\s*=\s*["\']([^"\']{10,})["\']', body)
    if m:
        return m.group(1)

    # 4. meta refresh
    m = re.search(
        r'<meta[^>]+http-equiv=["\']?refresh["\']?[^>]+content=["\'][^"\']*url=([^"\'>\s]+)',
        body, re.I
    )
    if m:
        return m.group(1)

    # 5. data-url attribute
    m = re.search(r'data-url=["\']([^"\']{10,})["\']', body)
    if m:
        return m.group(1)

    # 6. <a href> duy nhất trỏ về lazada.vn (trang đích)
    m = re.search(r'href=["\']([^"\']*lazada\.vn[^"\']*)["\']', body)
    if m:
        return m.group(1)

    return None

async def unshorten_lazada(url: str) -> str:
    """
    Lazada dùng JS redirect → phải parse HTML body.
    Trả về URL đích cuối cùng.
    """
    if not re.match(r'https?://', url, re.I):
        url = "https://" + url

    current_url = url
    visited = set()

    for hop in range(10):
        if current_url in visited:
            logging.info(f"[Lazada] Vòng lặp tại {current_url}")
            break
        visited.add(current_url)
        logging.info(f"[Lazada hop {hop+1}] Đang fetch: {current_url[:80]}")

        try:
            async with httpx.AsyncClient(
                follow_redirects=False,
                timeout=15,
                headers=LAZADA_HEADERS
            ) as c:
                r = await c.get(current_url)

            # --- Thử HTTP redirect trước ---
            location = r.headers.get("location", "").strip()
            if location and r.status_code in (301, 302, 303, 307, 308):
                if location.startswith("/"):
                    parsed = urlparse(current_url)
                    location = f"{parsed.scheme}://{parsed.netloc}{location}"
                logging.info(f"[Lazada hop {hop+1}] HTTP redirect → {location[:80]}")
                current_url = location

                # Nếu đã ra khỏi domain rút gọn → dừng
                if not re.search(r's\.lazada\.vn|c\.lazada\.vn/t/', current_url, re.I):
                    return current_url
                continue

            # --- Không có HTTP redirect → parse HTML/JS ---
            body = r.text
            next_url = extract_url_from_html(body, current_url)

            if next_url:
                if next_url.startswith("/"):
                    parsed = urlparse(current_url)
                    next_url = f"{parsed.scheme}://{parsed.netloc}{next_url}"
                logging.info(f"[Lazada hop {hop+1}] JS/HTML redirect → {next_url[:80]}")
                current_url = next_url

                if not re.search(r's\.lazada\.vn|c\.lazada\.vn/t/', current_url, re.I):
                    return current_url
                continue

            # Không tìm thấy redirect nào → đây là trang đích
            logging.info(f"[Lazada] Kết thúc tại: {current_url[:80]}")
            break

        except Exception as e:
            logging.error(f"[Lazada hop {hop+1}] Lỗi: {e}")
            break

    return current_url

# ========== SHOPEE UNSHORTEN ==========
async def get_final_url(url: str) -> str:
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

# ========== SHORTEN + AFF ==========
async def shorten(long_url: str) -> str:
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
    enc = quote(real_url, safe='')
    return f"{LAZADA_AFF}{enc}"

# ========== XỬ LÝ TEXT ==========
async def process_rut(text: str) -> str:
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
    """Link rút gọn Lazada → unshorten (parse JS) → encode → aff → rút gọn."""
    result = text
    matches = list(dict.fromkeys(LAZADA_SHORT_REGEX.findall(text)))
    for raw in matches:
        clean = raw.rstrip(".,!? ")
        url = clean if re.match(r'https?://', clean, re.I) else "https://" + clean

        real_url = await unshorten_lazada(url)
        logging.info(f"[Lazada short] {url} → {real_url[:80]}")

        # Chỉ xử lý nếu đã thoát khỏi domain rút gọn
        if re.search(r'lazada\.vn', real_url, re.I) and not re.search(r's\.lazada\.vn|c\.lazada\.vn/t/', real_url, re.I):
            aff_link = build_lazada_aff(real_url)
            try:
                short = await shorten(aff_link)
                result = result.replace(raw, short, 1)
            except Exception as e:
                logging.error(f"[Lazada short] Lỗi rút gọn: {e}")
        else:
            logging.warning(f"[Lazada short] Không lấy được URL đích, bỏ qua: {real_url[:80]}")

    return result

async def process_lazada_direct(text: str) -> str:
    """Link sản phẩm Lazada trực tiếp → encode + aff → rút gọn."""
    result = text
    matches = list(dict.fromkeys(LAZADA_DIRECT_REGEX.findall(text)))
    for raw in matches:
        clean = raw.rstrip(".,!? ")
        url = clean if re.match(r'https?://', clean, re.I) else "https://" + clean
        aff_link = build_lazada_aff(url)
        try:
            short = await shorten(aff_link)
            result = result.replace(raw, short, 1)
        except Exception as e:
            logging.error(f"[Lazada direct] Lỗi: {e}")
    return result

# ========== SEND HELPER ==========
async def send_result(update: Update, result: str):
    escaped = html.escape(result)
    try:
        await update.message.reply_text(escaped, parse_mode="HTML")
    except Exception:
        await update.message.reply_text(result)

# ========== HANDLERS ==========
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 Bot Rút Gọn Link\n\n"
        "🛒 Link Shopee → tự thêm affiliate + rút gọn\n"
        "💙 Link Lazada (rút gọn/trực tiếp) → tự thêm affiliate + rút gọn\n"
        "✂️ /rut [link/đoạn văn] → chỉ rút gọn thuần\n\n"
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
    text = update.message.text or ""
    has_shopee        = bool(SHOPEE_REGEX.search(text))
    has_lazada_short  = bool(LAZADA_SHORT_REGEX.search(text))
    has_lazada_direct = bool(LAZADA_DIRECT_REGEX.search(text))

    if not any([has_shopee, has_lazada_short, has_lazada_direct]):
        return

    await update.message.reply_text("⏳ Đang xử lý...")
    result = text
    if has_shopee:
        result = await process_shopee_aff(result)
    if has_lazada_short:
        result = await process_lazada_short(result)
    if has_lazada_direct:
        result = await process_lazada_direct(result)
    await send_result(update, result)

# ========== SETUP ==========
ptb_app = Application.builder().token(BOT_TOKEN).updater(None).build()
ptb_app.add_handler(CommandHandler("start", cmd_start))
ptb_app.add_handler(CommandHandler("rut", cmd_rut))
ptb_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

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
