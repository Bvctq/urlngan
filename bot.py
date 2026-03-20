import os, re, html, httpx, hmac, hashlib, time, logging
from urllib.parse import quote, urlparse, parse_qs, urlencode, urlunparse
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from telegram import Update
from telegram.ext import Application, MessageHandler, CommandHandler, filters, ContextTypes

# ========== CẤU HÌNH ==========
BOT_TOKEN       = os.environ.get("BOT_TOKEN")
WEBHOOK_URL     = os.environ.get("WEBHOOK_URL")

SHOPEE_SUB_ID   = "----CR--"
SHOPEE_AFF_ID   = os.environ.get("SHOPEE_AFF_ID", "17350890105")
current_aff_id  = SHOPEE_AFF_ID

API_URL         = os.environ.get("API_URL", "https://s.allvn.top/api.php")
current_api_url = API_URL

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

# Domain Shopee chính thức
SHOPEE_DIRECT_REGEX = re.compile(
    r'(?:https?://)?(?:[a-z0-9.-]*)'
    r'(?:shopee\.vn|shope\.ee|app\.shopeepay\.vn|s\.5anm\.net|shp\.ee)'
    r'[^\s\n\r,<>"]*',
    re.IGNORECASE
)

# Domain rút gọn không rõ đích (có thể là Shopee hoặc Lazada)
# → cần follow để biết đích rồi mới xử lý
SHORT_UNKNOWN_REGEX = re.compile(
    r'(?:https?://)?'
    r'(?:sandeal\.co|hoisansale\.pro|nghien\.co|thanhsansale\.online|bit\.ly|tinyurl\.com)'
    r'/[^\s\n\r,<>"]*',
    re.IGNORECASE
)

# Domain Lazada chính thức
LAZADA_REGEX = re.compile(
    r'(?:https?://)?'
    r'(?:'
        r'(?:s|c)\.lazada\.(?:vn|sg|co\.th|com\.my|co\.id|com\.ph)/[^\s\n\r,<>"?]*(?:\?[^\s\n\r,<>"]*)?'
        r'|'
        r'(?:www\.)?lazada\.(?:vn|sg|co\.th|com\.my|co\.id|com\.ph)/[^\s\n\r,<>"]*'
    r')',
    re.IGNORECASE
)

LAZ_TRACKING_PARAMS = {
    'trafficfrom','laz_trackid','mkttid','exlaz','spm','scm','from',
    'clicktrackinfo','search','mp','c','abbucket','aff_trace_key',
    'aff_platform','aff_request_id','sk','utparam','dsource',
    'laz_share_info','laz_token','cc','src','channel'
}

LAZ_FETCH_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}

# ============================================================
# LAZADA API
# ============================================================
def laz_sign(api_path: str, params: dict) -> str:
    sorted_items   = sorted(params.items())
    string_to_sign = api_path + "".join(str(k) + str(v) for k, v in sorted_items)
    return hmac.new(
        LAZ_APP_SECRET.encode("utf-8"),
        string_to_sign.encode("utf-8"),
        hashlib.sha256
    ).hexdigest().upper()

async def laz_call_getlink(input_type: str, input_value: str) -> str | None:
    api_path  = "/marketing/getlink"
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
            r    = await c.get(url)
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
                    logging.info(f"[LazAPI] OK: {link[:80]}")
                    return link

        return (result_data.get("trackingLink")
                or result_data.get("regularPromotionLink")
                or result_data.get("offerPromotionLink")
                or None)

    except Exception as e:
        logging.error(f"[LazAPI] Exception: {e}")
        return None

# ============================================================
# LAZADA RESOLVE SHORT URL
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
            val = m.group(1).replace("&amp;", "&").replace("&#39;", "'")
            return val
    return None

async def laz_follow_url(url: str, referer: str = None) -> dict | None:
    headers = dict(LAZ_FETCH_HEADERS)
    if referer:
        headers["Referer"] = referer
    try:
        async with httpx.AsyncClient(
            follow_redirects=True, timeout=20,
            verify=False, headers=headers
        ) as c:
            r = await c.get(url)
            return {"finalUrl": str(r.url), "body": r.text}
    except Exception as e:
        logging.error(f"[LazFollow] {e}")
        return None

