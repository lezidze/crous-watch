#!/usr/bin/env python3
"""
crous_watch — следит за поиском на trouverunlogement.lescrous.fr
и присылает в Telegram новые предложения жилья.

  python crous_watch.py                      одна проверка (cron / GitHub Actions)
  CHECK_INTERVAL=120 python crous_watch.py   бесконечный цикл, проверка раз в 120 с
  python crous_watch.py chat-id              показать твой chat_id
"""
from __future__ import annotations

import html
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup


def load_dotenv(path: str = ".env") -> None:
    """Мини-загрузчик .env для локального запуска."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


load_dotenv()

DEFAULT_URL = (
    "https://trouverunlogement.lescrous.fr/tools/48/search"
    "?bounds=2.224122_48.902156_2.4697602_48.8155755&locationName=Paris"
)
SEARCH_URL = os.environ.get("CROUS_URL") or DEFAULT_URL
TG_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TG_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
STATE_FILE = Path(os.environ.get("STATE_FILE") or "state.json")
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL") or 0)
if 0 < CHECK_INTERVAL < 60:
    CHECK_INTERVAL = 60  # не долбим сайт чаще раза в минуту

MAX_PAGES = 20
ALERT_AFTER_FAILURES = 5

ACC_RE = re.compile(r"/accommodations/(\d+)")
COUNT_RE = re.compile(r"(\d+)\s+logements?\s+trouvés?", re.IGNORECASE)
EMPTY_RE = re.compile(r"aucun\s+logement", re.IGNORECASE)

http = requests.Session()
http.headers["User-Agent"] = "crous-watch/1.0 (personal housing alert)"


class PageError(Exception):
    """Страница выглядит не так, как ожидалось (вёрстка, техработы, блокировка)."""


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def clean(s: str) -> str:
    return " ".join(s.split())


# ---------------------------------------------------------------- парсинг

def get_html(url: str) -> str:
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            r = http.get(url, timeout=30)
            r.raise_for_status()
            return r.text
        except requests.RequestException as exc:
            last_exc = exc
            time.sleep(5 * (attempt + 1))
    raise PageError(f"не удалось загрузить страницу: {last_exc}")


def acc_id_of(a) -> str | None:
    m = ACC_RE.search(a.get("href", ""))
    return m.group(1) if m else None


def find_card(link, acc_id: str):
    """Самый широкий предок ссылки, где нет других предложений, = карточка."""
    card = link
    for parent in link.parents:
        if parent.name in ("main", "body", "html", "[document]"):
            break
        if any(acc_id_of(a) not in (None, acc_id) for a in parent.find_all("a", href=True)):
            break
        card = parent
        classes = parent.get("class") or []
        if parent.name in ("li", "article") or "fr-card" in classes or "card" in classes:
            break
    return card


def parse_offer(link, acc_id: str, page_url: str) -> dict:
    card = find_card(link, acc_id)
    lines = [clean(t) for t in card.get_text("\n").split("\n") if t.strip()]
    name = clean(link.get_text()) or f"Logement #{acc_id}"

    def first(pred) -> str:
        return next((l for l in lines if l != name and pred(l)), "")

    return {
        "name": name,
        "url": urljoin(page_url, link["href"]),
        "address": first(lambda l: re.search(r"\b\d{5}\b", l)),
        "area": first(lambda l: "m²" in l),
        "price": first(lambda l: "€" in l),
    }


def parse_page(page_html: str, page_url: str):
    soup = BeautifulSoup(page_html, "html.parser")

    # на одно предложение может быть несколько ссылок (фото, заголовок) — берём с текстом
    links: dict[str, object] = {}
    for a in soup.find_all("a", href=ACC_RE):
        acc_id = acc_id_of(a)
        best = links.get(acc_id)
        if best is None or len(clean(a.get_text())) > len(clean(best.get_text())):
            links[acc_id] = a
    offers = {i: parse_offer(a, i, page_url) for i, a in links.items()}

    text = clean(soup.get_text(" "))
    m = COUNT_RE.search(text)
    total = int(m.group(1)) if m else (0 if EMPTY_RE.search(text) else None)

    next_url = None
    for a in soup.find_all("a", href=True):
        label = " ".join([a.get_text(" "), a.get("title", ""), a.get("aria-label", "")]).lower()
        if "suivante" in label and a["href"] not in ("", "#"):
            next_url = urljoin(page_url, a["href"])
            break
    return offers, total, next_url


def fetch_offers() -> dict[str, dict]:
    offers: dict[str, dict] = {}
    url, visited, total = SEARCH_URL, set(), None
    while url and url not in visited and len(visited) < MAX_PAGES:
        visited.add(url)
        page_offers, page_total, url = parse_page(get_html(url), url)
        if total is None:
            total = page_total
        if not page_offers:
            break
        offers.update(page_offers)

    if total is None and not offers:
        raise PageError("нет ни предложений, ни счётчика — сайт мог поменять вёрстку")
    if total and not offers:
        raise PageError(f"сайт пишет «{total} logements trouvés», но ничего не распознано")
    if total and len(offers) < total:
        print(f"[warn] распознано {len(offers)} из {total}", file=sys.stderr)
    return offers


# ---------------------------------------------------------------- telegram

def tg(method: str, **payload):
    if not TG_TOKEN:
        raise RuntimeError("TELEGRAM_TOKEN не задан")
    try:
        r = http.post(f"https://api.telegram.org/bot{TG_TOKEN}/{method}", json=payload, timeout=30)
        data = r.json()
    except (requests.RequestException, ValueError) as exc:
        # исходную ошибку не показываем: в её тексте URL с токеном
        raise RuntimeError(f"Telegram: ошибка запроса ({type(exc).__name__})") from None
    if not data.get("ok"):
        raise RuntimeError(f"Telegram API {r.status_code}: {data.get('description')}")
    return data["result"]


def send(text: str) -> None:
    if not (TG_TOKEN and TG_CHAT_ID):
        print("[telegram не настроен]\n" + text + "\n")
        return
    tg("sendMessage", chat_id=TG_CHAT_ID, text=text, parse_mode="HTML",
       disable_web_page_preview=True)
    time.sleep(1)  # лимит Telegram ~1 сообщение/с в один чат


def send_chunks(header: str, blocks: list[str]) -> None:
    msg = header
    for b in blocks:
        if len(msg) + len(b) + 2 > 4000:  # лимит сообщения 4096 символов
            send(msg)
            msg = ""
        msg += ("\n\n" if msg else "") + b
    if msg:
        send(msg)


def fmt_offer(o: dict) -> str:
    e = html.escape
    parts = [f"<b>{e(o['name'])}</b>"]
    if o.get("address"):
        parts.append(e(o["address"]))
    details = " · ".join(x for x in (o.get("area"), o.get("price")) if x)
    if details:
        parts.append(e(details))
    parts.append(f'<a href="{e(o["url"])}">Открыть на сайте CROUS</a>')
    return "\n".join(parts)


# ---------------------------------------------------------------- логика

def load_state() -> dict | None:
    if not STATE_FILE.exists():
        return None
    return json.loads(STATE_FILE.read_text(encoding="utf-8"))


def save_state(offers: dict) -> None:
    STATE_FILE.write_text(
        json.dumps({"offers": offers}, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def check() -> None:
    offers = fetch_offers()
    state = load_state()

    if state is None:
        send_chunks(f"✅ Мониторинг CROUS запущен. Сейчас предложений: {len(offers)}.",
                    [fmt_offer(o) for o in offers.values()])
    else:
        # сравниваем с прошлой проверкой, а не со «всеми когда-либо виденными»:
        # если жильё исчезло и снова появилось (кто-то отказался) — это тоже новость
        prev = state.get("offers", {})
        new_ids = [i for i in offers if i not in prev]
        for i in new_ids:
            send("🏠 <b>Новое предложение CROUS!</b>\n\n" + fmt_offer(offers[i]))
        gone = len(set(prev) - set(offers))
        print(f"{now()} предложений: {len(offers)}, новых: {len(new_ids)}, исчезло: {gone}")

    save_state(offers)  # сохраняем только после успешной отправки


def print_chat_ids() -> None:
    updates = tg("getUpdates")
    chats = {}
    for u in updates:
        chat = (u.get("message") or {}).get("chat")
        if chat:
            chats[chat["id"]] = chat.get("first_name") or chat.get("title") or ""
    if not chats:
        print("Сообщений нет. Напиши боту что-нибудь в Telegram и запусти снова.")
    for cid, name in chats.items():
        print(f"chat_id = {cid}   ({name})")


def main() -> int:
    if sys.argv[1:] == ["chat-id"]:
        print_chat_ids()
        return 0

    if CHECK_INTERVAL <= 0:
        try:
            check()
            return 0
        except Exception as exc:
            print(f"{now()} ошибка: {exc}", file=sys.stderr)
            return 1

    print(f"Проверка каждые {CHECK_INTERVAL} с. Ctrl+C — выход.")
    failures = 0
    while True:
        try:
            check()
            failures = 0
        except Exception as exc:
            failures += 1
            print(f"{now()} ошибка: {exc}", file=sys.stderr)
            if failures == ALERT_AFTER_FAILURES:
                try:
                    send(f"⚠️ crous-watch: {failures} ошибок подряд\n{html.escape(str(exc))}")
                except Exception:
                    pass
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    sys.exit(main())
