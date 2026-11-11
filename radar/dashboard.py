"""Дашборд Radar: курси гривні, облікова ставка, резерви, світові ринки й криптовалюта.

Дані оновлюються під час кожного запуску Radar; повільні ряди (історія курсу НБУ, резерви)
— рідше, за TTL нижче. Якщо джерело не відповіло, лишаються попередні значення.
Результат — сторінка dashboard.html поряд зі сторінкою новин.
"""

import base64
import datetime as dt
import html
import json
import re
import urllib.parse
import urllib.request
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
USER_AGENT = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")

# Скільки годин тримати дані до наступного запиту (0 — щоразу)
TTL_HOURS = {"official": 6, "key": 12, "reserves": 24, "bars": 12,
             "interbank": 0, "cash": 0, "quotes": 0, "crypto": 0}


def http_get(url, timeout=25):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept-Language": "uk,en;q=0.8"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def get_json(url):
    return json.loads(http_get(url).decode("utf-8"))


def utcnow():
    return dt.datetime.now(dt.timezone.utc)


def iso(moment):
    return moment.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def number(text):
    """«7,541.40» (CNBC) або «45,52293» (Мінфін) → float."""
    text = (text or "").strip().replace("%", "").replace("+", "")
    if "," in text and "." in text:
        text = text.replace(",", "")
    return float(text.replace(",", ".").replace(" ", "").replace(" ", ""))


# ---------------------------------------------------------------- джерела

def fetch_official(_old):
    """Офіційний курс НБУ: останні 120 днів і рекорд з 2022 року."""
    out = {}
    end = dt.date.today().strftime("%Y%m%d")
    for cc in ("usd", "eur"):
        rows = get_json(f"https://bank.gov.ua/NBU_Exchange/exchange_site?start=20220101&end={end}"
                        f"&valcode={cc}&sort=exchangedate&order=asc&json")
        series = sorted([dt.datetime.strptime(r["exchangedate"], "%d.%m.%Y").date().isoformat(), r["rate_per_unit"]]
                        for r in rows)
        top = max(series, key=lambda x: x[1])
        out[cc] = {"series": series[-120:], "max": top}
    return out


def fetch_key(_old):
    """Облікова ставка на сьогодні й на перше число кожного з останніх 8 місяців."""
    today = dt.date.today()
    points, first = {today}, today.replace(day=1)
    for _ in range(8):
        points.add(first)
        first = (first - dt.timedelta(days=1)).replace(day=1)
    result = []
    for day in sorted(points):
        rows = get_json(f"https://bank.gov.ua/NBUStatService/v1/statdirectory/key?date={day:%Y%m%d}&json")
        value = next((r["value"] for r in rows if r["id_api"] == "KEY_PolicyRate"), None)
        if value is not None:
            result.append([day.isoformat(), value])
    return result


def fetch_reserves(_old):
    rows = get_json("https://bank.gov.ua/NBUStatService/v1/statdirectory/res?json")
    series = sorted([f"{r['dt'][:4]}-{r['dt'][4:6]}-{r['dt'][6:]}", r["value"]]
                    for r in rows if r["id_api"] == "RES_OffReserveAssets")
    return series[-14:]


def fetch_interbank(old):
    """Міжбанк (Мінфін): котирування за сьогодні кожні 30 хв і щоденна історія середини."""
    out = dict(old or {})
    for cc in ("usd", "eur"):
        rows = get_json(f"https://minfin.com.ua/data/currency/ib/{cc}.ib.today.json")
        points = [[r["date"], number(r["bid"]), number(r["ask"])] for r in rows if r.get("bid") and r.get("ask")]
        if not points:
            continue
        history = dict((old or {}).get(cc, {}).get("history", []))
        last = points[-1]
        history[last[0][:10]] = round((last[1] + last[2]) / 2, 4)
        out[cc] = {"today": points, "history": sorted(history.items())[-120:]}
    return out


CASH_RE = r"Середній курс в банках.*?{cc}\s+([\d,]+)\s+(-?[\d.]+)\s+([\d,]+)\s+(-?[\d.]+)\s+([\d,]+)" \
          r".*?Готівковий курс.*?{cc}\s+([\d,]+)\s+(-?[\d.]+)\s+([\d,]+)\s+(-?[\d.]+)"


def fetch_cash(old):
    """Середній курс у банках і на готівковому ринку (сторінки Мінфіну)."""
    out = dict(old or {})
    for cc in ("usd", "eur"):
        page = http_get(f"https://minfin.com.ua/ua/currency/{cc}/").decode("utf-8", "replace")
        text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", page, flags=re.S)
        text = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", text)))
        m = re.search(CASH_RE.format(cc=cc.upper()), text)
        if not m:
            raise ValueError(f"на сторінці Мінфіну не знайдено курсів {cc.upper()}")
        g = m.groups()
        history = dict((old or {}).get(cc, {}).get("history", []))
        market = [number(g[5]), number(g[7])]
        history[dt.date.today().isoformat()] = round(sum(market) / 2, 4)
        out[cc] = {"banks": [number(g[0]), number(g[2])], "banks_change": [number(g[1]), number(g[3])],
                   "market": market, "market_change": [number(g[6]), number(g[8])],
                   "history": sorted(history.items())[-120:]}
    return out


