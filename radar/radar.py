#!/usr/bin/env python3
"""Radar — збирає фінансові новини з обраних видань і показує їх списком.

Команди (див. `radar help`):
    radar run                  оновити новини зараз
    radar open                 відкрити сторінку зі списком
    radar gui                  вікно налаштувань
    radar sources              список видань
    radar source add URL [Назва]
    radar source remove НОМЕР|НАЗВА
    radar source on|off НОМЕР|НАЗВА
    radar topics               список тем
    radar topic add "Тема: ключ1, ключ2"
    radar topic remove НАЗВА
    radar schedule [60 | 08:00 13:00 19:00 | off]
    radar status
"""

import base64
import collections
import concurrent.futures
import math
import datetime as dt
import email.utils
import fcntl
import hashlib
import html
import html.parser
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "radar"
DATA_DIR = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "radar"
SYSTEMD_DIR = Path.home() / ".config/systemd/user"

SOURCES_FILE = CONFIG_DIR / "sources.json"
TOPICS_FILE = CONFIG_DIR / "topics.txt"
SETTINGS_FILE = CONFIG_DIR / "settings.json"
DB_FILE = DATA_DIR / "radar.db"
REPORT_FILE = DATA_DIR / "report.html"
LOCK_FILE = DATA_DIR / "radar.lock"
LOG_FILE = DATA_DIR / "radar.log"
ICON_FILE = APP_DIR / "radar.svg"

USER_AGENT = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")

DEFAULT_SETTINGS = {
    # Розклад: "interval" (кожні N хвилин), "times" (у визначений час) або "off"
    "schedule_mode": "interval",
    "interval_minutes": 60,
    "times": ["08:00", "13:00", "19:00"],
    # Скільки днів новин показувати на сторінці
    "report_days": 3,
    # Новини, старші за цю кількість годин, не вважаються новими
    "max_age_hours": 36,
    # Шукати ключі ще й у першому абзаці новини, а не лише в заголовку
    "match_summary": True,
    # Сповіщення про свіжі новини після кожного оновлення; якщо вимкнено, новини лише
    # оновлюються на сторінці. Ранковий підсумок вмикається окремо.
    "notifications": True,
    # Ранковий підсумок: одне сповіщення з головним за ніч
    "digest_enabled": True,
    "digest_time": "09:00",
}


# ---------------------------------------------------------------- налаштування

def ensure_config():
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    for name in ("sources.json", "topics.txt"):
        target = CONFIG_DIR / name
        if not target.exists():
            shutil.copy(APP_DIR / "defaults" / name, target)
    if not SETTINGS_FILE.exists():
        save_settings(DEFAULT_SETTINGS)


def load_settings():
    settings = dict(DEFAULT_SETTINGS)
    try:
        settings.update(json.loads(SETTINGS_FILE.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        pass
    settings.pop("translate", None)  # переклад тепер завжди увімкнений
    settings.pop("important_min_outlets", None)  # важливість тепер визначають самі видання
    settings.pop("notify_when_empty", None)  # сповіщення «без змін» прибрано
    return settings


def save_settings(settings):
    SETTINGS_FILE.write_text(json.dumps(settings, ensure_ascii=False, indent=2) + "\n",
                             encoding="utf-8")


def load_sources():
    return json.loads(SOURCES_FILE.read_text(encoding="utf-8"))


def save_sources(sources):
    SOURCES_FILE.write_text(json.dumps(sources, ensure_ascii=False, indent=2) + "\n",
                            encoding="utf-8")


def log(message):
    stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > 512_000:
            LOG_FILE.replace(LOG_FILE.with_suffix(".log.old"))
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(f"{stamp} {message}\n")
    except OSError:
        pass


# ---------------------------------------------------------------- теми

VOWELS = "аеєиіїоуюяьй"
APOSTROPHES = str.maketrans({"’": "'", "ʼ": "'", "`": "'", "‘": "'"})


def normalize(text):
    return (text or "").translate(APOSTROPHES).casefold()


def stem(word):
    while len(word) > 4 and word[-1] in VOWELS:
        word = word[:-1]
    return word


def keyword_regex(keyword):
    """Перетворює ключ із файлу тем на регулярний вираз."""
    keyword = normalize(keyword.strip().strip("«»\"'"))
    if not keyword:
        return None
    exact = keyword.startswith("=")
    words = keyword.lstrip("=").split()
    if not words:
        return None
    parts = []
    for i, word in enumerate(words):
        last = i == len(words) - 1
        if exact and last:
            parts.append(re.escape(word) + r"(?!\w)")
        else:
            parts.append(re.escape(stem(word)) + r"\w*")
    return re.compile(r"(?<!\w)" + r"\W+".join(parts))


Topic = collections.namedtuple("Topic", "name alternatives category")


def parse_topic_line(line):
    """«Назва: ключі» → (назва, ключі). «!» на початку (зі старіших версій) ігнорується."""
    name, _, keys = line.partition(":")
    return name.strip().lstrip("!").strip(), keys.strip()


def parse_topics(text):
    """Повертає список Topic. alternatives — зовнішній список «або», внутрішній «і»."""
    topics = []
    category = "Інше"
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            category = line[1:-1].strip() or "Інше"
            continue
        name, keys = parse_topic_line(line)
        alternatives = []
        for key in (keys or name).split(","):
            group = [keyword_regex(k) for k in re.split(r"\s+&\s+", key)]
            if group and all(group):
                alternatives.append(group)
        if name and alternatives:
            topics.append(Topic(name, alternatives, category))
    return topics


def load_topics():
    return parse_topics(TOPICS_FILE.read_text(encoding="utf-8"))


def match_topics(text, topics):
    text = normalize(text)
    return [t.name for t in topics
            if any(all(rx.search(text) for rx in group) for group in t.alternatives)]


# ---------------------------------------------------------------- завантаження

def http_get(url, timeout=25):
    request = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept-Language": "uk,en;q=0.8",
    })
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = response.read()
        charset = response.headers.get_content_charset()
    return data, charset


def parse_date(value):
    if not value:
        return None
    value = value.strip()
    try:
        parsed = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        try:
            parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def clean_text(value):
    value = re.sub(r"<[^>]+>", " ", value or "")
    return " ".join(html.unescape(value).split())


def local_tag(element):
    return element.tag.rsplit("}", 1)[-1]


def child_text(element, *names):
    for child in element:
        if local_tag(child) in names and (child.text or "").strip():
            return child.text.strip()
    return ""


def parse_feed(data):
    root = ET.fromstring(data)
    items = []
    for element in root.iter():
        tag = local_tag(element)
        if tag not in ("item", "entry"):
            continue
        title = clean_text(child_text(element, "title"))
        link = child_text(element, "link")
        if not link:  # Atom: <link href="..."/>
            for child in element:
                if local_tag(child) == "link" and child.get("href") and child.get("rel", "alternate") == "alternate":
                    link = child.get("href")
                    break
        published = parse_date(child_text(element, "pubDate", "published", "updated", "date"))
        summary = clean_text(child_text(element, "description", "summary"))[:500]
        source = child_text(element, "source")  # Google News: назва видання
        if source and title.endswith(" - " + source):
            title = title[: -len(source) - 3].rstrip()
        categories = [clean_text(c.text) for c in element if local_tag(c) == "category" and c.text]
        if "bing.com/news/apiclick" in link:  # Bing News веде через переадресацію — беремо справжнє посилання
            real = urllib.parse.parse_qs(urllib.parse.urlparse(link).query).get("url")
            if real:
                link = real[0]
        if title and link:
            items.append({"title": title, "link": link.strip(), "published": published, "summary": summary,
                          "categories": categories})
    return items


PAGE_TIME = re.compile(r"^(20\d\d-\d\d-\d\d)[ T](\d\d:\d\d)(?::\d\d)?([+-]\d\d:?\d\d|Z)?$")


