"""
Bot de Telegram para Cines Unidos Maturín.
Vigila la página de la película y avisa cuando aparezca una FECHA concreta
con funciones a partir de cierta HORA.

Uso:
  pip install requests playwright
  playwright install chromium
  python bot_preventa.py

Comandos: /start o /menu (elegir), /estado (revisar ahora), /debug (capturas), /stop (cancelar)
"""
import json
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests
from playwright.sync_api import sync_playwright

TOKEN = os.environ.get("TELEGRAM_TOKEN", "8856533536:AAEY015XC9TkadO00a7JGttZl_3lxkIlPN0")
# IDs de chat (separados por coma) que se suscriben solos al arrancar. Úsalo en Render,
# porque el disco gratis se borra al reiniciar. Para saber tu ID, escríbele /id al bot.
CHAT_IDS = [c.strip() for c in os.environ.get("CHAT_IDS", "").split(",") if c.strip()]
INTERVALO_REVISION = 60  # segundos entre revisiones (60-120 recomendado)
STATE_FILE = "estado.json"
API = f"https://api.telegram.org/bot{TOKEN}"

# ---------------------------------------------------------------
# AVISOS DISPONIBLES EN EL MENÚ
#   url       : página de la película (la que abres al tocar el póster en Preventa)
#   dia / mes : fecha que quieres. El mes tal como lo muestra la web: "dic", "ene", ...
#   hora_min  : hora mínima en formato 24h (18 = 6:00 pm). 0 = cualquier hora.
# ---------------------------------------------------------------
URL_AVENGERS = "https://www.cinesunidos.com/pelicula/HO00006062?city=matur%C3%ADn"

PELICULAS = {
    "Avengers: Doomsday - jue 17 dic desde 6pm": {
        "url": URL_AVENGERS, "dia": 17, "mes": "dic", "hora_min": 18,
    },
    "PRUEBA: Avengers - mié 16 dic (cualquier hora)": {
        "url": URL_AVENGERS, "dia": 16, "mes": "dic", "hora_min": 0,
    },
}
NOMBRES = list(PELICULAS)
DEBUG_PELI = NOMBRES[-1]  # /debug usa esta (la de prueba, porque esa fecha ya existe)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def tg(method, **params):
    try:
        return requests.post(f"{API}/{method}", json=params, timeout=40).json()
    except Exception as e:
        print("Error Telegram:", e)
        return {}


