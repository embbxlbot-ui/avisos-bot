"""
tweet2insta v2

1) Tweets: le mandas el enlace a tu bot de Telegram -> captura -> se publica en Instagram.
2) Fuentes web (RSS o páginas vigiladas, en sources.json): el bot detecta novedades,
   genera una tarjeta y te la propone por Telegram con botones Publicar / Descartar.

Pensado para ejecutarse en GitHub Actions cada 10 minutos.
"""
import os, re, json, time, html, hashlib, pathlib, subprocess
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
import feedparser
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright
from PIL import Image, ImageDraw, ImageFont

# ---------------------------------------------------------------- configuración
TG_TOKEN = os.environ["TG_TOKEN"]
ALLOWED_CHAT = str(os.environ["TG_CHAT_ID"])    # solo acepta mensajes tuyos
IG_TOKEN = os.environ["IG_TOKEN"]
IG_USER = os.environ["IG_USER_ID"]
REPO = os.environ["GITHUB_REPOSITORY"]          # lo pone GitHub: usuario/repo
IG_API = "https://graph.instagram.com/v22.0"    # sube la versión si Meta la retira

DISCLAIMER = (
    "\n\nℹ️ Cuenta no oficial. Contenido compartido de fuentes oficiales; "
    "no nos responsabilizamos de cambios o información desactualizada. "
    "Confirma siempre en los canales oficiales."
)
WATERMARK = "Compartido por @espana.bxl · Se recomienda consultar canales oficiales"
RUN_MINUTES = 25              # cuánto tiempo escucha Telegram cada ejecución
SOURCES_EVERY = 10            # cada cuántos minutos revisa las fuentes web
MAX_PROPOSALS_PER_RUN = 5       # como mucho 5 propuestas nuevas por ejecución
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; avisos-bot/1.0)"}

STATE = pathlib.Path("state.json")
SOURCES = json.loads(pathlib.Path("sources.json").read_text(encoding="utf-8"))
IMG_DIR = pathlib.Path("images")
IMG_DIR.mkdir(exist_ok=True)
TWEET_RE = re.compile(r"https?://(?:www\.|mobile\.)?(?:twitter|x)\.com/(\w+)/status/(\d+)\S*")


# ---------------------------------------------------------------- utilidades
def tg(method, files=None, **params):
    url = f"https://api.telegram.org/bot{TG_TOKEN}/{method}"
    if files:
        r = requests.post(url, data=params, files=files, timeout=60)
    else:
        r = requests.post(url, json=params, timeout=60)
    r.raise_for_status()
    return r.json()


def say(text, **extra):
    tg("sendMessage", chat_id=ALLOWED_CHAT, text=text[:4000], **extra)


def git_push(message):
    subprocess.run(["git", "add", "-A"], check=True)
    if subprocess.run(["git", "diff", "--cached", "--quiet"]).returncode == 0:
        return
    subprocess.run(["git", "commit", "-m", message], check=True)
    for attempt in range(3):
        # se sincroniza con lo último del repositorio; si hay choque, ganan los cambios del bot
        subprocess.run(["git", "pull", "--rebase", "-X", "theirs"])
        if subprocess.run(["git", "push"]).returncode == 0:
            return
        time.sleep(5)
    raise RuntimeError("No se pudo subir al repositorio. Revisa los permisos de Actions (Read and write).")


def image_url(name):
    """URL pública de la imagen, servida por GitHub Pages."""
    owner, repo = REPO.split("/")
    return f"https://{owner.lower()}.github.io/{repo}/images/{name}.jpg"


def wait_until_online(url, timeout=300):
    """Espera a que GitHub Pages publique la imagen (suele tardar 1-2 minutos)."""
    end = time.time() + timeout
    while time.time() < end:
        try:
            r = requests.head(url, timeout=15)
            if r.status_code == 200 and r.headers.get("content-type", "").startswith("image/"):
                return
        except requests.RequestException:
            pass
        time.sleep(10)
    raise RuntimeError(f"La imagen no está disponible en {url}. ¿Está activado GitHub Pages?")


def load_state():
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    state.setdefault("offset", 0)
    state.setdefault("seen", {})
    state.setdefault("pending", {})
    return state


