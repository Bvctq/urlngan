import os, re, html, httpx, hmac, hashlib, time, logging
from urllib.parse import quote, urlparse, parse_qs, urlencode, urlunparse
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from telegram import Update
from telegram.ext import Application, MessageHandler, CommandHandler, filters, ContextTypes

# ========== CẤU HÌNH ==========
BOT_TOKEN      = os.environ.get("BOT_TOKEN")
API_URL        = os.environ.get("API_URL", "https://s.allvn.top/api.php")
WEBHOOK_URL    = os.environ.get("WEBHOOK_URL")

# Shopee
SHOPEE_AFF_ID  = "17350890105"
SHOPEE_SUB_ID  = "----CR--"

# Lazada Official API
LAZ_APP_KEY    = os.environ.get("LAZ_APP_KEY",    "105827")
LAZ_APP_SECRET = os.environ.get("LAZ_APP_SECRET", "r8ZMKhPxu1JZUCwTUBVMJiJnZKjhWeQF")
LAZ_USER_TOKEN = os.environ.get("LAZ_USER_TOKEN", "f879c4163b0f4c5a90c1567fcffac91e")
LAZ_BASE_URL   = "https://api.lazada.vn"
LAZ_SDK_VER    = "lazop-sdk-python-affiliate-1.0"

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

LAZADA_REGEX = re.compile(
    r'(?:https?://)?'
    r'(?:s\.lazada\.vn/s\.[^\s\n\r,<>"]+|'
    r'c\.lazada\.vn/t/c\.[^\s\n\r,<>"]+|'
    r'(?:www\.)?lazada\.vn/[^\s\n\r,<>"]+)',
    re.IGNORECASE
)

# Tracking params cần xóa (theo PHP gốc)
LAZ_TRACKING_PARAMS = {
    'trafficfrom','laz_trackid','mkttid','exlaz','spm','scm','from',
    'clicktrackinfo','search','mp','c','abbucket','aff_trace_key',
    'aff_platform','aff_request_id','sk','utparam','dsource',
    'laz_share_info','laz_token','cc','src','channel'
}

LAZ_FETCH_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Upgrade-Insecure-Requests": "1",
}

# ============================================================
# LAZADA: SIGN + CALL API
# ============================================================
def laz_sign(api_path: str, params: dict) -> str:
    sorted_items = sorted(params.items())
    string_to_sign = api_path + "".join(k + str(v) for k, v in sorted_items)
    return hmac.new(
        LAZ_APP_SECRET.encode("utf-8"),
        string_to_sign.encode("utf-8"),
        hashlib.sha256
    ).hexdigest().upper()

async def laz_call_getlink(input_type: str, input_value: str) -> str | None:
    """
    Gọi Lazada /marketing/getlink.
    input_type: 'productId' | 'url'
    Trả về tracking link hoặc None.
    """
    api_path = "/marketing/getlink"
    timestamp = int(time.time() * 1000)

    api_params = {
        "userToken":  LAZ_USER_TOKEN,
        "inputType":  input_type,
        "inputValue": input_value,
    }
    sys_params = {
        "app_key":     LAZ_APP_KEY,
        "sign_method": "sha256",
        "timestamp":   timestamp,
        "partner_id":  LAZ_SDK_VER,
    }
    all_params = {**api_params, **sys_params}
    sys_params["sign"] = laz_sign(api_path, all_params)

    url = f"{LAZ_BASE_URL}/rest{api_path}?{urlencode(sys_params)}&{urlencode(api_params)}"

    try:
        async with httpx.AsyncClient(timeout=15, verify=False,
                                     headers={"User-Agent": LAZ_SDK_VER}) as c:
            r = await c.get(url)
            data = r.json()

        code = str(data.get("code", ""))
        if code not in ("0", ""):
            logging.error(f"[LazAPI] code={code} msg={data.get('message')}")
            return None

        result_data = data.get("result", {}).get("data", {})

        for list_key in ("urlBatchGetLinkInfoList",
                         "productBatchGetLinkInfoList",
                         "offerBatchGetLinkInfoList"):
            items = result_data.get(list_key, [])
            if items:
                item = items[0]
                link = (item.get("regularPromotionLink")
                        or item.get("offerPromotionLink")
                        or item.get("mmPromotionLink")
                        or item.get("dmPromotionLink") or "")
                if link:
                    logging.info(f"[LazAPI] tracking link: {link[:80]}")
                    return link

        return (result_data.get("trackingLink")
                or result_data.get("regularPromotionLink")
                or result_data.get("offerPromotionLink")
                or None)

    except Exception as e:
        logging.error(f"[LazAPI] Exception: {e}")
        return None