def cnbc_symbols(cfg):
    return [s["symbol"] for group in cfg.get("markets", []) for s in group["items"]]


def fetch_quotes(_old, cfg):
    symbols = "|".join(cnbc_symbols(cfg))
    data = get_json("https://quote.cnbc.com/quote-html-webservice/restQuote/symbolType/symbol?symbols="
                    + urllib.parse.quote(symbols) + "&requestMethod=itv&noform=1&partnerId=2&fund=1"
                    "&exthrs=1&output=json&events=1")
    out = {}
    for q in data["FormattedQuoteResult"]["FormattedQuote"]:
        try:
            out[q["symbol"]] = {"last": number(q["last"]), "change": number(q.get("change") or "0"),
                                "pct": number(q.get("change_pct") or "0"), "time": q.get("last_time", ""),
                                "currency": q.get("currencyCode", "")}
        except (KeyError, ValueError):
            continue
    return out


def fetch_bars(old, cfg):
    """Денні закриття за ~4 місяці для графіків."""
    out = dict(old or {})
    end = dt.date.today() + dt.timedelta(days=1)
    start = end - dt.timedelta(days=125)
    for symbol in cnbc_symbols(cfg):
        try:
            data = get_json(f"https://ts-api.cnbc.com/harmony/app/bars/{urllib.parse.quote(symbol)}/1D/"
                            f"{start:%Y%m%d}000000/{end:%Y%m%d}000000/adjusted/EST5EDT.json")
            bars = data["barData"]["priceBars"]
            out[symbol] = [[b["tradeTime"][:8], float(b["close"])] for b in bars if b.get("close")]
        except Exception:
            continue  # графік одного інструмента не критичний
    return out


def fetch_crypto(_old, cfg):
    ids = ",".join(cfg.get("crypto", []))
    coins = get_json("https://api.coingecko.com/api/v3/coins/markets?vs_currency=usd&ids=" + ids
                     + "&sparkline=true&price_change_percentage=24h,7d")
    order = {c: i for i, c in enumerate(cfg.get("crypto", []))}
    coins.sort(key=lambda c: order.get(c["id"], 99))
    result = {"coins": [{
        "name": c["name"], "symbol": c["symbol"].upper(), "price": c["current_price"],
        "d1": c.get("price_change_percentage_24h_in_currency") or c.get("price_change_percentage_24h"),
        "d7": c.get("price_change_percentage_7d_in_currency"), "cap": c.get("market_cap"),
        "spark": [round(p, 6) for p in (c.get("sparkline_in_7d") or {}).get("price", [])[::4]],
    } for c in coins]}
    try:
        g = get_json("https://api.coingecko.com/api/v3/global")["data"]
        result["global"] = {"cap": g["total_market_cap"]["usd"], "btc": g["market_cap_percentage"]["btc"],
                            "change": g.get("market_cap_change_percentage_24h_usd")}
    except Exception:
        pass
    return result


# ---------------------------------------------------------------- оновлення

def load_config(config_dir):
    for path in (Path(config_dir) / "dashboard.json", APP_DIR / "defaults" / "dashboard.json"):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
    return {}


def update(config_dir, data_dir, log=print, force=False):
    cfg = load_config(config_dir)
    cache_file = Path(data_dir) / "dashboard.json"
    try:
        cache = json.loads(cache_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cache = {}
    fetched, errors = cache.setdefault("fetched", {}), cache.setdefault("errors", {})
    now = utcnow()
    jobs = {
        "official": fetch_official, "key": fetch_key, "reserves": fetch_reserves,
        "interbank": fetch_interbank, "cash": fetch_cash,
        "quotes": lambda old: fetch_quotes(old, cfg), "bars": lambda old: fetch_bars(old, cfg),
        "crypto": lambda old: fetch_crypto(old, cfg),
    }
    for name, job in jobs.items():
        last = fetched.get(name)
        if not force and last and TTL_HOURS[name] and \
                now - dt.datetime.fromisoformat(last.replace("Z", "+00:00")) < dt.timedelta(hours=TTL_HOURS[name]):
            continue
        try:
            cache[name] = job(cache.get(name))
            fetched[name] = iso(now)
            errors.pop(name, None)
        except Exception as error:
            errors[name] = str(error)[:200]
            log(f"дашборд, {name}: {error}")
    cache["updated"] = iso(now)
    cache_file.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    write_page(cfg, cache, Path(data_dir) / "dashboard.html")
    return cache


def write_page(cfg, cache, target):
    logo = ""
    try:
        logo = re.sub(r"<\?xml[^>]*>", "", (APP_DIR / "radar.svg").read_text(encoding="utf-8"))
    except OSError:
        pass
    page = (APP_DIR / "dashboard_template.html").read_text(encoding="utf-8")
    payload = {"cache": cache, "markets": cfg.get("markets", []), "now": iso(utcnow())}
    for key, value in {
        "__LOGO__": logo,
        "__FAVICON__": "data:image/svg+xml;base64," + base64.b64encode(logo.encode()).decode(),
        "__UPDATED__": dt.datetime.now().strftime("%H:%M, %d.%m.%Y"),
        "__DATA__": json.dumps(payload, ensure_ascii=False).replace("</", "<\\/"),
    }.items():
        page = page.replace(key, value)
    tmp = target.with_suffix(".tmp")
    tmp.write_text(page, encoding="utf-8")
    tmp.replace(target)