# ---------------------------------------------------------------- imágenes
def capture_tweet(url, out_png):
    """Renderiza el tweet embebido oficial y le hace captura."""
    oembed = requests.get(
        "https://publish.twitter.com/oembed",
        params={"url": url, "hide_thread": "true", "dnt": "true"},
        timeout=30,
    ).json()
    page_html = (
        "<html><body style='margin:0;background:#fff;display:flex;justify-content:center'>"
        f"<div style='width:550px'>{oembed['html']}</div></body></html>"
    )
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(device_scale_factor=2, viewport={"width": 600, "height": 1400})
        page.set_content(page_html, wait_until="networkidle")
        frame = page.wait_for_selector("iframe", timeout=30000)
        page.wait_for_timeout(4000)
        frame.screenshot(path=str(out_png))
        browser.close()


def watermark_font(size):
    for path in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                 "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)


def add_watermark(jpg):
    """Marca de agua discreta en la esquina inferior derecha (capturas de tweets)."""
    im = Image.open(jpg).convert("RGBA")
    W, H = im.size
    size = 24
    while True:   # reduce la letra hasta que quepa
        font = watermark_font(size)
        x0, y0, x1, y1 = font.getbbox(WATERMARK)
        tw, th = x1 - x0, y1 - y0
        if tw + 40 <= W - 40 or size <= 14:
            break
        size -= 1
    pad_x, pad_y = 20, 10
    box_w, box_h = tw + 2 * pad_x, th + 2 * pad_y
    bx, by = W - box_w - 20, H - box_h - 14
    layer = Image.new("RGBA", im.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    draw.rounded_rectangle([bx, by, bx + box_w, by + box_h], radius=box_h // 2, fill=(15, 35, 65, 200))
    draw.text((bx + pad_x - x0, by + pad_y - y0), WATERMARK, font=font, fill=(255, 255, 255, 240))
    Image.alpha_composite(im, layer).convert("RGB").save(jpg, "JPEG", quality=92)


def to_instagram(png, jpg, W=1080, H=1350, margin=70):
    """Centra la captura en un lienzo 4:5. Instagram solo acepta JPEG."""
    im = Image.open(png).convert("RGB")
    scale = min((W - 2 * margin) / im.width, (H - 2 * margin) / im.height)
    im = im.resize((int(im.width * scale), int(im.height * scale)), Image.LANCZOS)
    canvas = Image.new("RGB", (W, H), (255, 255, 255))
    canvas.paste(im, ((W - im.width) // 2, (H - im.height) // 2))
    canvas.save(jpg, "JPEG", quality=92)


ACCOUNT = "@espana.bxl"

CARD_TEMPLATE = """<!doctype html><html><head><meta charset="utf-8">
<link href="https://fonts.googleapis.com/css2?family=Archivo:wght@500;700;800&family=Barlow+Condensed:wght@600;700;800&display=swap" rel="stylesheet">
<style>
* { box-sizing: border-box; margin: 0; }
body { width: 1080px; height: 1350px; background: #F5F3EF; color: #0F2341;
       font-family: 'Archivo', 'DejaVu Sans', sans-serif; display: flex; flex-direction: column; }
.cond { font-family: 'Barlow Condensed', 'DejaVu Sans Condensed', sans-serif; }
.top { flex-shrink: 0; background: #8E1B2C; height: 150px; padding: 0 80px; display: flex;
       align-items: center; justify-content: space-between; }
.handle { color: #FFFFFF; font-size: 52px; font-weight: 800; letter-spacing: 1px; }
.region { color: #FFFFFF; font-size: 32px; font-weight: 700; letter-spacing: 6px; }
.band { flex-shrink: 0; height: 8px; background: #C9A646; }
.main { flex: 1; min-height: 0; padding: 70px 80px 40px; display: flex; flex-direction: column; }
.tag { align-self: flex-start; background: #0F2341; color: #FFFFFF; font-size: 34px; font-weight: 700;
       letter-spacing: 3px; text-transform: uppercase; padding: 12px 34px; border-radius: 999px; }
.src { margin-top: 40px; font-size: 32px; font-weight: 700; color: #8E1B2C;
       text-transform: uppercase; letter-spacing: 2px; line-height: 1.3; }
.body { flex: 1; min-height: 0; overflow: hidden; display: flex; flex-direction: column; justify-content: center; padding: 30px 0; }
.title { font-size: __SIZE__px; line-height: 1.04; font-weight: 800; text-transform: uppercase;
         display: -webkit-box; -webkit-line-clamp: 8; -webkit-box-orient: vertical; overflow: hidden; }
.summary { margin-top: 34px; font-size: 34px; line-height: 1.4; font-weight: 500; color: #3B4658;
           display: -webkit-box; -webkit-line-clamp: 6; -webkit-box-orient: vertical; overflow: hidden; }
.summary:empty { display: none; }
.info { flex-shrink: 0; display: flex; align-items: center; justify-content: space-between; gap: 30px;
        background: #E9E5DC; border-radius: 22px; padding: 30px 36px; }
.info .lbl { font-size: 26px; font-weight: 700; letter-spacing: 3px; color: #8E1B2C; }
.info .dom { margin-top: 6px; font-size: 40px; font-weight: 800; color: #0F2341; }
.info .hint { font-size: 26px; font-weight: 500; color: #5B6577; text-align: right; line-height: 1.35; }
.bottom { flex-shrink: 0; display: flex; flex-direction: column; align-items: center; margin-top: 30px; gap: 22px; }
.date { align-self: flex-start; font-size: 28px; font-weight: 500; color: #5B6577; }
.wm { white-space: nowrap; background: rgba(15, 35, 65, 0.78); color: #FFFFFF; font-size: 20px; font-weight: 700;
      padding: 10px 22px; border-radius: 999px; }
</style></head><body>
<div class="top"><span class="handle cond">__ACCOUNT__</span><span class="region cond">BRUSELAS</span></div>
<div class="band"></div>
<div class="main">
  <span class="tag cond">__TAG__</span>
  <div class="src">__SRC__</div>
  <div class="body">
    <div class="title cond">__TITLE__</div>
    <div class="summary">__SUMMARY__</div>
  </div>
  <div class="info">
    <div><div class="lbl">MÁS INFORMACIÓN</div><div class="dom">__DOMAIN__</div></div>
    <div class="hint">Enlace completo<br>en la descripción ↓</div>
  </div>
  <div class="bottom">
    <div class="date">Aviso del __DATE__</div>
    <span class="wm">__WATERMARK__</span>
  </div>
</div>
</body></html>"""


def render_card(source_name, title, out_jpg, tag="Aviso", summary="", link=""):
    n = len(title)
    if summary:   # con resumen, el titular algo más pequeño para que quepa todo
        size = 66 if n <= 70 else 58 if n <= 130 else 50 if n <= 210 else 42
    else:
        size = 92 if n <= 70 else 78 if n <= 130 else 64 if n <= 210 else 54
    paras = [x for x in summary.split("\n\n") if x]
    short = " ".join(paras[:2])
    if len(short) > 360:
        short = short[:360].rsplit(" ", 1)[0] + "…"
    domain = urlparse(link).netloc.removeprefix("www.") or "la fuente oficial"
    page_html = (CARD_TEMPLATE
                 .replace("__SIZE__", str(size))
                 .replace("__ACCOUNT__", html.escape(ACCOUNT))
                 .replace("__WATERMARK__", html.escape(WATERMARK))
                 .replace("__TAG__", html.escape(tag))
                 .replace("__SRC__", html.escape(source_name))
                 .replace("__TITLE__", html.escape(title))
                 .replace("__SUMMARY__", html.escape(short))
                 .replace("__DOMAIN__", html.escape(domain))
                 .replace("__DATE__", datetime.now().strftime("%d/%m/%Y")))
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1080, "height": 1350})
        page.set_content(page_html, wait_until="networkidle")   # espera a la tipografía
        page.wait_for_timeout(500)
        page.screenshot(path=str(out_jpg), type="jpeg", quality=92)
        browser.close()


# ---------------------------------------------------------------- Instagram
def publish(image_url, caption):
    c = requests.post(f"{IG_API}/{IG_USER}/media",
                      data={"image_url": image_url, "caption": caption, "access_token": IG_TOKEN},
                      timeout=60).json()
    if "id" not in c:
        raise RuntimeError(c)
    for _ in range(12):
        st = requests.get(f"{IG_API}/{c['id']}",
                          params={"fields": "status_code", "access_token": IG_TOKEN}, timeout=30).json()
        if st.get("status_code") == "FINISHED":
            break
        if st.get("status_code") == "ERROR":
            raise RuntimeError(st)
        time.sleep(5)
    r = requests.post(f"{IG_API}/{IG_USER}/media_publish",
                      data={"creation_id": c["id"], "access_token": IG_TOKEN}, timeout=60).json()
    if "id" not in r:
        raise RuntimeError(r)
    return r["id"]
   # ---------------------------------------------------------------- Telegram: mensajes y botones
def tweet_info(url):
    """Texto e idioma del tweet, sacados del embed oficial de X."""
    try:
        oembed = requests.get("https://publish.twitter.com/oembed",
                              params={"url": url, "dnt": "true"}, timeout=30).json()
        p = BeautifulSoup(oembed["html"], "html.parser").find("p")
        if p is None:
            return "", None
        for a in p.find_all("a"):
            if a.get_text().startswith(("http", "pic.")):   # quita enlaces acortados
                a.decompose()
        return " ".join(p.get_text(" ").split()), p.get("lang")
    except Exception:
        return "", None


def translate(text, src_lang, dest="es"):
    """Traducción gratuita con MyMemory (sin clave). Devuelve None si falla."""
    chunks, current = [], ""
    for sentence in re.split(r"(?<=[.!?])\s+", text):   # trozos de menos de 450 bytes
        if len((current + " " + sentence).encode()) > 450 and current:
            chunks.append(current)
            current = sentence
        else:
            current = (current + " " + sentence).strip()
    if current:
        chunks.append(current)
    out = []
    for chunk in chunks:
        try:
            r = requests.get("https://api.mymemory.translated.net/get",
                             params={"q": chunk, "langpair": f"{src_lang}|{dest}"}, timeout=30).json()
            piece = (r.get("responseData") or {}).get("translatedText")
            if r.get("responseStatus") != 200 or not piece:
                return None
            out.append(html.unescape(piece))
        except Exception:
            return None
    return " ".join(out)


def handle_tweet(text):
    m = TWEET_RE.search(text)
    if not m:
        return
    user, tid = m.group(1), m.group(2)
    url = f"https://x.com/{user}/status/{tid}"
    extra = TWEET_RE.sub("", text).strip()   # texto junto al enlace = pie de foto

    translation = ""
    original, lang = tweet_info(url)
    if original and lang and lang not in ("es", "und", "zxx"):
        tr = translate(original, lang)
        if tr:
            translation = f"🇪🇸 Traducción automática:\n{tr}\n\n"
        else:
            say(f"⚠️ No se pudo traducir el tweet ({lang}). Se publica sin traducción.")

    caption = (extra + "\n\n" if extra else "") + translation + f"Fuente: @{user} en X" + DISCLAIMER
    try:
        png, jpg = IMG_DIR / f"{tid}.png", IMG_DIR / f"{tid}.jpg"
        capture_tweet(url, png)
        to_instagram(png, jpg)
        add_watermark(jpg)
        png.unlink()
        git_push(f"imagen {tid}")
        url_img = image_url(tid)
        wait_until_online(url_img)
        post_id = publish(url_img, caption)
        say(f"✅ Tweet publicado en Instagram (id {post_id})")
    except Exception as e:
        say(f"❌ Error con {url}:\n{e}")


def handle_callback(cq, state):
    msg = cq.get("message") or {}
    if str(msg.get("chat", {}).get("id")) != ALLOWED_CHAT:
        return
    action, _, pid = (cq.get("data") or "").partition(":")
    try:   # responde al momento para que Telegram deje de "pensar"
        tg("answerCallbackQuery", callback_query_id=cq["id"],
           text="⏳ Publicando… tarda 1-2 minutos" if action == "ok" else "Descartando…")
    except Exception:
        pass
    item = state["pending"].pop(pid, None)

    if item is None:
        result = "Ya estaba procesado"
    elif action == "ok":
        try:
            url_img = image_url(pid)
            wait_until_online(url_img)
            post_id = publish(url_img, item["caption"])
            result = f"✅ Publicado en Instagram (id {post_id})"
        except Exception as e:
            state["pending"][pid] = item   # se queda pendiente para reintentar
            result = f"❌ Error al publicar: {e}"
    else:
        (IMG_DIR / f"{pid}.jpg").unlink(missing_ok=True)
        result = "🗑️ Descartado"

    if pid not in state["pending"]:   # quita los botones salvo si hay que reintentar
        try:
            tg("editMessageReplyMarkup", chat_id=ALLOWED_CHAT, message_id=msg["message_id"],
               reply_markup={"inline_keyboard": []})
        except Exception:
            pass
    say(result, reply_to_message_id=msg.get("message_id"))


def handle_updates(state, wait=0):
    """Lee los mensajes y botones de Telegram. Con wait>0 espera hasta que llegue algo."""
    updates = tg("getUpdates", offset=state["offset"], timeout=wait)["result"]
    for u in updates:
        state["offset"] = u["update_id"] + 1
        if "callback_query" in u:
            handle_callback(u["callback_query"], state)
        elif "message" in u:
            msg = u["message"]
            if str(msg.get("chat", {}).get("id")) == ALLOWED_CHAT:
                handle_tweet(msg.get("text", ""))
    return len(updates)


# ---------------------------------------------------------------- fuentes web
def fetch_rss(src):
    r = requests.get(src["url"], headers=HEADERS, timeout=30)
    r.raise_for_status()
    feed = feedparser.parse(r.content)
    items = []
    for e in feed.entries:
        if e.get("title") and e.get("link"):
            summary = BeautifulSoup(e.get("summary") or "", "html.parser").get_text(" ")
            items.append({"title": " ".join(e["title"].split()), "link": e["link"],
                          "summary": " ".join(summary.split())[:900]})
    return items


def fetch_page(src):
    """Devuelve los enlaces de la página. Los nuevos = novedades."""
    r = requests.get(src["url"], headers=HEADERS, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.content, "html.parser")
    if src.get("pattern"):   # vigila un texto concreto (p. ej. la fecha de actualización)
        text = " ".join(soup.get_text(" ").split())
        m = re.search(src["pattern"], text)
        if not m:
            return []
        found = m.group(0)
        tag = hashlib.sha1(found.encode()).hexdigest()[:8]
        return [{"title": f"{src['title']}: {found}", "link": f"{src['url']}#{tag}"}]
    root = soup.select_one(src.get("selector", "body")) or soup
    items, seen_links = [], set()
    for a in root.select("a[href]"):
        title = " ".join(a.get_text(" ").split())
        href = a["href"].strip()
        if len(title) < src.get("min_len", 15) or href.startswith(("mailto:", "javascript:", "#", "tel:")):
            continue
        link = urljoin(src["url"], href)
        if link not in seen_links:
            seen_links.add(link)
            items.append({"title": title, "link": link})
    return items


def article_summary(url, max_chars=900):
    """Primeros párrafos de la página enlazada, para dar contexto al aviso."""
    try:
        r = requests.get(url, headers=HEADERS, timeout=30)
        r.raise_for_status()
        if "html" not in r.headers.get("content-type", "text/html"):
            return ""   # p. ej. un PDF
        soup = BeautifulSoup(r.content, "html.parser")
        for tag in soup(["script", "style", "nav", "header", "footer", "aside", "form", "noscript"]):
            tag.decompose()
        root = soup.find("article") or soup.find("main") or soup.body or soup
        paras, total = [], 0
        for el in root.find_all(["p", "li"]):
            t = " ".join(el.get_text(" ").split())
            low = t.lower()
            if len(t) < 60 or t in paras or "cookie" in low or "javascript" in low:
                continue
            paras.append(t)
            total += len(t)
            if total >= max_chars:
                break
        text = "\n\n".join(paras)
        if not text:
            meta = (soup.find("meta", attrs={"property": "og:description"})
                    or soup.find("meta", attrs={"name": "description"}))
            text = " ".join(((meta.get("content") if meta else "") or "").split())
        if len(text) > max_chars:
            text = text[:max_chars].rsplit(" ", 1)[0] + "…"
        return text
    except Exception:
        return ""


def fetch_stib(src):
    """Avisos de la STIB (API pública de datos abiertos, sin clave). Solo devuelve los importantes."""
    r = requests.get(src["url"], headers=HEADERS, timeout=30)
    r.raise_for_status()
    records = r.json().get("results", [])

    keywords = [k.lower() for k in src.get("keywords", [])]
    items, seen_texts = [], set()
    for rec in records:
        try:
            texts = json.loads(rec.get("content") or "[]")[0]["text"][0]
            points = json.loads(rec.get("points") or "[]")
        except (ValueError, KeyError, IndexError, TypeError):
            continue
        text = " ".join((texts.get("fr") or texts.get("en") or texts.get("nl") or "").split())
        low = text.lower()
        important = any(k in low for k in keywords) or (
            "métro" in low and len(points) >= src.get("min_points_metro", 8))
        key = hashlib.sha1(low.encode()).hexdigest()[:10]   # mismo texto en varias paradas = 1 aviso
        if not text or not important or key in seen_texts:
            continue
        seen_texts.add(key)
        items.append({"title": text, "link": f"{src['link']}#{key}"})
    return items


def propose(src, item, state):
    pid = hashlib.sha1(item["link"].encode()).hexdigest()[:12]
    if pid in state["pending"]:   # mismo enlace aparecido en otra fuente
        return
    jpg = IMG_DIR / f"{pid}.jpg"
    title, original = item["title"], ""
    if src.get("translate_from"):
        tr = translate(item["title"], src["translate_from"])
        if not tr:   # nunca se propone en francés: se reintenta en la siguiente ejecución
            raise RuntimeError("no se pudo traducir al español, se reintentará")
        title = tr
        original = f"\n\n🇪🇸 Traducción automática. Texto original: {item['title']}"
    link = item["link"].split("#")[0]
    summary = item.get("summary", "")
    if not summary and src["type"] == "page" and not src.get("pattern"):
        summary = article_summary(link)
    render_card(src["name"], title, jpg, src.get("tag", "Aviso"), summary, link)
    body = f"\n\n{summary}" if summary else ""
    caption = f"{title}{body}{original}\n\nMás información: {link}\nFuente: {src['name']}" + DISCLAIMER
    state["pending"][pid] = {"caption": caption}
    keyboard = {"inline_keyboard": [[
        {"text": "✅ Publicar", "callback_data": f"ok:{pid}"},
        {"text": "❌ Descartar", "callback_data": f"no:{pid}"},
    ]]}
    with open(jpg, "rb") as f:
        tg("sendPhoto", files={"photo": f}, chat_id=ALLOWED_CHAT,
           caption=caption[:1000], reply_markup=json.dumps(keyboard))


def check_sources(state):
    proposals = 0
    for src in SOURCES:
        if not src.get("enabled", True):
            continue
        try:
            fetch = {"rss": fetch_rss, "stib": fetch_stib}.get(src["type"], fetch_page)
            items = fetch(src)
        except Exception as e:
            print(f"[{src['id']}] no se pudo leer: {e}")
            continue

        keywords = [k.lower() for k in src.get("keywords", [])]
        if keywords and src["type"] != "stib":   # la STIB ya filtra dentro de fetch_stib
            items = [i for i in items if any(k in i["title"].lower() for k in keywords)]

        seen = state["seen"].get(src["id"])
        if seen is None and src.get("propose_on_first_run"):
            seen = []   # fuente con pocos avisos filtrados: se proponen ya los actuales
        if seen is None:
            # Primera vez que se mira esta fuente: memoriza lo que hay sin proponer nada,
            # para no recibir de golpe todo el contenido antiguo.
            state["seen"][src["id"]] = [i["link"] for i in items][-500:]
            print(f"[{src['id']}] inicializada con {len(items)} elementos")
            continue

        seen_set = set(seen)
        for item in items:
            if item["link"] in seen_set:
                continue
            if proposals >= MAX_PROPOSALS_PER_RUN:
                break   # lo que quede se propone en la siguiente ejecución
            try:
                propose(src, item, state)
                proposals += 1
            except Exception as e:
                print(f"[{src['id']}] error al proponer {item['link']}: {e}")
                continue   # no se marca como visto: se reintenta en la siguiente ejecución
            seen.append(item["link"])
            seen_set.add(item["link"])
        state["seen"][src["id"]] = seen[-500:]


# ---------------------------------------------------------------- principal
def save_and_push(state, message):
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=1))
    git_push(message)


def main():
    """Se queda escuchando Telegram unos minutos para que los botones respondan al momento.
    Las fuentes web se revisan al empezar y cada SOURCES_EVERY minutos."""
    state = load_state()
    end = time.time() + RUN_MINUTES * 60
    next_sources = 0
    while time.time() < end:
        if time.time() >= next_sources:
            check_sources(state)
            save_and_push(state, "fuentes revisadas")
            next_sources = time.time() + SOURCES_EVERY * 60
        wait = int(max(1, min(45, end - time.time())))
        if handle_updates(state, wait):
            save_and_push(state, "mensajes de Telegram procesados")
    save_and_push(state, "actualización del bot")


if __name__ == "__main__":
    main()
