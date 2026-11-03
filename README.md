# Radar

Фінансові новини з обраних видань. Radar збирає стрічки й сторінки сайтів, відбирає новини за
темами, перекладає іноземні заголовки українською й об'єднує повтори. Окремо він позначає те, що
самі видання подають як головне. Штучний інтелект не використовується.

**Сторінка новин:** https://goloborodko-yaroslav.github.io/radar/

## Дві версії, один код

| | Веб-версія | Програма для Linux |
|---|---|---|
| Як користуватися | відкрити сторінку в будь-якому браузері | Меню → Radar |
| Де працює | GitHub Actions за розкладом | на комп'ютері, таймер systemd |
| Налаштування | тека [`config/radar`](config/radar) | `~/.config/radar` або вікно програми |
| Сповіщення | немає | так, вмикаються у вікні |

Обидві запускають [`radar/radar.py`](radar/radar.py). Він написаний на Python 3 і використовує
лише стандартну бібліотеку, тож додаткові пакети не потрібні.

## Документація

| Файл | Для кого | Що всередині |
|---|---|---|
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | розробник | як працює програма: цикл оновлення, сюжети, важливість, база, сторінка, розклад |
| [`docs/CONFIGURATION.md`](docs/CONFIGURATION.md) | розробник, редактор | формати `sources.json`, `topics.txt`, `settings.json` |
| [`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md) | розробник | запуск, перевірка, випуск, розширення, відомі проблеми |
| [`docs/DECISIONS.md`](docs/DECISIONS.md) | усі | вимоги власника й чому зроблено саме так |
| [`radar/README.md`](radar/README.md) | користувач Linux | встановлення й щоденне користування |

## Швидкий старт для розробника

```bash
git clone https://github.com/goloborodko-yaroslav/radar.git && cd radar
XDG_CONFIG_HOME="$PWD/config" XDG_DATA_HOME="$PWD/.data" python3 radar/radar.py run --no-notify
xdg-open .data/radar/report.html
```

## Як змінити видання, теми чи параметри (веб-версія)

Усе лежить у теці [`config/radar`](config/radar):

| Файл | Що в ньому |
|---|---|
| `topics.txt` | теми й ключові слова, згруповані в розділи `[Назва групи]` |
| `sources.json` | видання: адреси стрічок, режим «усі новини», ознаки головних новин |
| `settings.json` | скільки днів показувати, чи шукати в першому абзаці тощо |

Відкрийте файл на GitHub, натисніть олівець (Edit), змініть і збережіть (Commit changes).
Оновлення запуститься одразу, а нова сторінка з'явиться за кілька хвилин. Оновити вручну:
**Actions** → **Radar** → **Run workflow**.

## Структура

```
radar/                 програма (ядро, вікно GTK, шаблон сторінки, установник)
config/radar/          налаштування веб-версії
.github/workflows/     розклад і публікація на GitHub Pages
docs/                  документація
```

---

## In English (summary)

Radar is a rule-based (no AI) news aggregator for Ukrainian business and finance readers. It
fetches RSS feeds and scraped pages from 37 outlets, keeps the items that match 189 keyword-defined
topics, translates foreign headlines into Ukrainian via Google Translate, clusters the same event
across outlets into one "story", and flags stories the outlets themselves present as top news. The
output is a single static HTML page.

- Core: `radar/radar.py`, Python 3.10+, standard library only (sqlite3, urllib, html.parser).
- Desktop app: GTK 3 settings window (`radar/radar_gui.py`), systemd user timers, D-Bus
  notifications. Linux only.
- Web: GitHub Actions runs the same script on a cron schedule and deploys the page to GitHub Pages.
- Configuration: `sources.json` (outlets), `topics.txt` (topics and keywords), `settings.json`.
- Documentation is in Ukrainian under `docs/`. Code comments and UI are in Ukrainian; identifiers
  are in English.