class LinkCollector(html.parser.HTMLParser):
    """Збирає з HTML-сторінки посилання разом із їхнім текстом і часом публікації, якщо він стоїть
    перед посиланням (наприклад, <span itemprop="datePublished" content="2026-10-20 14:29:00">)."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []
        self._href = None
        self._text = []
        self._time = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        for value in (attrs.get("datetime"), attrs.get("content")):
            if value and PAGE_TIME.match(value.strip()):
                self._time = value.strip()
        if tag == "a":
            self._href = attrs.get("href")
            self._text = []

    def handle_data(self, data):
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._href is not None:
            text = " ".join(" ".join(self._text).split())
            self.links.append((self._href, text, self._time))
            self._href = None
            if len(text) >= 25:  # час належить лише до найближчого заголовка
                self._time = None


def decode(data, charset):
    for encoding in (charset, "utf-8", "windows-1251"):
        if encoding:
            try:
                return data.decode(encoding)
            except (LookupError, UnicodeDecodeError):
                continue
    return data.decode("utf-8", errors="replace")


def page_links(url, data, charset):
    collector = LinkCollector()
    collector.feed(decode(data, charset))
    result = []
    for href, text, moment in collector.links:
        if href and not href.startswith(("#", "javascript:", "mailto:")):
            result.append((urllib.parse.urljoin(url, href).split("#")[0], text, moment))
    return result


def page_time(value):
    """Час зі сторінки сайту; без часового поясу вважаємо його київським (місцевим)."""
    m = PAGE_TIME.match(value or "")
    if not m:
        return None
    moment = dt.datetime.fromisoformat(f"{m[1]}T{m[2]}" + ((m[3] or "").replace("Z", "+00:00")))
    if moment.tzinfo is None:
        moment = moment.astimezone()  # місцевий час комп'ютера
    return moment.astimezone(dt.timezone.utc)


EMPHASIS_CLASS = re.compile(r"bold|accent|important|urgent|breaking|highlight|hot\b|top-news|main-news", re.I)


class EmphasisCollector(html.parser.HTMLParser):
    """Знаходить посилання, які редакція виділила на сторінці: жирним, «важливо», акцентом."""

    VOID = {"br", "img", "hr", "input", "meta", "link", "source", "wbr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []        # (тег, чи виділений)
        self.links = []        # (href, текст, виділене)
        self._link = None

    def handle_starttag(self, tag, attrs):
        if tag in self.VOID:
            return
        attrs = dict(attrs)
        marked = bool(EMPHASIS_CLASS.search(attrs.get("class") or ""))
        self.stack.append((tag, marked))
        if tag == "a":
            # виділення на самому посиланні або на одному з двох-трьох найближчих блоків над ним
            near = any(m for _, m in self.stack[-4:])
            self._link = [attrs.get("href"), [], near]
        elif tag in ("b", "strong") and self._link:
            self._link[2] = True

    def handle_endtag(self, tag):
        if tag in self.VOID:
            return
        if tag == "a" and self._link:
            href, text, marked = self._link
            self.links.append((href, " ".join(" ".join(text).split()), marked))
            self._link = None
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        if self._link:
            self._link[1].append(data)


def emphasized_links(url, data, charset):
    """Посилання на новини, виділені редакцією. Якщо виділено майже все — це не сигнал."""
    collector = EmphasisCollector()
    collector.feed(decode(data, charset))
    news = [(urllib.parse.urljoin(url, h), m) for h, t, m in collector.links if h and len(t) >= 25]
    marked = {link for link, m in news if m}
    if not news or len(marked) > len(news) * 0.4:
        return set()
    return marked


# Хвости, які стрічки дописують до анонсу
LEAD_JUNK = re.compile(r"\s*(The post .*? appeared first on .*|Читати далі.*|Read more.*|Continue reading.*|"
                       r"Детальніше.*|\[…\]|\[\.\.\.\])$", re.I | re.S)


def feed_lead(source, item):
    """Перший абзац зі стрічки. None — абзацу немає, тож Radar відкриє саму статтю."""
    if source.get("type") == "html":
        return None
    lead = extract_lead(item["summary"], item["title"])
    if not lead and "news.google.com" in item["link"]:
        return ""  # статтю через Google News не відкрити
    return lead or None


def extract_lead(text, title):
    """Перший абзац новини (до ~300 знаків). Порожньо, якщо анонс лише повторює заголовок."""
    text = LEAD_JUNK.sub("", " ".join((text or "").split()))
    if len(text) < 40:
        return ""
    head = normalize(title)[:40]
    if head and normalize(text).startswith(head):
        rest = text[len(title):].strip(" -–—|:")
        if len(rest) < 40:
            return ""
        text = rest
    if len(text) > 300:
        cut = max(text.rfind(". ", 0, 300), text.rfind("! ", 0, 300), text.rfind("? ", 0, 300))
        text = text[: cut + 1] if cut > 120 else text[:300].rsplit(" ", 1)[0] + "…"
    return text


def fetch_lead(link, title):
    """Опис статті з її сторінки (og:description / description) — коли стрічка не дала анонсу."""
    if "news.google.com" in link:  # там лише загальний опис сервісу Google News
        return ""
    try:
        page = decode(*http_get(link, timeout=20))
    except Exception:
        return ""
    for name in ("og:description", "description", "twitter:description"):
        m = (re.search(r'<meta[^>]+(?:property|name)=["\']%s["\'][^>]*content=["\']([^"\']*)' % re.escape(name), page, re.I)
             or re.search(r'<meta[^>]+content=["\']([^"\']*)["\'][^>]*(?:property|name)=["\']%s["\']' % re.escape(name), page, re.I))
        if m and m.group(1).strip():
            return extract_lead(html.unescape(m.group(1)), title)
    return ""


# Позначки «терміново» в самому заголовку
URGENT_TITLE = re.compile(r"^\W*(breaking|just in|urgent|термінов\w*|блискавка|увага)\b|\bbreaking news\b", re.I)


# Не новини, а промо, відео, огляди з подій — такі матеріали не позначаємо «Важливо»
NOT_NEWS_TITLE = re.compile(r"\((?:відео|video|фото|промо)\)|за кулісами|за лаштунками|\bпромо\b|"
                            r"партнерськ\w* матеріал|на правах реклами|спецпроєкт|sponsored", re.I)
NOT_TOP = "-"  # перевірено: видання виділило, але це не новина


def passes_top_check(source, link):
    """Відкриває статтю й перевіряє, що це справжня новина (для видань із «top_verify»)."""
    rules = source.get("top_verify")
    if not rules:
        return True
    try:
        page = decode(*http_get(link, timeout=20))
    except Exception:
        return True  # не змогли перевірити — довіряємо виділенню редакції
    if rules.get("require") and rules["require"] not in page:
        return False
    return not (rules.get("reject") and rules["reject"] in page)


def link_key(url):
    """Посилання без протоколу, www, параметрів і кінцевої «/» — щоб порівнювати однакові статті."""
    parsed = urllib.parse.urlparse(url.strip())
    host = re.sub(r"^www\.", "", parsed.netloc.lower())
    query = parsed.query if host == "news.google.com" else ""
    return host + parsed.path.rstrip("/") + ("?" + query if query else "")


def editorial_signals(source):
    """Посилання, які видання саме подає як головні: {ключ посилання: причина}."""
    signals = {}
    name = source["name"]
    if source.get("top_page"):
        data, charset = http_get(source["url"])
        for link in emphasized_links(source["url"], data, charset):
            signals[link_key(link)] = f"{name} виділяє як головну"
    for feed in source.get("top_feeds", []):
        data, _ = http_get(feed)
        for item in parse_feed(data):
            signals.setdefault(link_key(item["link"]), f"на головній сторінці {name}")
    return signals


def scrape_html(source):
    data, charset = http_get(source["url"])
    pattern = re.compile(source.get("link_regex") or ".")
    best, times = {}, {}
    for link, text, moment in page_links(source["url"], data, charset):
        if len(text) < 25 or not pattern.search(link):
            continue
        # Те саме посилання буває на сторінці кілька разів — беремо найдовший текст
        if len(text) > len(best.get(link, "")):
            best[link] = text
        if moment and link not in times:
            times[link] = page_time(moment)
    # Час публікації беремо зі сторінки, якщо він там є; інакше — час, коли Radar побачив новину.
    # Дата в самому посиланні (/2026/10/19/) допомагає відсіяти старі статті.
    items = []
    for link, title in best.items():
        m = re.search(r"/(20\d\d)/(\d\d)/(\d\d)/", link)
        day = dt.date(*map(int, m.groups())) if m else None
        items.append({"title": title, "link": link, "published": times.get(link), "summary": "", "day": day})
    return items


def read_feeds(name, feeds):
    """Читає всі стрічки видання. Якщо частина не працює — бере решту; помилка лише коли не вдалося жодну."""
    items, first_error = [], None
    for feed in feeds:
        try:
            data, _ = http_get(feed)
            items.extend(parse_feed(data))
        except Exception as error:
            first_error = first_error or error
            if len(feeds) > 1:
                log(f"{name}: одна зі стрічок не відповіла ({error}): {feed[:80]}")
    if first_error and not items:
        raise first_error
    return items


def fetch_source(source):
    try:
        if source.get("type") == "html":
            items = scrape_html(source)
        else:
            items = read_feeds(source["name"], source.get("feeds", []))
    except urllib.error.HTTPError as error:
        # Деякі сайти не пускають запити з серверів (наприклад, з GitHub). Тоді беремо
        # заголовки видання із запасних стрічок — зазвичай через Google News.
        if error.code not in (401, 403, 429) or not source.get("fallback_feeds"):
            raise
        log(f"{source['name']}: сайт заблокував запит ({error.code}), беру запасні стрічки")
        return read_feeds(source["name"], source["fallback_feeds"])
    if source.get("link_contains"):
        items = [i for i in items if source["link_contains"] in i["link"]]
    return items


# ---------------------------------------------------------------- переклад

def needs_translation(title):
    letters = [c for c in title if c.isalpha()]
    if not letters:
        return False
    cyrillic = sum("Ѐ" <= c <= "ӿ" for c in letters)
    if cyrillic / len(letters) < 0.5:
        return True
    lower = title.lower()
    return bool(re.search("[ыэъё]", lower)) and not re.search("[іїєґ]", lower)


def google_translate(text):
    url = "https://translate.googleapis.com/translate_a/single?" + urllib.parse.urlencode(
        {"client": "gtx", "sl": "auto", "tl": "uk", "dt": "t", "q": text})
    data, _ = http_get(url, timeout=20)
    payload = json.loads(data)
    return "".join(part[0] for part in payload[0] if part and part[0])


def translate_titles(titles):
    """Перекладає список заголовків українською. Повертає {оригінал: переклад}."""
    result = {}
    batch, size = [], 0
    batches = []
    for title in titles:
        if size + len(title) > 1500 and batch:
            batches.append(batch)
            batch, size = [], 0
        batch.append(title)
        size += len(title) + 1
    if batch:
        batches.append(batch)
    for batch in batches:
        try:
            lines = google_translate("\n".join(batch)).split("\n")
            if len(lines) == len(batch):
                result.update({src: dst.strip() for src, dst in zip(batch, lines)})
                continue
            for title in batch:  # перекладач з'єднав або розбив рядки — перекладаємо по одному
                result[title] = google_translate(title).strip()
        except Exception as error:  # мережа недоступна — спробуємо в наступному запуску
            log(f"переклад не вдався: {error}")
            break
    return result


# ---------------------------------------------------------------- база

def open_db():
    db = sqlite3.connect(DB_FILE)
    db.row_factory = sqlite3.Row
    db.executescript("""
        CREATE TABLE IF NOT EXISTS items (
            id INTEGER PRIMARY KEY,
            link TEXT UNIQUE NOT NULL,
            source TEXT NOT NULL,
            title TEXT NOT NULL,
            title_uk TEXT,
            needs_tr INTEGER NOT NULL DEFAULT 0,
            summary TEXT,
            published TEXT,
            first_seen TEXT NOT NULL,
            topics TEXT,            -- NULL: ще не перевірено, '': не підходить
            notified INTEGER NOT NULL DEFAULT 0,
            run_id INTEGER
        );
        CREATE INDEX IF NOT EXISTS items_seen ON items(first_seen);
        CREATE TABLE IF NOT EXISTS runs (
            id INTEGER PRIMARY KEY,
            started TEXT NOT NULL,
            finished TEXT,
            new_count INTEGER,
            errors TEXT
        );
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
    """)
    columns = {row[1] for row in db.execute("PRAGMA table_info(items)")}
    if "lead" not in columns:  # перший абзац новини (NULL — ще не діставали) і його переклад
        db.execute("ALTER TABLE items ADD COLUMN lead TEXT")
        db.execute("ALTER TABLE items ADD COLUMN lead_uk TEXT")
        db.commit()
    if "top" not in columns:  # чи подає видання новину як головну (і чому)
        db.execute("ALTER TABLE items ADD COLUMN top TEXT NOT NULL DEFAULT ''")
        db.commit()
    if "imp_notified" not in columns:  # база зі старішої версії Radar
        db.execute("ALTER TABLE items ADD COLUMN imp_notified INTEGER NOT NULL DEFAULT 0")
        db.execute("UPDATE items SET imp_notified = 1")  # про старі сюжети вже не сповіщаємо
        db.commit()
    return db


def utcnow():
    return dt.datetime.now(dt.timezone.utc)


def iso(moment):
    return moment.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if moment else None


def from_iso(value):
    return dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)


# ---------------------------------------------------------------- сюжети
#
# Сюжет — одна подія, про яку написали кілька видань. Два заголовки належать до одного
# сюжету, якщо вони вийшли з різницею до доби, мають багато спільних рідкісних слів
# і хоча б один спільний «предмет»: назву, компанію, абревіатуру або число.

STORY_STOPWORDS = set("""і й та в у на з із зі до від за по про для що як це не але або ж чи вже ще має мають
може можуть буде були був була після через під над між щодо при без його її їх свої своїх які який
яка яке котрий the a an of to in on for and or is are as at by with from that this its it be has
have will after over into""".split())
STORY_SYNONYMS = {"нацба": "нбу", "мільяр": "млрд", "billio": "млрд", "мільйо": "млн", "millio": "млн",
                  "долар": "usd", "dolla": "usd", "біткой": "біткої", "bitcoi": "біткої"}
STORY_WINDOW = dt.timedelta(hours=24)
STORY_SIMILARITY = 0.45
STORY_RARE_NAME = 6  # назва вважається рідкісною, якщо трапляється не більш як у стількох заголовках


def story_tokens(title):
    """Слова заголовка (перші 6 літер), «предмети» серед них і назви.

    Назва — кілька «предметів» поспіль як одне ціле: «General Motors», «Ілон Маск», «Morgan Stanley».
    """
    text = (title or "").translate(APOSTROPHES)
    text = re.sub(r"(\d)[\s\u00a0,.](\d{3})\b", r"\1\2", text)  # 82 000 → 82000
    text = re.sub(r"(\d),(\d)", r"\1.\2", text)                    # 5,5 → 5.5
    words, subjects, names, run = set(), set(), set(), []

    def close_run():
        if run:
            names.add("_".join(run))
            run.clear()

    previous_end = 0
    for n, match in enumerate(re.finditer(r"[\w$€%.']+", text)):
        gap = text[previous_end:match.start()].strip()
        if gap and gap not in ("-", "‑", "–"):  # кома, лапки, двокрапка розривають назву; дефіс — ні
            close_run()
        previous_end = match.end()
        word = match.group().strip(".'")
        lower = word.lower()
        if not word or lower in STORY_STOPWORDS:
            close_run()
            continue
        if re.fullmatch(r"\$?[\d.]+%?", lower):
            key = lower.lstrip("$")
            words.add(key)
            subjects.add(key)
            close_run()
            continue
        if len(lower) < 3:
            close_run()
            continue
        key = STORY_SYNONYMS.get(lower[:6], lower[:6])
        words.add(key)
        if (n > 0 and word[0].isupper()) or word.isupper() or re.search("[a-z]", lower):
            subjects.add(key)
            run.append(key)
        else:
            close_run()
    close_run()
    return words, subjects, names


def build_stories(rows, source_order, half_weight):
    """Групує новини в сюжети. Сюжет важливий, якщо хоч одне видання подає його як головний
    (виділяє на сайті, ставить на головну сторінку або позначає «терміново»).

    source_order — порядок видань (перше видання стає головним у сюжеті);
    half_weight — видання, що рахуються як пів видання (профільні, які переказують одне одного);
    вага потрібна лише для сортування в ранковому підсумку.
    """
    items = []
    names_of = []
    for row in rows:
        words, subjects, names = story_tokens(row["title_uk"] or row["title"])
        items.append((row, words, subjects, from_iso(row["published"] or row["first_seen"])))
        names_of.append(names)
    name_frequency = collections.Counter(n for names in names_of for n in names)
    total = len(items) or 1
    frequency = collections.Counter(w for _, words, _, _ in items for w in words)
    idf = {w: math.log(total / c) for w, c in frequency.items()}
    common = {w for w, c in frequency.items() if c >= max(3, total * 0.05)}

    parent = list(range(len(items)))
    # Яке видання вже є в кожному сюжеті: нова публікація того самого видання — це окрема новина,
    # а не той самий сюжет (окрім точного дубля заголовка з іншої стрічки видання)
    outlets_in = [{row["source"]} for row, _, _, _ in items]
    titles_in = [{normalize(row["title"])} for row, _, _, _ in items]

    def root(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def join(a, b):
        a, b = root(a), root(b)
        if a == b:
            return
        if outlets_in[a] & outlets_in[b] and not titles_in[a] & titles_in[b]:
            return
        parent[a] = b
        outlets_in[b] |= outlets_in[a]
        titles_in[b] |= titles_in[a]

    index = collections.defaultdict(list)
    for i, (_, words, _, _) in enumerate(items):
        for w in words - common:
            index[w].append(i)
    for i, (_, words, subjects, moment) in enumerate(items):
        candidates = collections.Counter(j for w in words - common for j in index[w] if j < i)
        for j, shared_count in candidates.items():
            if shared_count < 2:
                continue
            _, other_words, other_subjects, other_moment = items[j]
            if abs(moment - other_moment) > STORY_WINDOW or not (subjects & other_subjects) - common:
                continue
            # Дві і більше спільних рідкісних назв (PZU + АМКУ, MetLife + PZU) — та сама подія,
            # навіть якщо заголовки сформульовано зовсім по-різному
            rare_names = {n for n in names_of[i] & names_of[j] if name_frequency[n] <= STORY_RARE_NAME}
            if len(rare_names) >= 2:
                join(i, j)
                continue
            shared = words & other_words
            weight = math.sqrt(sum(idf[w] for w in words) * sum(idf[w] for w in other_words)) or 1
            if sum(idf[w] for w in shared) / weight >= STORY_SIMILARITY:
                join(i, j)

    groups = collections.defaultdict(list)
    for i, item in enumerate(items):
        groups[root(i)].append(item)

    rank = {name: i for i, name in enumerate(source_order)}
    stories = []
    for members in groups.values():
        # Головна — найраніша публікація; інші видання показуються як «також»
        members.sort(key=lambda m: (m[3], rank.get(m[0]["source"], len(rank))))
        rows_ = [m[0] for m in members]
        outlets = list(dict.fromkeys(r["source"] for r in rows_))
        weight = sum(0.5 if o in half_weight else 1 for o in outlets)
        topics = list(dict.fromkeys(t for r in rows_ for t in (r["topics"] or "").split("|") if t))
        reason = next((r["top"] for r in rows_ if r["top"] and r["top"] != NOT_TOP), "")
        stories.append({
            "rows": rows_, "primary": rows_[0], "outlets": outlets, "weight": weight, "topics": topics,
            "important": bool(reason), "reason": reason,
            "moment": min(m[3] for m in members),
            "first_seen": min(r["first_seen"] for r in rows_),
        })
    stories.sort(key=lambda st: st["moment"], reverse=True)
    return stories


def stories_for(db, since, sources_all, topics, settings):
    rows = db.execute("SELECT * FROM items WHERE topics != '' AND COALESCE(published, first_seen) >= ?",
                      (iso(since),)).fetchall()
    return build_stories(
        rows,
        [s["name"] for s in sources_all],
        # Профільні видання (з власною темою, як крипто-сайти) часто переказують одне одного,
        # тому кожне з них рахується як пів видання
        {s["name"] for s in sources_all if s.get("all_news") and s.get("all_news_topic")})


# ---------------------------------------------------------------- запуск

def run(notify=True, verbose=True, wait=False):
    ensure_config()
    lock = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX if wait else fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        if verbose:
            print("Radar вже оновлює новини — зачекайте хвилину.")
        return None

    settings = load_settings()
    sources = [s for s in load_sources() if s.get("enabled", True)]
    topics = load_topics()
    db = open_db()
    now = utcnow()
    run_id = db.execute("INSERT INTO runs(started) VALUES (?)", (iso(now),)).lastrowid
    db.commit()

    # 1. Завантажуємо всі видання паралельно
    errors = {}
    fetched = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        signal_jobs = {pool.submit(editorial_signals, s): s for s in sources
                       if s.get("top_page") or s.get("top_feeds")}
        futures = {pool.submit(fetch_source, s): s for s in sources}
        for future in concurrent.futures.as_completed(futures):
            source = futures[future]
            try:
                fetched[source["name"]] = future.result()
            except Exception as error:
                errors[source["name"]] = friendly_error(error)
                log(f"{source['name']}: {error}")
        # Що видання подають як головне (виділення на сайті, головна сторінка)
        signals = {}
        for future in concurrent.futures.as_completed(signal_jobs):
            try:
                for key, reason in future.result().items():
                    signals.setdefault(key, reason)
            except Exception as error:  # без сигналів новини все одно з'являться, лише без позначки
                log(f"{signal_jobs[future]['name']}: головні новини не отримано: {error}")

    # 2. Додаємо нові посилання в базу
    max_age = now - dt.timedelta(hours=settings["max_age_hours"])
    added = 0
    by_name = {s["name"]: s for s in sources}
    to_verify = []  # (посилання, видання, причина) — виділені новини, які треба відкрити й перевірити
    for name, items in fetched.items():
        hot_categories = set(by_name[name].get("top_categories", []))
        for item in items:
            top = signals.get(link_key(item["link"]), "")
            if URGENT_TITLE.search(item["title"]):
                top = f"{name} подає як термінову"
            hot = hot_categories.intersection(item.get("categories", []))
            if hot and not top:
                top = f"{name}: «{sorted(hot)[0]}»"
            if top and NOT_NEWS_TITLE.search(item["title"]):
                top = NOT_TOP
            elif top and by_name[name].get("top_verify"):
                to_verify.append((item["link"], name, top))
                top = ""
            published = item["published"]
            if published and published > now:
                published = now
            day = item.get("day")
            too_old = bool(published and published < max_age) or bool(day and day < max_age.date())
            cursor = db.execute(
                "INSERT OR IGNORE INTO items(link, source, title, needs_tr, summary, published,"
                " first_seen, topics, notified, run_id, top, lead) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (item["link"], name, item["title"], int(needs_translation(item["title"])),
                 item["summary"], iso(published), iso(now),
                 "" if too_old else None, int(too_old), run_id, top,
                 feed_lead(by_name[name], item)))
            added += cursor.rowcount
            if published and not cursor.rowcount:  # час публікації з'явився пізніше (зі сторінки сайту)
                db.execute("UPDATE items SET published = ? WHERE link = ? AND published IS NULL",
                           (iso(published), item["link"]))
            if top and not cursor.rowcount:  # редакція виділила новину вже після публікації
                db.execute("UPDATE items SET top = ? WHERE link = ? AND top = ''", (top, item["link"]))
    # Перевіряємо виділені новини, які ще не перевіряли: чи це справжня новина, а не промо чи відео
    pending = [(link, name, reason) for link, name, reason in to_verify
               if db.execute("SELECT top FROM items WHERE link = ?", (link,)).fetchone()[0] == ""]
    if pending:
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            verdicts = pool.map(lambda job: passes_top_check(by_name[job[1]], job[0]), pending)
            for (link, _name, reason), ok in zip(pending, list(verdicts)):
                db.execute("UPDATE items SET top = ? WHERE link = ?", (reason if ok else NOT_TOP, link))
    # Перший абзац: зі стрічки, а якщо там його немає (або видання читається зі сторінки) —
    # з опису на сторінці самої статті. Однаково для всіх видань.
    recent_cut = iso(max_age)
    for row in db.execute("SELECT id, title, summary, source FROM items WHERE lead IS NULL AND"
                          " (published IS NULL OR published >= ?) AND first_seen >= ?",
                          (recent_cut, iso(now - dt.timedelta(days=settings["report_days"])))).fetchall():
        lead = feed_lead(by_name.get(row["source"], {}), {"summary": row["summary"], "title": row["title"],
                                                           "link": db.execute("SELECT link FROM items WHERE id = ?", (row["id"],)).fetchone()[0]})
        if lead is not None:
            db.execute("UPDATE items SET lead = ? WHERE id = ?", (lead, row["id"]))
    to_fetch = db.execute("SELECT id, link, title FROM items WHERE lead IS NULL AND first_seen >= ?"
                          " AND (published IS NULL OR published >= ?)", (recent_cut, recent_cut)).fetchall()
    if to_fetch:
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            leads = list(pool.map(lambda r: fetch_lead(r["link"], r["title"]), to_fetch))
        for row, lead in zip(to_fetch, leads):
            # теми перевіряємо заново вже з першим абзацом
            db.execute("UPDATE items SET lead = ?, topics = NULL WHERE id = ?", (lead, row["id"]))
    db.commit()

    # Головні новини з окремих стрічок (наприклад, головна сторінка NYT), які вже є в базі
    if signals:
        recent = db.execute("SELECT id, link, title, source FROM items WHERE top = '' AND first_seen >= ?",
                            (iso(max_age),)).fetchall()
        for row in recent:
            reason = signals.get(link_key(row["link"]))
            if reason and not by_name.get(row["source"], {}).get("top_verify"):
                db.execute("UPDATE items SET top = ? WHERE id = ?",
                           (NOT_TOP if NOT_NEWS_TITLE.search(row["title"]) else reason, row["id"]))
    db.commit()

    # 3. Перекладаємо нові заголовки іншими мовами
    #    Сюди потрапляють і заголовки, пропущені раніше (переклад був вимкнений або сервіс
    #    не відповідав), — за весь період, що показується на сторінці.
    window = iso(now - dt.timedelta(days=settings["report_days"]))
    pending = db.execute(
        "SELECT id, title FROM items WHERE needs_tr = 1 AND title_uk IS NULL AND first_seen >= ?"
        " AND (published IS NULL OR published >= ?)", (window, iso(max_age))).fetchall()
    if pending:
        translated = translate_titles([row["title"] for row in pending])
        for row in pending:
            if row["title"] in translated:
                # Теми перевіряємо заново вже з перекладом; повторного сповіщення не буде
                db.execute("UPDATE items SET title_uk = ?, topics = NULL WHERE id = ?",
                           (translated[row["title"]], row["id"]))
        db.commit()
    pending = db.execute(
        "SELECT id, lead FROM items WHERE needs_tr = 1 AND lead != '' AND lead_uk IS NULL AND first_seen >= ?"
        " AND (published IS NULL OR published >= ?)", (window, iso(max_age))).fetchall()
    if pending and settings["match_summary"]:
        translated = translate_titles([row["lead"] for row in pending])
        for row in pending:
            if row["lead"] in translated:
                db.execute("UPDATE items SET lead_uk = ?, topics = NULL WHERE id = ?",
                           (translated[row["lead"]], row["id"]))
        db.commit()

    # 4. Перевіряємо теми. Якщо змінилися теми або режим «усі новини» в якомусь виданні —
    #    переперевіряємо новини за останні дні.
    all_news = {s["name"]: s.get("all_news_topic") or "Усі новини видання"
                for s in sources if s.get("all_news")}
    topics_hash = hashlib.sha1(TOPICS_FILE.read_bytes()
                               + json.dumps([all_news, settings["match_summary"]], sort_keys=True).encode()).hexdigest()
    old_hash = db.execute("SELECT value FROM meta WHERE key = 'topics_hash'").fetchone()
    if old_hash and old_hash[0] != topics_hash:
        db.execute("UPDATE items SET topics = NULL WHERE first_seen >= ? AND"
                   " (published IS NULL OR published >= ?)", (window, iso(max_age)))
    db.execute("INSERT OR REPLACE INTO meta VALUES ('topics_hash', ?)", (topics_hash,))

    give_up = iso(now - dt.timedelta(hours=6))
    for row in db.execute("SELECT * FROM items WHERE topics IS NULL").fetchall():
        lead_missing = settings["match_summary"] and row["lead"] and not row["lead_uk"]
        if row["needs_tr"] and (not row["title_uk"] or lead_missing) and row["first_seen"] > give_up:
            continue  # переклад ще не вдався — спробуємо наступного разу
        found = match_topics(" \n ".join(filter(None, [row["title_uk"], row["title"]])), topics)
        lead = (row["lead_uk"] or row["lead"]) if settings["match_summary"] else ""
        if lead:
            # Перший абзац довший і багатослівніший за заголовок: одне випадкове слово там
            # («дата-центр», «менеджер») ще не робить новину фінансовою. Тому з абзацу беремо
            # теми, лише коли він сам збігається щонайменше з двома різними темами.
            from_lead = [t for t in match_topics(lead + " \n " + (row["lead"] or ""), topics) if t not in found]
            if len(from_lead) >= 2:
                found += from_lead
        if not found and row["source"] in all_news:
            found = [all_news[row["source"]]]
        db.execute("UPDATE items SET topics = ? WHERE id = ?", ("|".join(found), row["id"]))
    db.commit()

    # 5. Сюжети: нові (усі їхні новини ще не показані) і ті, що щойно стали важливими
    sources_all = load_sources()
    stories = stories_for(db, now - dt.timedelta(hours=settings["max_age_hours"]), sources_all, topics, settings)
    fresh = [st for st in stories if all(not r["notified"] for r in st["rows"])]
    alerts = [st for st in stories if st["important"] and not any(r["imp_notified"] for r in st["rows"])]
    for st in alerts:
        db.executemany("UPDATE items SET imp_notified = 1 WHERE id = ?", [(r["id"],) for r in st["rows"]])
    db.execute("UPDATE items SET notified = 1 WHERE topics IS NOT NULL AND notified = 0")
    db.execute("UPDATE runs SET finished = ?, new_count = ?, errors = ? WHERE id = ?",
               (iso(utcnow()), len(fresh), json.dumps(errors, ensure_ascii=False), run_id))
    db.execute("DELETE FROM items WHERE first_seen < ?", (iso(now - dt.timedelta(days=60)),))
    db.execute("DELETE FROM runs WHERE started < ?", (iso(now - dt.timedelta(days=60)),))
    db.commit()

    write_report(db, settings, errors, sources_all, topics)
    db.close()

    log(f"запуск {run_id}: отримано {sum(len(v) for v in fetched.values())}, нових посилань {added},"
        f" нових сюжетів {len(fresh)}, важливих {len(alerts)}, помилок {len(errors)}")
    if verbose:
        print(f"Свіжих публікацій за вашими темами: {len(fresh)}")
        for st in alerts:
            print(f"  ★ ВАЖЛИВО ({st['reason']}): {story_title(st)}")
        for st in fresh[:15]:
            print(f"  • [{', '.join(st['outlets'])}] {story_title(st)}")
        if len(fresh) > 15:
            print(f"  … і ще {len(fresh) - 15}")
        for name, error in errors.items():
            print(f"  ! {name}: не вдалося отримати ({error})")
        print(f"Список: {REPORT_FILE}")
    if notify and near_digest(settings):
        notify = False  # за кілька хвилин прийде ранковий підсумок — він усе й покаже
    if notify and fresh and settings["notifications"]:
        notify_fresh(fresh, errors)
    return {"new": [story_summary(st) for st in fresh], "important": [story_summary(st) for st in alerts],
            "errors": errors}


def story_title(story):
    row = story["primary"]
    return row["title_uk"] or row["title"]


def story_summary(story):
    return {"source": ", ".join(story["outlets"]), "title": story_title(story),
            "title_uk": story_title(story), "reason": story["reason"]}


# ---------------------------------------------------------------- сповіщення

def plural(n, one, few, many):
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def plural_count(n, one, few, many):
    return f"{n} {plural(n, one, few, many)}"


def publications(n):
    """«1 свіжа публікація», «3 свіжі публікації», «7 свіжих публікацій»."""
    return f"{n} {plural(n, 'свіжа публікація', 'свіжі публікації', 'свіжих публікацій')}"


NOTIFY_STATE = DATA_DIR / "notification.json"


def send_notification(summary, body, urgency="normal"):
    """Показує сповіщення окремим процесом: він чекає на натискання, поки сповіщення лежить у треї."""
    subprocess.Popen([sys.executable, str(APP_DIR / "radar.py"), "notify", urgency, summary, body],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)


def notify_and_wait(urgency, summary, body):
    """Надсилає сповіщення через D-Bus і відкриває стрічку, коли на нього натиснуть.

    Клік по самому сповіщенню (спливаючому чи в треї) — дія «default», кнопка — «open».
    Сповіщення не зникає саме, тож лишається в треї; нове сповіщення Radar замінює попереднє,
    а процес, що чекав на попереднє, завершується.
    """
    import signal
    from gi.repository import Gio, GLib

    previous = {}
    try:
        previous = json.loads(NOTIFY_STATE.read_text(encoding="utf-8"))
        os.kill(int(previous.get("pid", 0)), signal.SIGTERM)  # старий процес більше не потрібен
    except (OSError, ValueError, TypeError):
        pass

    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    hints = {"urgency": GLib.Variant("y", {"low": 0, "normal": 1, "critical": 2}.get(urgency, 1)),
             "desktop-entry": GLib.Variant("s", "radar")}
    result = bus.call_sync(
        "org.freedesktop.Notifications", "/org/freedesktop/Notifications", "org.freedesktop.Notifications",
        "Notify",
        GLib.Variant("(susssasa{sv}i)", ("Radar", int(previous.get("id", 0)), str(ICON_FILE), summary,
                                          html.escape(body), ["default", "Відкрити стрічку", "open", "Читати стрічку"],
                                          hints, 0)),
        GLib.VariantType("(u)"), Gio.DBusCallFlags.NONE, -1, None)
    my_id = result.unpack()[0]
    NOTIFY_STATE.write_text(json.dumps({"id": my_id, "pid": os.getpid()}), encoding="utf-8")

    loop = GLib.MainLoop()

    def on_signal(_conn, _sender, _path, _iface, name, params, _data):
        values = params.unpack()
        if values[0] != my_id:
            return
        if name == "ActionInvoked" and values[1] in ("default", "open"):
            open_report()
            loop.quit()
        elif name == "NotificationClosed":
            loop.quit()

    for name in ("ActionInvoked", "NotificationClosed"):
        bus.signal_subscribe("org.freedesktop.Notifications", "org.freedesktop.Notifications", name,
                             "/org/freedesktop/Notifications", None, Gio.DBusSignalFlags.NONE, on_signal, None)
    GLib.timeout_add_seconds(3 * 24 * 3600, loop.quit)  # про всяк випадок не чекаємо вічно
    loop.run()


def bullet_list(stories, limit):
    """Список заголовків для сповіщення: спершу важливі (★), далі решта (•)."""
    stories = sorted(stories, key=lambda st: not st["important"])
    lines = [f"{'★' if st['important'] else '•'} {story_title(st)}" for st in stories[:limit]]
    if len(stories) > limit:
        lines.append(f"… і ще {len(stories) - limit}")
    return "\n".join(lines)


def notify_fresh(stories, errors):
    summary, body = f"Radar · {publications(len(stories))}", bullet_list(stories, 4)
    if errors:
        body += f"\n⚠ Не вдалося отримати: {', '.join(errors)}"
    send_notification(summary, body)


# ---------------------------------------------------------------- сторінка

MONTHS = ["січня", "лютого", "березня", "квітня", "травня", "червня", "липня",
          "серпня", "вересня", "жовтня", "листопада", "грудня"]


def day_label(day, today):
    if day == today:
        prefix = "Сьогодні"
    elif day == today - dt.timedelta(days=1):
        prefix = "Вчора"
    else:
        prefix = ["Понеділок", "Вівторок", "Середа", "Четвер", "Пʼятниця", "Субота", "Неділя"][day.weekday()]
    return f"{prefix}, {day.day} {MONTHS[day.month - 1]}"


def page_logo():
    try:
        return re.sub(r"<\?xml[^>]*>", "", ICON_FILE.read_text(encoding="utf-8"))
    except OSError:
        return ""


def write_report(db, settings, errors, sources, topics=None):
    topics = topics if topics is not None else load_topics()
    now = utcnow()
    stories = stories_for(db, now - dt.timedelta(days=settings["report_days"]), sources, topics, settings)
    last_run = db.execute("SELECT MAX(id) FROM runs").fetchone()[0]
    category_of = {t.name: t.category for t in topics}
    categories = list(dict.fromkeys(t.category for t in topics))
    specialised = {s["name"]: category_of.get(s["all_news_topic"]) for s in sources
                   if s.get("all_news") and s.get("all_news_topic")}

    def main_category(story):
        """Група, до якої належить більшість тем сюжету; профільні видання тягнуть до своєї."""
        votes = collections.Counter(category_of[t] for t in story["topics"] if t in category_of)
        if specialised.get(story["primary"]["source"]):
            votes[specialised[story["primary"]["source"]]] += 1.5
        if not votes:
            return "Інше"
        order = {c: i for i, c in enumerate(categories)}
        return max(votes, key=lambda c: (votes[c], -order.get(c, len(order))))
    today = dt.datetime.now().date()

    data = []
    for st in stories:
        primary = st["primary"]
        moment = st["moment"].astimezone()
        also, seen_outlets = [], {primary["source"]}
        for r in st["rows"][1:]:
            if r["source"] not in seen_outlets:
                seen_outlets.add(r["source"])
                also.append({"s": r["source"], "u": r["link"], "t": r["title_uk"] or r["title"]})
        data.append({
            "t": primary["title_uk"] or primary["title"],
            "o": primary["title"] if primary["title_uk"] else "",
            "u": primary["link"], "s": primary["source"], "also": also,
            "all": st["outlets"], "links": [r["link"] for r in st["rows"]],
            "tags": st["topics"][:3], "cat": main_category(st),
            "imp": st["important"], "why": st["reason"],
            "time": f"{moment:%H:%M}", "day": day_label(moment.date(), today),
            "seen": int(from_iso(st["first_seen"]).timestamp()),
            "latest": any(r["run_id"] == last_run for r in st["rows"]),
        })
    if "Інше" not in categories:
        categories.append("Інше")

    def as_json(value):
        return json.dumps(value, ensure_ascii=False).replace("</", "<\\/")

    warn = ""
    if errors:
        warn = ('<p class="warn">Не вдалося отримати: '
                + ", ".join(f'<span title="{html.escape(e)}">{html.escape(n)}</span>' for n, e in errors.items())
                + "</p>")
    days = settings["report_days"]
    page = (APP_DIR / "report_template.html").read_text(encoding="utf-8")
    for key, value in {
        "__LOGO__": page_logo(),
        # Іконка вкладки й закладки — той самий логотип, вбудований у сторінку
        "__FAVICON__": "data:image/svg+xml;base64," + base64.b64encode(page_logo().encode()).decode(),
        "__UPDATED__": dt.datetime.now().strftime("%H:%M, %d.%m.%Y"),
        "__DAYS__": "останній день" if days == 1 else plural_count(days, "день", "дні", "днів"),
        "__WARN__": warn,
        "__DATA__": as_json(data),
        "__SOURCES__": as_json([s["name"] for s in sources if s.get("enabled", True)]),
        "__CATEGORIES__": as_json(categories),
    }.items():
        page = page.replace(key, value)
    tmp = REPORT_FILE.with_suffix(".tmp")
    tmp.write_text(page, encoding="utf-8")
    tmp.replace(REPORT_FILE)


def open_report():
    ensure_config()
    if not REPORT_FILE.exists():
        write_report(open_db(), load_settings(), {}, load_sources())
    import webbrowser  # відкриваємо саме у браузері, а не в програмі для .html-файлів
    webbrowser.open(REPORT_FILE.as_uri())


# ---------------------------------------------------------------- видання

def friendly_error(error):
    """Пояснює помилку мережі людською мовою."""
    if isinstance(error, urllib.error.HTTPError):
        if error.code in (401, 403, 429):
            return "сайт блокує автоматичні запити"
        if error.code == 404:
            return "сторінку не знайдено (перевірте адресу)"
        if error.code >= 500:
            return f"сайт зараз не працює (помилка {error.code})"
        return f"сайт відповів помилкою {error.code}"
    if isinstance(error, urllib.error.URLError):
        reason = str(error.reason)
        if "Name or service not known" in reason or "getaddrinfo" in reason:
            return "немає інтернету або адреса сайту неправильна"
        if "timed out" in reason:
            return "сайт не відповідає"
        return "не вдалося з'єднатися з сайтом"
    if isinstance(error, TimeoutError):
        return "сайт не відповідає"
    if isinstance(error, ET.ParseError):
        return "стрічка новин пошкоджена"
    return str(error)


def google_news_feed(host):
    domain = re.sub(r"^www\.", "", host)
    lang = "hl=uk&gl=UA&ceid=UA:uk" if domain.endswith(".ua") else "hl=en-US&gl=US&ceid=US:en"
    return f"https://news.google.com/rss/search?q=site:{domain}+when:1d&{lang}"


def discover_source(url, name=None):
    """Визначає, як читати сайт: знаходить RSS або налаштовує читання сторінки.

    Порядок: RSS, указаний на сторінці → типові адреси RSS → посилання на самій сторінці →
    пошук Google News по цьому сайту (для сайтів, що блокують автоматичні запити).
    """
    if not re.match(r"https?://", url):
        url = "https://" + url
    parsed_url = urllib.parse.urlparse(url)
    host = parsed_url.netloc
    root = f"{parsed_url.scheme}://{host}"
    name = name or re.sub(r"^www\.", "", host)

    def feed_items(feed_url):
        try:
            data, _ = http_get(feed_url, timeout=15)
            return parse_feed(data)
        except Exception:
            return []

    def rss(feed_url, via=None):
        source = {"name": name, "url": url, "type": "rss", "feeds": [feed_url], "enabled": True}
        if via:
            source["via"] = via
        return source

    if feed_items(url):  # користувач одразу дав RSS
        return rss(url)

    page_error = None
    try:
        data, charset = http_get(url)
    except Exception as error:  # сайт може блокувати сторінку, але віддавати RSS
        data, charset, page_error = b"", None, error

    candidates = []
    for tag in re.findall(r"<link\b[^>]*>", decode(data, charset), re.I):
        if re.search(r"application/(rss|atom)\+xml", tag, re.I):
            href = re.search(r'href=["\']([^"\']+)', tag, re.I)
            if href:
                candidates.append(urllib.parse.urljoin(url, html.unescape(href.group(1))))
    # Спершу стрічки з того самого піддомену (finance.liga.net), далі — зі схожим шляхом (/finance)
    path = [p for p in parsed_url.path.strip("/").split("/") if len(p) > 2]
    candidates.sort(key=lambda c: (urllib.parse.urlparse(c).netloc != host,
                                   -sum(part in c for part in path)))
    page = url.split("?")[0].rstrip("/")
    candidates += [page + "?format=rss", page + "/feed", page + "/rss", page + "/rss.xml"]
    candidates += [root + p for p in ("/feed", "/rss", "/rss.xml", "/feed.xml", "/feed/", "/rss/home")]
    seen = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        if feed_items(candidate):
            return rss(candidate)

    # RSS немає — читаємо посилання на сторінці. Шукаємо найпоширеніший початок шляху статей.
    prefixes = {}
    if data:
        for link, title, _time in page_links(url, data, charset):
            parsed = urllib.parse.urlparse(link)
            if parsed.netloc != host or len(title) < 25:
                continue
            parts = [p for p in parsed.path.split("/") if p]
            if len(parts) < 2 and not (parts and len(parts[0]) > 30):
                continue
            prefix = "/" + parts[0] + "/" if len(parts) > 1 else "/"
            prefixes[prefix] = prefixes.get(prefix, 0) + 1
    if prefixes:
        prefix = max(prefixes, key=prefixes.get)
        regex = re.escape(host) + re.escape(prefix) + r".{8,}"
        return {"name": name, "url": url, "type": "html", "link_regex": regex, "enabled": True}

    # Останній варіант — заголовки цього сайту з Google News (так працює Reuters)
    if feed_items(google_news_feed(host)):
        return rss(google_news_feed(host), via="Google News")
    if isinstance(page_error, urllib.error.HTTPError):
        raise ValueError(friendly_error(page_error) + ", а RSS-стрічки знайти не вдалося")
    if page_error:
        raise ValueError(friendly_error(page_error))
    raise ValueError("на сторінці не знайдено ні RSS, ні списку новин")


HOW = {"RSS": "через RSS-стрічку", "сторінка сайту": "зі сторінки сайту",
       "Google News": "через Google News, бо сайт блокує прямий доступ"}


def source_kind(source):
    if source.get("via") or "news.google.com" in " ".join(source.get("feeds", [])):
        return "Google News"
    return "сторінка сайту" if source.get("type") == "html" else "RSS"


def find_source(sources, key):
    if key.isdigit() and 1 <= int(key) <= len(sources):
        return int(key) - 1
    for i, source in enumerate(sources):
        if source["name"].casefold() == key.casefold() or source["url"] == key:
            return i
    raise SystemExit(f"Видання «{key}» не знайдено. Подивіться список: radar sources")


def add_source(url, name=None):
    sources = load_sources()
    source = discover_source(url, name)
    items = fetch_source(source)
    if not items:
        raise ValueError("сайт відкрився, але новин на ньому не знайдено")
    if any(s["name"].casefold() == source["name"].casefold() for s in sources):
        source["name"] += f" ({len(sources) + 1})"
    sources.append(source)
    save_sources(sources)
    return source, items


# ---------------------------------------------------------------- розклад

SERVICE = """[Unit]
Description={description}
After=network-online.target

