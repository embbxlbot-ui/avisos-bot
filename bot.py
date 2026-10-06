"""
tweet2insta v2

1) Tweets: le mandas el enlace a tu bot de Telegram -> captura -> se publica en Instagram.
2) Fuentes web (RSS o páginas vigiladas, en sources.json): el bot detecta novedades,
   genera una tarjeta y te la propone por Telegram con botones Publicar / Descartar.

Pensado para ejecutarse en GitHub Actions cada 10 minutos.
"""
import os, re, json, time, html, hashlib, pathlib, subprocess
from datetime import datetime
from urllib.parse import urljoin

import requests
import feedparser
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright
from PIL import Image

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


def to_instagram(png, jpg, W=1080, H=1350, margin=60):
    """Centra la captura en un lienzo 4:5. Instagram solo acepta JPEG."""
    im = Image.open(png).convert("RGB")
    scale = min((W - 2 * margin) / im.width, (H - 2 * margin) / im.height)
    im = im.resize((int(im.width * scale), int(im.height * scale)), Image.LANCZOS)
    canvas = Image.new("RGB", (W, H), (255, 255, 255))
    canvas.paste(im, ((W - im.width) // 2, (H - im.height) // 2))
    canvas.save(jpg, "JPEG", quality=92)


CARD_TEMPLATE = """<!doctype html><html><head><meta charset="utf-8"><style>
* { box-sizing: border-box; margin: 0; }
body { width: 1080px; height: 1350px; background: #FBF6EC; padding: 90px 80px;
       font-family: 'DejaVu Sans', 'Liberation Sans', Arial, sans-serif;
       display: flex; flex-direction: column; }
.bar { height: 18px; width: 220px; border-radius: 9px;
       background: linear-gradient(90deg, #C8102E 0 50%, #F1BF00 50% 100%); }
.src { margin-top: 50px; font-size: 32px; font-weight: 700; color: #C8102E;
       text-transform: uppercase; letter-spacing: 2px; line-height: 1.3; }
.title { margin-top: 40px; font-size: __SIZE__px; line-height: 1.22; font-weight: 700;
         color: #1d1d1f; display: -webkit-box; -webkit-line-clamp: 10;
         -webkit-box-orient: vertical; overflow: hidden; }
.date { margin-top: 40px; font-size: 30px; color: #6b6b6b; }
.foot { margin-top: auto; padding-top: 30px; border-top: 2px solid #e3dccd;
        font-size: 28px; color: #6b6b6b; }
</style></head><body>
<div class="bar"></div>
<div class="src">__SRC__</div>
<div class="title">__TITLE__</div>
<div class="date">__DATE__</div>
<div class="foot">Cuenta no oficial · Más información en la fuente oficial</div>
</body></html>"""


def render_card(source_name, title, out_jpg):
    size = 64 if len(title) <= 110 else 52 if len(title) <= 200 else 44
    page_html = (CARD_TEMPLATE
                 .replace("__SIZE__", str(size))
                 .replace("__SRC__", html.escape(source_name))
                 .replace("__TITLE__", html.escape(title))
                 .replace("__DATE__", datetime.now().strftime("%d/%m/%Y")))
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1080, "height": 1350})
        page.set_content(page_html)
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
def handle_tweet(text):
    m = TWEET_RE.search(text)
    if not m:
        return
    user, tid = m.group(1), m.group(2)
    url = f"https://x.com/{user}/status/{tid}"
    extra = TWEET_RE.sub("", text).strip()   # texto junto al enlace = pie de foto
    caption = (extra + "\n\n" if extra else "") + f"Fuente: @{user} en X" + DISCLAIMER
    try:
        png, jpg = IMG_DIR / f"{tid}.png", IMG_DIR / f"{tid}.jpg"
        capture_tweet(url, png)
        to_instagram(png, jpg)
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

    try:
        tg("answerCallbackQuery", callback_query_id=cq["id"], text=result[:190])
    except Exception:
        pass   # Telegram rechaza respuestas a botones pulsados hace rato; no importa
    if pid not in state["pending"]:   # quita los botones salvo si hay que reintentar
        try:
            tg("editMessageReplyMarkup", chat_id=ALLOWED_CHAT, message_id=msg["message_id"],
               reply_markup={"inline_keyboard": []})
        except Exception:
            pass
    say(result, reply_to_message_id=msg.get("message_id"))


def handle_updates(state):
    updates = tg("getUpdates", offset=state["offset"], timeout=0)["result"]
    for u in updates:
        state["offset"] = u["update_id"] + 1
        if "callback_query" in u:
            handle_callback(u["callback_query"], state)
        elif "message" in u:
            msg = u["message"]
            if str(msg.get("chat", {}).get("id")) == ALLOWED_CHAT:
                handle_tweet(msg.get("text", ""))


# ---------------------------------------------------------------- fuentes web
def fetch_rss(src):
    r = requests.get(src["url"], headers=HEADERS, timeout=30)
    r.raise_for_status()
    feed = feedparser.parse(r.content)
    return [{"title": " ".join((e.get("title") or "").split()), "link": e.get("link")}
            for e in feed.entries if e.get("title") and e.get("link")]


def fetch_page(src):
    """Devuelve los enlaces de la página. Los nuevos = novedades."""
    r = requests.get(src["url"], headers=HEADERS, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.content, "html.parser")
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


def propose(src, item, state):
    pid = hashlib.sha1(item["link"].encode()).hexdigest()[:12]
    if pid in state["pending"]:   # mismo enlace aparecido en otra fuente
        return
    jpg = IMG_DIR / f"{pid}.jpg"
    render_card(src["name"], item["title"], jpg)
    caption = f"{item['title']}\n\nFuente: {src['name']}\n{item['link']}" + DISCLAIMER
    state["pending"][pid] = {"caption": caption}
    keyboard = {"inline_keyboard": [[
        {"text": "✅ Publicar", "callback_data": f"ok:{pid}"},
        {"text": "❌ Descartar", "callback_data": f"no:{pid}"},
    ]]}
    with open(jpg, "rb") as f:
        tg("sendPhoto", files={"photo": f}, chat_id=ALLOWED_CHAT,
           caption=caption[:1000], reply_markup=json.dumps(keyboard))


def