def laz_is_short_domain(url: str) -> bool:
    return bool(re.match(r'https?://(?:s|c)\.lazada\.', url, re.I))

async def laz_resolve_short_url(url: str) -> str:
    if not re.match(r'https?://', url, re.I):
        url = "https://" + url

    result = await laz_follow_url(url)
    if not result:
        return url

    final_url = result["finalUrl"]
    body      = result["body"]

    next_url = laz_find_js_redirect(body)
    if next_url and next_url.startswith("http"):
        second = await laz_follow_url(next_url, url)
        if second:
            final_url = second["finalUrl"]
            body      = second["body"]
            next_url2 = laz_find_js_redirect(body)
            if next_url2 and next_url2.startswith("http"):
                third = await laz_follow_url(next_url2, final_url)
                if third:
                    final_url = third["finalUrl"]

    if re.search(r'c\.lazada\.', final_url, re.I):
        extra = await laz_follow_url(final_url, url)
        if extra:
            new_final = extra["finalUrl"]
            if not re.search(r'c\.lazada\.', new_final, re.I):
                final_url = new_final
            else:
                next_js = laz_find_js_redirect(extra["body"])
                if next_js and next_js.startswith("http"):
                    last = await laz_follow_url(next_js, final_url)
                    if last:
                        final_url = last["finalUrl"]

    logging.info(f"[LazResolve] {url[:60]} → {final_url[:80]}")
    return final_url

# ============================================================
# LAZADA CLEAN + EXTRACT
# ============================================================
def laz_clean_url(url: str) -> str:
    try:
        parsed = urlparse(url)
        if not parsed.netloc:
            return url
        query = parsed.query
        pos   = query.lower().find("&trafficfrom=")
        if pos != -1:
            query = query[:pos]
        if query.lower().startswith("trafficfrom="):
            query = ""
        params  = parse_qs(query, keep_blank_values=True)
        cleaned = {k: v[0] for k, v in params.items()
                   if k.lower() not in LAZ_TRACKING_PARAMS}
        new_query = urlencode(cleaned) if cleaned else ""
        return urlunparse(parsed._replace(query=new_query, fragment=""))
    except Exception:
        return url

def laz_is_homepage(url: str) -> bool:
    parsed = urlparse(url)
    path   = parsed.path.strip("/")
    return not path or bool(re.match(r'^(vn|sg|th|my|id|ph)$', path, re.I))

def laz_extract_product_id(url: str) -> str | None:
    for pat in (r'-i(\d+)(?:-s|\.|$|\?)', r'/i(\d+)(?:-|\.|$|\?)',
                r'itemId=(\d+)', r'product/(\d+)'):
        m = re.search(pat, url)
        if m:
            return m.group(1)
    return None

def laz_is_lazada_domain(host: str) -> bool:
    return "lazada." in host.lower()

def is_shopee_domain(host: str) -> bool:
    return bool(re.search(r'shopee\.|shope\.ee', host, re.I))

# ============================================================
# FOLLOW URL KHÔNG RÕ ĐÍCH → phát hiện Shopee hay Lazada
# ============================================================
async def follow_unknown_url(url: str) -> tuple[str, str]:
    """
    Follow URL không rõ đích.
    Trả về (final_url, loại): loại = 'shopee' | 'lazada' | 'unknown'
    """
    if not re.match(r'https?://', url, re.I):
        url = "https://" + url

    result = await laz_follow_url(url)
    if not result:
        return url, "unknown"

    final_url  = result["finalUrl"]
    final_host = urlparse(final_url).netloc.lower()

    if is_shopee_domain(final_host):
        logging.info(f"[Unknown] {url[:50]} → Shopee: {final_url[:60]}")
        return final_url, "shopee"

    if laz_is_lazada_domain(final_host):
        logging.info(f"[Unknown] {url[:50]} → Lazada: {final_url[:60]}")
        return final_url, "lazada"

    # Thử parse JS nếu chưa ra đích
    next_url = laz_find_js_redirect(result["body"])
    if next_url and next_url.startswith("http"):
        second     = await laz_follow_url(next_url, final_url)
        if second:
            final_url  = second["finalUrl"]
            final_host = urlparse(final_url).netloc.lower()
            if is_shopee_domain(final_host):
                return final_url, "shopee"
            if laz_is_lazada_domain(final_host):
                return final_url, "lazada"

    logging.warning(f"[Unknown] Không nhận ra đích: {final_url[:60]}")
    return final_url, "unknown"