# ============================================================
# LAZADA: RESOLVE SHORT URL (giống PHP resolveShortUrl)
# ============================================================
def laz_find_js_redirect(body: str) -> str | None:
    patterns = [
        r'window\.location\.href\s*=\s*["\']([^"\']{10,})["\']',
        r'window\.location\s*=\s*["\']([^"\']{10,})["\']',
        r'location\.href\s*=\s*["\']([^"\']{10,})["\']',
        r'location\.replace\s*\(\s*["\']([^"\']{10,})["\']',
        r'location\.assign\s*\(\s*["\']([^"\']{10,})["\']',
        r'top\.location\.href\s*=\s*["\']([^"\']{10,})["\']',
        r'window\.location\.replace\s*\(\s*["\']([^"\']{10,})["\']',
        r'<meta[^>]+http-equiv=["\']?refresh["\']?[^>]+content=["\']\d+;\s*url=([^"\'>\s]+)',
    ]
    for pat in patterns:
        m = re.search(pat, body, re.I)
        if m:
            val = m.group(1)
            val = val.replace("&amp;", "&").replace("&#39;", "'")
            try:
                from urllib.parse import unquote
                val = unquote(val)
            except Exception:
                pass
            return val
    return None

async def laz_follow_url(url: str, referer: str = None) -> dict | None:
    """Follow URL, trả về {finalUrl, body}. Giống PHP followUrl."""
    headers = dict(LAZ_FETCH_HEADERS)
    if referer:
        headers["Referer"] = referer
    try:
        async with httpx.AsyncClient(
            follow_redirects=True, timeout=20, verify=False,
            headers=headers
        ) as c:
            r = await c.get(url)
            return {"finalUrl": str(r.url), "body": r.text}
    except Exception as e:
        logging.error(f"[LazFollow] {e}")
        return None

async def laz_resolve_short_url(url: str) -> str:
    """
    Giải link s.lazada.vn / c.lazada.vn → URL thật.
    Logic giống hệt PHP resolveShortUrl.
    """
    if not re.match(r'https?://', url, re.I):
        url = "https://" + url

    result = await laz_follow_url(url)
    if not result:
        return url

    final_url = result["finalUrl"]
    body = result["body"]

    # Thử JS redirect lần 1
    next_url = laz_find_js_redirect(body)
    if next_url and next_url.startswith("http"):
        second = await laz_follow_url(next_url, url)
        if second:
            final_url = second["finalUrl"]
            body = second["body"]
            # Thử lần 2
            next_url2 = laz_find_js_redirect(body)
            if next_url2 and next_url2.startswith("http"):
                third = await laz_follow_url(next_url2, final_url)
                if third:
                    final_url = third["finalUrl"]

    # Nếu vẫn là c.lazada.vn → follow thêm lần nữa
    if re.search(r'c\.lazada\.', final_url, re.I):
        extra = await laz_follow_url(final_url, url)
        if extra:
            new_final = extra["finalUrl"]
            if not re.search(r'c\.lazada\.', new_final, re.I):
                final_url = new_final
            else:
                # Thử parse JS
                next_js = laz_find_js_redirect(extra["body"])
                if next_js and next_js.startswith("http"):
                    last = await laz_follow_url(next_js, final_url)
                    if last:
                        final_url = last["finalUrl"]

    logging.info(f"[LazResolve] {url[:60]} → {final_url[:80]}")
    return final_url