[Service]
Type=oneshot
ExecStart={python} {script} {command}
# Сповіщення з кнопкою «Читати стрічку» живе довше за сам запуск
KillMode=process
"""

TIMER = """[Unit]
Description={description}

[Timer]
{rules}
Persistent=true

[Install]
WantedBy=timers.target
"""


def systemctl(*args):
    return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True)


def install_timer(unit, description, command, rules):
    """Створює (або, якщо rules порожні, вимикає) таймер systemd для поточного користувача."""
    SYSTEMD_DIR.mkdir(parents=True, exist_ok=True)
    service = SYSTEMD_DIR / f"{unit}.service"
    timer = SYSTEMD_DIR / f"{unit}.timer"
    service.write_text(SERVICE.format(description=description, python=sys.executable,
                                      script=APP_DIR / "radar.py", command=command), encoding="utf-8")
    if not rules:
        systemctl("disable", "--now", f"{unit}.timer")
        if timer.exists():
            timer.unlink()
        systemctl("daemon-reload")
        return
    timer.write_text(TIMER.format(description=description, rules=rules), encoding="utf-8")
    systemctl("daemon-reload")
    systemctl("enable", f"{unit}.timer")
    systemctl("restart", f"{unit}.timer")


def apply_schedule(settings):
    mode = settings["schedule_mode"]
    if mode == "off":
        rules = ""
    elif mode == "times":
        rules = "\n".join(f"OnCalendar=*-*-* {t}:00" for t in settings["times"])
    else:
        minutes = max(5, int(settings["interval_minutes"]))
        rules = f"OnBootSec=3min\nOnUnitActiveSec={minutes}min"
    install_timer("radar", "Radar — оновлення фінансових новин", "run --quiet", rules)
    digest_rules = f"OnCalendar=*-*-* {settings['digest_time']}:00" if settings["digest_enabled"] else ""
    install_timer("radar-digest", "Radar — ранковий підсумок", "digest", digest_rules)


def near_digest(settings, moment=None):
    """Чи зараз час ранкового підсумку (±10 хв) — тоді звичайне сповіщення зайве."""
    if not settings.get("digest_enabled"):
        return False
    moment = moment or dt.datetime.now()
    hours, minutes = map(int, settings["digest_time"].split(":"))
    digest = moment.replace(hour=hours, minute=minutes, second=0, microsecond=0)
    return abs((moment - digest).total_seconds()) <= 10 * 60


def digest():
    """Оновлює новини й надсилає одне сповіщення з головним від попереднього підсумку."""
    run(notify=False, verbose=False, wait=True)
    settings = load_settings()
    topics = load_topics()
    db = open_db()
    now = utcnow()
    last = db.execute("SELECT value FROM meta WHERE key = 'last_digest'").fetchone()
    since = from_iso(last[0]) if last else now - dt.timedelta(hours=16)
    since = max(since, now - dt.timedelta(hours=36))
    stories = [st for st in stories_for(db, now - dt.timedelta(hours=48), load_sources(), topics, settings)
               if st["first_seen"] >= iso(since)]
    db.execute("INSERT OR REPLACE INTO meta VALUES ('last_digest', ?)", (iso(now),))
    db.commit()
    db.close()
    stories.sort(key=lambda st: (not st["important"], -st["weight"], -st["moment"].timestamp()))
    important = sum(st["important"] for st in stories)
    if stories:
        lines = [f"{'★' if st['important'] else '•'} {story_title(st)}" for st in stories[:6]]
        if len(stories) > 6:
            lines.append(f"… усього {publications(len(stories))}")
        summary = "Radar · Головне за ніч" + (f" ({important} важл.)" if important else "")
        send_notification(summary, "\n".join(lines))
    else:
        send_notification("Radar · Ранковий підсумок", "За ніч нічого нового за вашими темами")
    log(f"ранковий підсумок: {len(stories)} сюжетів, важливих {important}")


def describe_schedule(settings):
    mode = settings["schedule_mode"]
    if mode == "off":
        return "автоматичне оновлення вимкнено"
    if mode == "times":
        return "щодня о " + ", ".join(settings["times"])
    minutes = int(settings["interval_minutes"])
    if minutes % 60 == 0:
        hours = minutes // 60
        return "щогодини" if hours == 1 else f"кожні {hours} год"
    return f"кожні {minutes} хв"


def next_run_text():
    out = systemctl("list-timers", "radar.timer", "--no-pager", "--output=json")
    try:
        timers = json.loads(out.stdout or "[]")
        moment = dt.datetime.fromtimestamp(timers[0]["next"] / 1_000_000)
    except (ValueError, LookupError, TypeError, OSError):
        return None
    today = dt.date.today()
    day = moment.date()
    when = "сьогодні" if day == today else "завтра" if day == today + dt.timedelta(days=1) else f"{day:%d.%m}"
    return f"{when} о {moment:%H:%M}"


def set_schedule(args):
    settings = load_settings()
    if not args:
        pass
    elif args[0] in ("off", "вимкнути", "0"):
        settings["schedule_mode"] = "off"
    elif all(re.fullmatch(r"\d{1,2}:\d\d", a) for a in args):
        settings["schedule_mode"] = "times"
        settings["times"] = sorted(f"{int(a.split(':')[0]):02d}:{a.split(':')[1]}" for a in args)
    elif re.fullmatch(r"\d+(хв|m|min)?", args[0]):
        settings["schedule_mode"] = "interval"
        settings["interval_minutes"] = max(5, int(re.match(r"\d+", args[0]).group()))
    elif re.fullmatch(r"\d+(год|h)", args[0]):
        settings["schedule_mode"] = "interval"
        settings["interval_minutes"] = int(re.match(r"\d+", args[0]).group()) * 60
    else:
        raise SystemExit("Приклади: radar schedule 60   ·   radar schedule 2год   ·   "
                         "radar schedule 08:00 13:00 19:00   ·   radar schedule off")
    save_settings(settings)
    apply_schedule(settings)
    print(f"Розклад: {describe_schedule(settings)}")
    nxt = next_run_text()
    if nxt:
        print(f"Наступне оновлення: {nxt}")


# ---------------------------------------------------------------- команди

HELP = """Radar — фінансові новини з ваших видань

  radar run                         оновити новини зараз
  radar open                        відкрити сторінку зі списком новин
  radar gui                         вікно налаштувань

  radar sources                     список видань
  radar source add URL [Назва]      додати видання (RSS знайдеться автоматично)
  radar source remove НОМЕР|НАЗВА   видалити видання
  radar source off НОМЕР|НАЗВА      тимчасово вимкнути (on — увімкнути)
  radar source all НОМЕР|НАЗВА      усі новини видання без фільтра за темами (ще раз — вимкнути)

  radar topics                      список тем
  radar topic add "Тема: ключ1, ключ2"
  radar topic remove "Тема"
  radar topics edit                 відкрити файл тем у текстовому редакторі

  radar schedule                    показати розклад
  radar schedule 60                 кожні 60 хвилин (або 30, 2год…)
  radar schedule 08:00 13:00 19:00  у визначений час
  radar schedule off                вимкнути автоматичне оновлення
  radar notifications off|on        вимкнути / увімкнути сповіщення про свіжі новини
  radar digest                      надіслати ранковий підсумок зараз

  radar status                      стан, останній запуск, файли налаштувань