# ============================================================
# LAZADA FLOW CHÍNH
# ============================================================
async def laz_get_tracking(raw: str) -> str | None:
    url = raw.strip().rstrip(".,!? ")
    if not re.match(r'https?://', url, re.I):
        url = "https://" + url

    host = urlparse(url).netloc.lower()

    if laz_is_short_domain(url):
        resolved = await laz_resolve_short_url(url)
        if laz_is_homepage(resolved):
            return None
        url = resolved
    elif not laz_is_lazada_domain(host):
        result = await laz_follow_url(url)
        if not result:
            return None
        final_host = urlparse(result["finalUrl"]).netloc.lower()
        if not laz_is_lazada_domain(final_host):
            return None
        url = result["finalUrl"]

    cleaned    = laz_clean_url(url)
    product_id = laz_extract_product_id(cleaned)

    if product_id:
        logging.info(f"[Lazada] → productId: {product_id}")
        return await laz_call_getlink("productId", product_id)
    else:
        logging.info(f"[Lazada] → url: {cleaned[:80]}")
        return await laz_call_getlink("url", cleaned)

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
    global current_aff_id
    if "an_redir" in real_url and "affiliate_id" in real_url:
        parsed = urlparse(real_url)
        qs     = parse_qs(parsed.query, keep_blank_values=True)
        qs["affiliate_id"] = [current_aff_id]
        qs["sub_id"]       = [SHOPEE_SUB_ID]
        new_query = urlencode({k: v[0] for k, v in qs.items()})
        return urlunparse(parsed._replace(query=new_query))
    enc = quote(real_url, safe="")
    return f"https://s.shopee.vn/an_redir?origin_link={enc}&affiliate_id={current_aff_id}&sub_id={SHOPEE_SUB_ID}"

# ============================================================
# SHORTEN
# ============================================================
async def shorten(long_url: str) -> str:
    global current_api_url
    if not re.match(r'https?://', long_url, re.I):
        long_url = "https://" + long_url
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.post(current_api_url, data={"long_url": long_url})
        d = r.json()
        if "short_url" in d:
            return d["short_url"]
        raise ValueError(d.get("error", "Lỗi API"))

# ============================================================
# PROCESS TEXT
# ============================================================
async def process_rut(text: str) -> str:
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

async def process_shopee_direct(text: str, url: str, raw: str) -> str:
    """Xử lý 1 URL Shopee đã biết chắc là Shopee."""
    real_url = await shopee_get_final_url(url)
    real_url = real_url.replace("thanhsansale.com", "").replace("thanhsansale", "")
    if re.search(r'shopee\.vn|shope\.ee', real_url, re.I):
        try:
            short = await shorten(shopee_build_aff(real_url))
            return text.replace(raw, short, 1)
        except Exception:
            pass
    return text

async def process_lazada_direct(text: str, url: str, raw: str) -> str:
    """Xử lý 1 URL Lazada đã biết chắc là Lazada."""
    tracking = await laz_get_tracking(url)
    if tracking:
        try:
            short = await shorten(tracking)
            return text.replace(raw, short, 1)
        except Exception as e:
            logging.error(f"[Lazada] Lỗi rút gọn: {e}")
    return text