# ============================================================
# LAZADA: CLEAN + EXTRACT + ANALYZE (giống PHP analyzeInput)
# ============================================================
def laz_clean_url(url: str) -> str:
    """Xóa tracking params, giống PHP cleanTrackingParams."""
    try:
        parsed = urlparse(url)
        if not parsed.netloc:
            return url

        query = parsed.query

        # Cắt &trafficFrom= trở đi
        pos = query.lower().find("&trafficfrom=")
        if pos != -1:
            query = query[:pos]
        if query.lower().startswith("trafficfrom="):
            query = ""

        params = parse_qs(query, keep_blank_values=True)
        cleaned = {k: v[0] for k, v in params.items()
                   if k.lower() not in LAZ_TRACKING_PARAMS}
        new_query = urlencode(cleaned) if cleaned else ""

        return urlunparse(parsed._replace(query=new_query, fragment=""))
    except Exception:
        return url

def laz_is_homepage(url: str) -> bool:
    parsed = urlparse(url)
    path = parsed.path.strip("/")
    if not path:
        return True
    if re.match(r'^(vn|sg|th|my|id|ph)$', path, re.I):
        return True
    return False

def laz_extract_product_id(url: str) -> str | None:
    patterns = [
        r'-i(\d+)(?:-s|\.|$|\?)',
        r'/i(\d+)(?:-|\.|$|\?)',
        r'itemId=(\d+)',
        r'product/(\d+)',
    ]
    for pat in patterns:
        m = re.search(pat, url)
        if m:
            return m.group(1)
    return None

def laz_is_lazada_domain(host: str) -> bool:
    host = host.lower()
    return "lazada." in host

async def laz_get_tracking(raw: str) -> str | None:
    """
    Flow đầy đủ cho 1 URL Lazada (giống PHP analyzeInput + getTrackingLink):
    1. Nếu là short link → resolve
    2. Nếu không phải domain Lazada → follow xem có ra Lazada không
    3. Clean tracking params
    4. Extract product ID → gọi API productId, không có thì gọi url
    5. Trả về tracking link
    """
    url = raw.strip().rstrip(".,!? ")
    if not re.match(r'https?://', url, re.I):
        url = "https://" + url

    original_host = urlparse(url).netloc.lower()

    # Bước 1: Xử lý short link hoặc domain không phải Lazada
    if not laz_is_lazada_domain(original_host):
        # Follow xem có ra Lazada không (link rút gọn từ bên thứ 3)
        result = await laz_follow_url(url)
        if not result:
            return None
        final_host = urlparse(result["finalUrl"]).netloc.lower()
        if not laz_is_lazada_domain(final_host):
            logging.warning(f"[Lazada] Không phải domain Lazada: {result['finalUrl'][:60]}")
            return None
        url = result["finalUrl"]

    elif re.match(r'https?://(?:s|c)\.lazada\.vn/', url, re.I):
        # Short link chính thức của Lazada
        resolved = await laz_resolve_short_url(url)
        if laz_is_homepage(resolved):
            logging.warning(f"[Lazada] Resolved to homepage")
            return None
        url = resolved

    # Bước 2: Clean
    cleaned = laz_clean_url(url)
    logging.info(f"[Lazada] Cleaned: {cleaned[:80]}")

    # Bước 3: Extract product ID
    product_id = laz_extract_product_id(cleaned)

    if product_id:
        logging.info(f"[Lazada] → productId: {product_id}")
        tracking = await laz_call_getlink("productId", product_id)
    else:
        logging.info(f"[Lazada] → url: {cleaned[:80]}")
        tracking = await laz_call_getlink("url", cleaned)

    return tracking

# ============================================================
# SHOPEE
# ============================================================
async def shopee_get_final_url(url: str) -> str:
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

def shopee_build_aff(real_url: str) -> str:
    if "an_redir" in real_url and "affiliate_id" in real_url:
        parsed = urlparse(real_url)
        qs = parse_qs(parsed.query, keep_blank_values=True)
        qs["affiliate_id"] = [SHOPEE_AFF_ID]
        qs["sub_id"]       = [SHOPEE_SUB_ID]
        new_query = urlencode({k: v[0] for k, v in qs.items()})
        return urlunparse(parsed._replace(query=new_query))
    enc = quote(real_url, safe="")
    return f"https://s.shopee.vn/an_redir?origin_link={enc}&affiliate_id={SHOPEE_AFF_ID}&sub_id={SHOPEE_SUB_ID}"