# ---------- servidor web + auto-ping (para que Render no duerma el servicio) ----------
class _Ping(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    do_HEAD = do_GET

    def log_message(self, *a):
        pass


def iniciar_keepalive():
    port = int(os.environ.get("PORT", "10000"))
    threading.Thread(target=lambda: HTTPServer(("0.0.0.0", port), _Ping).serve_forever(), daemon=True).start()
    url = os.environ.get("RENDER_EXTERNAL_URL")
    if url:
        def loop():
            while True:
                time.sleep(240)  # cada 4 min
                try:
                    requests.get(url, timeout=20)
                except Exception:
                    pass
        threading.Thread(target=loop, daemon=True).start()


# ---------- estado persistente ----------
def cargar():
    if os.path.exists(STATE_FILE):
        st = json.load(open(STATE_FILE))
        for k in list(st["subs"]):
            st["subs"][k] = [n for n in st["subs"][k] if n in PELICULAS]
        return st
    return {"subs": {}, "avisados": []}


def guardar(st):
    json.dump(st, open(STATE_FILE, "w"), ensure_ascii=False)


# ---------- horas ----------
def extraer_horas(texto: str):
    """Devuelve lista ordenada de (hora24, minuto) encontradas en el texto."""
    t = texto.lower()
    out = set()
    for h, m, ap in re.findall(r"(\d{1,2}):(\d{2})\s*([ap])\.?\s*m", t):
        out.add((int(h) % 12 + (12 if ap == "p" else 0), int(m)))
    if not out:
        for h, m in re.findall(r"\b([01]?\d|2[0-3]):([0-5]\d)\b", t):
            out.add((int(h), int(m)))
    return sorted(out)


def fmt_hora(h, m):
    return f"{(h % 12) or 12}:{m:02d} {'pm' if h >= 12 else 'am'}"


# ---------- scraping ----------
def consultar(nombres, captura=False):
    """Abre la página de cada aviso, selecciona la fecha y lee los horarios."""
    res = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"])
        ctx = browser.new_context(user_agent=UA, viewport={"width": 1366, "height": 900}, locale="es-VE")
        for n in nombres:
            cfg = PELICULAS[n]
            r = {"fecha": False, "horas": [], "texto": "", "foto": None, "error": None}
            try:
                page = ctx.new_page()
                page.goto(cfg["url"], wait_until="networkidle", timeout=60000)
                page.wait_for_timeout(5000)

                # tarjeta de fecha: "mié. / dic / 16" (con o sin espacios entre las partes)
                patron = re.compile(rf"^\s*[^\d\n]*?{cfg['mes']}\.?\s*{cfg['dia']}\s*$", re.I)
                chip = next((c for c in page.get_by_text(patron).all() if c.is_visible()), None)
                if chip:
                    r["fecha"] = True
                    chip.click(timeout=5000)
                    page.wait_for_timeout(3000)
                    r["texto"] = page.inner_text("body")
                    horas = set(extraer_horas(r["texto"]))
                    if not horas:  # quizá hay que elegir formato para ver horarios
                        for fmt in ("2D", "ESP"):
                            try:
                                b = page.get_by_text(re.compile(rf"^\s*{fmt}\s*$")).first
                                if b.is_visible():
                                    b.click(timeout=3000)
                                    page.wait_for_timeout(2000)
                                    r["texto"] = page.inner_text("body")
                                    horas |= set(extraer_horas(r["texto"]))
                            except Exception:
                                pass
                    r["horas"] = sorted(horas)
                else:
                    r["texto"] = page.inner_text("body")
                if captura:
                    r["foto"] = page.screenshot(full_page=True)
                page.close()
            except Exception as e:
                r["error"] = str(e)
                print("Error en", n, ":", e)
            print(f"[scrape] {n}: fecha={r['fecha']} horas={[fmt_hora(*x) for x in r['horas']]}")
            res[n] = r
        browser.close()
    return res


def evaluar(n, r):
    """Devuelve (cumple, mensaje)."""
    cfg = PELICULAS[n]
    if r["error"]:
        return False, f"⚠️ Error revisando: {r['error'][:200]}"
    if not r["fecha"]:
        return False, f"⏳ Todavía no aparece el {cfg['dia']} {cfg['mes']}"
    buenas = [x for x in r["horas"] if x[0] >= cfg["hora_min"]]
    if buenas:
        return True, "🎬 ¡HAY FUNCIÓN! Horarios: " + ", ".join(fmt_hora(*x) for x in buenas)
    if not r["horas"]:
        return True, "🎬 ¡Ya aparece la fecha! (No pude leer las horas, revisa la página)"
    return False, ("📅 La fecha ya aparece pero solo con: " +
                   ", ".join(fmt_hora(*x) for x in r["horas"]) + " (antes de la hora que quieres)")


# ---------- menú ----------
def teclado(st, chat_id):
    elegidas = st["subs"].get(str(chat_id), [])
    filas = [[{"text": ("✅ " if n in elegidas else "⬜ ") + n, "callback_data": f"t:{i}"}]
             for i, n in enumerate(NOMBRES)]
    return {"inline_keyboard": filas}


def enviar_menu(st, chat_id):
    tg("sendMessage", chat_id=chat_id,
       text="🎬 Elige qué quieres vigilar (toca para activar/desactivar):",
       reply_markup=teclado(st, chat_id))


def revisar_y_avisar(st):
    nombres = sorted({n for l in st["subs"].values() for n in l})
    if not nombres:
        return
    res = consultar(nombres)
    for chat_id, pelis in st["subs"].items():
        for n in pelis:
            clave = f"{chat_id}|{n}"
            ok, msg = evaluar(n, res[n])
            if ok:
                if clave not in st["avisados"]:
                    tg("sendMessage", chat_id=int(chat_id),
                       text=f"🚨 {n}\n{msg}\n{PELICULAS[n]['url']}")
                    st["avisados"].append(clave)
            elif clave in st["avisados"] and not res[n]["error"]:
                st["avisados"].remove(clave)
    guardar(st)


def main():
    iniciar_keepalive()
    st = cargar()
    for cid in CHAT_IDS:
        lista = st["subs"].setdefault(cid, [])
        if NOMBRES[0] not in lista:
            lista.append(NOMBRES[0])
    guardar(st)
    offset = None
    ultimo = 0
    print("Bot iniciado. Escríbele /start en Telegram.")
    while True:
        r = tg("getUpdates", offset=offset, timeout=20)
        for u in r.get("result", []):
            offset = u["update_id"] + 1

            cb = u.get("callback_query")
            if cb:
                chat_id = cb["message"]["chat"]["id"]
                if cb["data"].startswith("t:"):
                    nombre = NOMBRES[int(cb["data"][2:])]
                    lista = st["subs"].setdefault(str(chat_id), [])
                    if nombre in lista:
                        lista.remove(nombre)
                        st["avisados"] = [a for a in st["avisados"] if a != f"{chat_id}|{nombre}"]
                        aviso = "Desactivado"
                    else:
                        lista.append(nombre)
                        aviso = "Vigilando"
                    guardar(st)
                    tg("answerCallbackQuery", callback_query_id=cb["id"], text=aviso)
                    tg("editMessageReplyMarkup", chat_id=chat_id,
                       message_id=cb["message"]["message_id"], reply_markup=teclado(st, chat_id))
                continue

            msg = u.get("message") or {}
            chat_id = msg.get("chat", {}).get("id")
            texto = (msg.get("text") or "").strip().lower()
            if not chat_id:
                continue
            if texto.startswith(("/start", "/menu")):
                enviar_menu(st, chat_id)
            elif texto.startswith("/id"):
                tg("sendMessage", chat_id=chat_id, text=f"Tu ID de chat es: {chat_id}")
            elif texto.startswith("/stop"):
                st["subs"].pop(str(chat_id), None)
                guardar(st)
                tg("sendMessage", chat_id=chat_id, text="Listo, no te enviaré más avisos. Usa /menu para volver.")
            elif texto.startswith("/debug"):
                try:
                    r_ = consultar([DEBUG_PELI], captura=True)[DEBUG_PELI]
                    resumen = (f"Fecha encontrada: {'sí' if r_['fecha'] else 'NO'}\n"
                               f"Horas leídas: {[fmt_hora(*x) for x in r_['horas']]}\n"
                               f"Error: {r_['error']}\n\n"
                               f"Texto de la página:\n{r_['texto'][:1800]}")
                    tg("sendMessage", chat_id=chat_id, text=resumen)
                    if r_["foto"]:
                        requests.post(f"{API}/sendPhoto", data={"chat_id": chat_id, "caption": "Captura tras elegir la fecha"},
                                      files={"photo": ("captura.png", r_["foto"])}, timeout=60)
                except Exception as e:
                    tg("sendMessage", chat_id=chat_id, text=f"Error: {e}")
            elif texto.startswith("/estado"):
                elegidas = st["subs"].get(str(chat_id), [])
                if not elegidas:
                    tg("sendMessage", chat_id=chat_id, text="No has elegido nada. Usa /menu.")
                    continue
                res = consultar(elegidas)
                tg("sendMessage", chat_id=chat_id,
                   text="\n\n".join(f"{n}\n{evaluar(n, res[n])[1]}" for n in elegidas))

        if time.time() - ultimo >= INTERVALO_REVISION:
            ultimo = time.time()
            try:
                revisar_y_avisar(st)
            except Exception as e:
                print("Error revisando la web:", e)


if __name__ == "__main__":
    main()