async def process_all(text: str) -> str:
    result = text

    # 1. Xử lý Shopee domain chính thức
    for raw in list(dict.fromkeys(SHOPEE_DIRECT_REGEX.findall(text))):
        clean = raw.rstrip(".,!? ")
        url   = clean if re.match(r'https?://', clean, re.I) else "https://" + clean
        result = await process_shopee_direct(result, url, raw)

    # 2. Xử lý Lazada domain chính thức
    for raw in list(dict.fromkeys(LAZADA_REGEX.findall(text))):
        clean = raw.rstrip(".,!? ")
        result = await process_lazada_direct(result, clean, raw)

    # 3. Xử lý domain không rõ đích (sandeal.co, hoisansale.pro, ...)
    for raw in list(dict.fromkeys(SHORT_UNKNOWN_REGEX.findall(text))):
        clean    = raw.rstrip(".,!? ")
        url      = clean if re.match(r'https?://', clean, re.I) else "https://" + clean
        final_url, dest = await follow_unknown_url(url)

        if dest == "shopee":
            final_url = final_url.replace("thanhsansale.com", "").replace("thanhsansale", "")
            try:
                short  = await shorten(shopee_build_aff(final_url))
                result = result.replace(raw, short, 1)
            except Exception:
                pass

        elif dest == "lazada":
            tracking = await laz_get_tracking(final_url)
            if tracking:
                try:
                    short  = await shorten(tracking)
                    result = result.replace(raw, short, 1)
                except Exception:
                    pass

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
    global current_aff_id, current_api_url
    current_domain = current_api_url.replace("/api.php", "")
    await update.message.reply_text(
        "🤖 <b>Bot Rút Gọn Link</b>\n\n"
        "🛒 Gửi link Shopee → affiliate + rút gọn\n"
        "💙 Gửi link Lazada → affiliate (API chính thức) + rút gọn\n"
        "🔀 Domain rút gọn lạ → tự follow, nhận diện Shopee/Lazada\n"
        "✂️ /rut [link/đoạn văn] → chỉ rút gọn thuần\n"
        "🔑 /aff [id] → xem/đổi Shopee Affiliate ID\n"
        "🌐 /dm [domain] → xem/đổi domain rút gọn\n\n"
        f"Affiliate ID: <code>{current_aff_id}</code>\n"
        f"Domain: <code>{current_domain}</code>",
        parse_mode="HTML"
    )

async def cmd_aff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global current_aff_id
    if not context.args:
        await update.message.reply_text(
            f"🔑 Affiliate ID hiện tại: <code>{current_aff_id}</code>\n\n"
            f"Để đổi: <code>/aff 17317300048</code>",
            parse_mode="HTML"
        )
        return
    new_id = context.args[0].strip()
    if not re.match(r'^\d+$', new_id):
        await update.message.reply_text("⚠️ ID không hợp lệ, chỉ nhập số!\nVí dụ: /aff 17317300048")
        return
    old_id         = current_aff_id
    current_aff_id = new_id
    await update.message.reply_text(
        f"✅ Đã đổi Shopee Affiliate ID!\n\n"
        f"Cũ: <code>{old_id}</code>\n"
        f"Mới: <code>{current_aff_id}</code>",
        parse_mode="HTML"
    )

async def cmd_dm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global current_api_url
    if not context.args:
        current_domain = current_api_url.replace("/api.php", "")
        await update.message.reply_text(
            f"🌐 Domain rút gọn hiện tại: <code>{current_domain}</code>\n\n"
            f"Để đổi:\n"
            f"• <code>/dm s.allvn.top</code>\n"
            f"• <code>/dm s.salevn.top</code>",
            parse_mode="HTML"
        )
        return
    new_domain = context.args[0].strip().rstrip("/")
    if not re.match(r'https?://', new_domain, re.I):
        new_domain = "https://" + new_domain
    old_domain      = current_api_url.replace("/api.php", "")
    current_api_url = f"{new_domain}/api.php"
    await update.message.reply_text(
        f"✅ Đã đổi domain rút gọn!\n\n"
        f"Cũ: <code>{old_domain}</code>\n"
        f"Mới: <code>{new_domain}</code>",
        parse_mode="HTML"
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

    has_shopee  = bool(SHOPEE_DIRECT_REGEX.search(text))
    has_lazada  = bool(LAZADA_REGEX.search(text))
    has_unknown = bool(SHORT_UNKNOWN_REGEX.search(text))

    if not has_shopee and not has_lazada and not has_unknown:
        return

    await update.message.reply_text("⏳ Đang xử lý...")
    result = await process_all(text)
    await send_result(update, result)

# ============================================================
# FASTAPI
# ============================================================
ptb_app = Application.builder().token(BOT_TOKEN).updater(None).build()
ptb_app.add_handler(CommandHandler("start", cmd_start))
ptb_app.add_handler(CommandHandler("rut",   cmd_rut))
ptb_app.add_handler(CommandHandler("aff",   cmd_aff))
ptb_app.add_handler(CommandHandler("dm",    cmd_dm))
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