# ============================================================
# SHORTEN
# ============================================================
async def shorten(long_url: str) -> str:
    if not re.match(r'https?://', long_url, re.I):
        long_url = "https://" + long_url
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.post(API_URL, data={"long_url": long_url})
        d = r.json()
        if "short_url" in d:
            return d["short_url"]
        raise ValueError(d.get("error", "Lỗi API"))

# ============================================================
# PROCESS TEXT
# ============================================================
async def process_rut(text: str) -> str:
    """Rút gọn tất cả URL, giữ nguyên định dạng."""
    result = text
    seen   = {}
    for raw in URL_REGEX.findall(text):
        clean = raw.rstrip(".,!? ")
        if not clean:
            continue
        if clean in seen:
            result = result.replace(raw, seen[clean], 1)
            continue
        url = clean if re.match(r'https?://', clean, re.I) else "https://" + clean
        try:
            short = await shorten(url)
            seen[clean] = short
            result = result.replace(raw, short, 1)
        except Exception:
            pass
    return result

async def process_shopee(text: str) -> str:
    result  = text
    matches = list(dict.fromkeys(SHOPEE_REGEX.findall(text)))
    for raw in matches:
        clean    = raw.rstrip(".,!? ")
        url      = clean if re.match(r'https?://', clean, re.I) else "https://" + clean
        real_url = await shopee_get_final_url(url)
        real_url = real_url.replace("thanhsansale.com", "").replace("thanhsansale", "")
        if re.search(r'shopee\.vn|shope\.ee', real_url, re.I):
            try:
                short = await shorten(shopee_build_aff(real_url))
                result = result.replace(raw, short, 1)
            except Exception:
                pass
    return result

async def process_lazada(text: str) -> str:
    result  = text
    matches = list(dict.fromkeys(LAZADA_REGEX.findall(text)))
    for raw in matches:
        clean    = raw.rstrip(".,!? ")
        tracking = await laz_get_tracking(clean)
        if tracking:
            try:
                short  = await shorten(tracking)
                result = result.replace(raw, short, 1)
                logging.info(f"[Lazada] {clean[:40]} → {short}")
            except Exception as e:
                logging.error(f"[Lazada] Lỗi rút gọn: {e}")
        else:
            logging.warning(f"[Lazada] Bỏ qua (không lấy được tracking): {clean[:60]}")
    return result

# ============================================================
# SEND
# ============================================================
async def send_result(update: Update, text: str):
    try:
        await update.message.reply_text(html.escape(text), parse_mode="HTML")
    except Exception:
        await update.message.reply_text(text)

# ============================================================
# HANDLERS
# ============================================================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 Bot Rút Gọn Link\n\n"
        "🛒 Gửi link Shopee → affiliate + rút gọn\n"
        "💙 Gửi link Lazada → affiliate (API chính thức) + rút gọn\n"
        "✂️ /rut [link/đoạn văn] → chỉ rút gọn thuần\n\n"
        "💡 /rut có thể reply vào tin nhắn bất kỳ!"
    )

async def cmd_rut(update: Update, context: ContextTypes.DEFAULT_TYPE):
    full = update.message.text or ""
    text = re.sub(r'^/rut\s*', '', full, flags=re.IGNORECASE).strip()

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

    await send_result(update, await process_rut(text))

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or ""

    has_shopee = bool(SHOPEE_REGEX.search(text))
    has_lazada = bool(LAZADA_REGEX.search(text))

    if not has_shopee and not has_lazada:
        return

    await update.message.reply_text("⏳ Đang xử lý...")

    result = text
    if has_shopee:
        result = await process_shopee(result)
    if has_lazada:
        result = await process_lazada(result)

    await send_result(update, result)

# ============================================================
# FASTAPI + WEBHOOK
# ============================================================
ptb_app = Application.builder().token(BOT_TOKEN).updater(None).build()
ptb_app.add_handler(CommandHandler("start", cmd_start))
ptb_app.add_handler(CommandHandler("rut",   cmd_rut))
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
    data   = await request.json()
    update = Update.de_json(data, ptb_app.bot)
    await ptb_app.process_update(update)
    return {"ok": True}

@fastapi_app.get("/")
async def root():
    return {"status": "🤖 Bot đang chạy"}