"""


def cmd_sources():
    for i, s in enumerate(load_sources(), 1):
        mark = " " if s.get("enabled", True) else "✗"
        kind = source_kind(s) + (", усі новини" if s.get("all_news") else "")
        print(f"{i:>2}. {mark} {s['name']:<22} {s['url']}  ({kind})")


def cmd_source(args):
    if not args:
        return cmd_sources()
    action, rest = args[0], args[1:]
    sources = load_sources()
    if action == "add" and rest:
        print("Перевіряю сайт…")
        try:
            source, items = add_source(rest[0], " ".join(rest[1:]) or None)
        except Exception as error:
            raise SystemExit(f"Не вдалося додати: {friendly_error(error)}")
        how = HOW[source_kind(source)]
        print(f"Додано «{source['name']}» ({how}). Зараз на сайті {len(items)} новин, наприклад:")
        for item in items[:3]:
            print(f"  • {item['title']}")
    elif action in ("remove", "rm", "delete") and rest:
        i = find_source(sources, " ".join(rest))
        removed = sources.pop(i)
        save_sources(sources)
        print(f"Видалено «{removed['name']}»")
    elif action == "all" and rest:
        i = find_source(sources, " ".join(rest))
        sources[i]["all_news"] = not sources[i].get("all_news", False)
        save_sources(sources)
        state = "усі новини, без фільтра" if sources[i]["all_news"] else "лише новини за темами"
        print(f"«{sources[i]['name']}»: {state}")
    elif action in ("on", "off") and rest:
        i = find_source(sources, " ".join(rest))
        sources[i]["enabled"] = action == "on"
        save_sources(sources)
        print(f"«{sources[i]['name']}» {'увімкнено' if action == 'on' else 'вимкнено'}")
    else:
        print(HELP)


def cmd_topics(args):
    if args and args[0] == "edit":
        subprocess.Popen(["xdg-open", str(TOPICS_FILE)])
        return
    topics = load_topics()
    category = None
    for i, topic in enumerate(topics, 1):
        if topic.category != category:
            category = topic.category
            print(f"\n[{category}]")
        print(f"{i:>3}. {topic.name}")
    print(f"\nУсього тем: {len(topics)}. Файл із ключовими словами: {TOPICS_FILE}")


def remove_topic_line(text, name):
    lines = text.splitlines()
    for i, line in enumerate(lines):
        stripped = line.strip()
        if (stripped and not stripped.startswith(("#", "["))
                and parse_topic_line(stripped)[0].casefold() == name.casefold()):
            del lines[i]
            return "\n".join(lines) + "\n"
    return None


def cmd_topic(args):
    if len(args) < 2:
        return cmd_topics([])
    action, value = args[0], " ".join(args[1:]).strip()
    text = TOPICS_FILE.read_text(encoding="utf-8")
    if action == "add":
        if not parse_topics(value):
            raise SystemExit("Не вдалося розібрати тему. Приклад: radar topic add \"Лізинг авто: лізинг & авто\"")
        categories = re.findall(r"^\[(.+)\]\s*$", text, re.M)
        tail = "" if categories and categories[-1] == "Інше" else "\n[Інше]"
        TOPICS_FILE.write_text(text.rstrip("\n") + tail + "\n" + value + "\n", encoding="utf-8")
        print(f"Тему додано: {value}")
    elif action in ("remove", "rm", "delete"):
        new = remove_topic_line(text, value)
        if new is None:
            raise SystemExit(f"Тему «{value}» не знайдено. Подивіться список: radar topics")
        TOPICS_FILE.write_text(new, encoding="utf-8")
        print(f"Тему «{value}» видалено")
    else:
        print(HELP)


def cmd_status():
    settings = load_settings()
    print(f"Розклад: {describe_schedule(settings)}")
    nxt = next_run_text()
    if nxt:
        print(f"Наступне оновлення: {nxt}")
    print("Сповіщення про свіжі новини: " + ("увімкнено" if settings["notifications"] else "вимкнено"))
    print("Ранковий підсумок: " + (f"щодня о {settings['digest_time']}" if settings["digest_enabled"] else "вимкнено"))
    if DB_FILE.exists():
        db = open_db()
        row = db.execute("SELECT * FROM runs WHERE finished IS NOT NULL ORDER BY id DESC LIMIT 1").fetchone()
        if row:
            when = from_iso(row["finished"]).astimezone().strftime("%H:%M %d.%m.%Y")
            print(f"Останнє оновлення: {when}, свіжих публікацій: {row['new_count']}")
            errors = json.loads(row["errors"] or "{}")
            for name, error in errors.items():
                print(f"  ! {name}: {error}")
    active = sum(s.get("enabled", True) for s in load_sources())
    print(f"Видань: {active}, тем: {len(load_topics())}")
    print(f"\nНалаштування: {CONFIG_DIR}\nСторінка новин: {REPORT_FILE}\nЖурнал: {LOG_FILE}")


def main(argv):
    ensure_config()
    command, args = (argv[0], argv[1:]) if argv else ("help", [])
    if command == "run":
        run(notify="--no-notify" not in args, verbose="--quiet" not in args)
    elif command == "notify" and len(args) >= 3:  # службова: показати сповіщення й чекати на клік
        notify_and_wait(args[0], args[1], args[2])
    elif command == "digest":
        digest()
    elif command == "open":
        open_report()
    elif command == "gui":
        os.execv(sys.executable, [sys.executable, str(APP_DIR / "radar_gui.py")])
    elif command == "sources":
        cmd_sources()
    elif command == "source":
        cmd_source(args)
    elif command == "topics":
        cmd_topics(args)
    elif command == "topic":
        cmd_topic(args)
    elif command == "schedule":
        set_schedule(args)
    elif command == "notifications" and args and args[0] in ("on", "off"):
        settings = load_settings()
        settings["notifications"] = args[0] == "on"
        save_settings(settings)
        print("Сповіщення " + ("увімкнено" if settings["notifications"] else "вимкнено"))
    elif command == "status":
        cmd_status()
    else:
        print(HELP)


if __name__ == "__main__":
    main(sys.argv[1:])
