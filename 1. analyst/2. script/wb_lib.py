#!/usr/bin/env python3
"""Общая библиотека агента-аналитика: доступ к WB API, лимиты, БД.

Главная особенность WB API — очень жёсткие лимиты на аналитические методы:
финотчёт отдаётся раз в ~8 часов, воронка раз в ~24 минуты. Поэтому любой
запрос идёт через throttle(), который помнит время последнего вызова на диске
и не даёт сжечь квоту повторным запуском скрипта.
"""
import sys, os, json, time, pathlib, datetime
import requests

ROOT = pathlib.Path(__file__).resolve().parents[2]      # …/Менеджер WB
AGENT = pathlib.Path(__file__).resolve().parents[1]     # …/1. analyst
WORK = AGENT / "3. work"
DATE = AGENT / "1. date"
REPORTS = AGENT / "4. reports"
STATE = WORK / "api_state.json"

sys.path.insert(0, "/home/wb/oplog")
try:
    import db_log
except Exception:
    db_log = None

# ---------------------------------------------------------------- окружение

def load_env(path=None):
    path = pathlib.Path(path or ROOT / ".env")
    env = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env

ENV = load_env()
TOKEN = ENV.get("WB_API_TOKEN", "")

HOST = {
    "common":    "https://common-api.wildberries.ru",
    "stats":     "https://statistics-api.wildberries.ru",
    "market":    "https://marketplace-api.wildberries.ru",
    "prices":    "https://discounts-prices-api.wildberries.ru",
    "feedbacks": "https://feedbacks-api.wildberries.ru",
    "advert":    "https://advert-api.wildberries.ru",
    "analytics": "https://seller-analytics-api.wildberries.ru",
    "supplies":  "https://supplies-api.wildberries.ru",
    "returns":   "https://returns-api.wildberries.ru",
    "documents": "https://documents-api.wildberries.ru",
    "content":   "https://content-api.wildberries.ru",
    "finance":   "https://finance-api.wildberries.ru",
}

# Минимальный интервал между вызовами конкретного метода, секунды.
# Замерено на живом контуре 2026-10-07 по заголовку X-Ratelimit-Reset: у WB
# аналитические и финансовые методы отдаются окнами по часам, а не по минутам.
# Если сервер в 429 назовёт окно больше — _mark() запомнит его и подвинет отметку.
MIN_INTERVAL = {
    "finance:/api/finance/v1/sales-reports/detailed": 8 * 3600,    # замерено 27 995 с
    "finance:/api/finance/v1/sales-reports/list":     10 * 60,
    "analytics:/api/analytics/v3/sales-funnel/products":         25 * 60,   # замерено 1 444 с
    "analytics:/api/analytics/v3/sales-funnel/products/history": 25 * 60,
    "analytics:/api/v1/paid_storage":        10 * 60,
    "analytics:/api/v1/warehouse_remains":   20 * 60,
    "stats:/api/v1/supplier/sales":          2 * 3600,             # замерено 6 095 с
    "stats:/api/v1/supplier/orders":         2 * 3600,
    "advert:/adv/v3/fullstats":              50 * 60,   # замерено 2 755 с
    "advert:/adv/v1/upd":                    60 * 60,   # замерено 3 557 с
    "advert:/adv/v1/balance":                60,
    "advert:/adv/v1/promotion/count":        15 * 60,  # замерено 755 с
    "advert:/api/advert/v2/adverts":         60 * 60,   # замерено 3 594 с
    "content:/content/v2/get/cards/list":    1,
    "prices:/api/v2/list/goods/filter":      1,
    "common:/api/v1/tariffs/commission":     60,
    "common:/api/v1/tariffs/box":            60,
}

# ---------------------------------------------------------------- состояние лимитов

def _state():
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        return {}

def _save_state(st):
    WORK.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")

def quota_left(host, path):
    """Сколько секунд осталось до того, как метод снова можно вызвать.

    Два независимых ограничения, берём то, которое дольше:
      ts           — когда метод вызывали в прошлый раз, плюс наше окно;
      available_at — момент, назначенный самим сервером в ответе 429.

    available_at хранится абсолютным временем, а не сдвигом отметки ts.
    Сдвигать ts нельзя: окно потом прибавляется к сдвинутой отметке ещё раз,
    и ожидание удваивается — у fullstats это давало 1 ч 32 мин вместо 50 мин.
    """
    key = "%s:%s" % (host, path)
    rec = _state().get(key) or {}
    now = time.time()

    left = 0
    gap = MIN_INTERVAL.get(key)
    if gap:
        left = gap - (now - rec.get("ts", 0))
    avail = rec.get("available_at")
    if avail:
        left = max(left, avail - now)
    return max(0, int(left))

def _mark(host, path, retry_after=None):
    st = _state()
    key = "%s:%s" % (host, path)
    now = time.time()
    rec = {"ts": now, "at": datetime.datetime.now().isoformat(timespec="seconds")}
    if retry_after:
        rec["retry_after"] = retry_after
        rec["available_at"] = now + retry_after
        rec["available_at_h"] = datetime.datetime.fromtimestamp(
            now + retry_after).isoformat(timespec="seconds")
    st[key] = rec
    _save_state(st)

# ---------------------------------------------------------------- запросы

class Throttled(Exception):
    """Метод ещё нельзя вызывать — не жжём квоту."""
    def __init__(self, host, path, left):
        self.left = left
        super().__init__("%s%s: ещё %d с до следующего вызова (%s)"
                         % (host, path, left, hhmm(left)))

def hhmm(sec):
    sec = int(sec)
    if sec < 60:
        return "%d с" % sec
    if sec < 3600:
        return "%d мин" % (sec // 60)
    return "%d ч %d мин" % (sec // 3600, (sec % 3600) // 60)

MAX_WAIT = 300   # дольше пяти минут не ждём никогда: лучше честно сказать, сколько осталось


def call(host, path, method="GET", params=None, body=None, wait=False,
         timeout=120, agent="analyst", script=None, max_wait=MAX_WAIT,
         quota_key=None):
    """Запрос к WB API с уважением к лимитам.

    wait=False — если квота не восстановилась, сразу Throttled, запрос не тратится.
    wait=True  — подождём, но не больше max_wait секунд; иначе тоже Throttled.
    У WB встречаются окна по 1,5–8 часов, спать столько внутри скрипта бессмысленно.

    quota_key — учитывать лимит под другим путём. Нужно для отчётных методов:
    создание задания, проверка статуса и скачивание делят одну квоту, но у них
    разные URL (в путь входит task_id), поэтому своей записи им не завести.
    """
    qpath = quota_key or path
    left = quota_left(host, qpath)
    if left > 0:
        if not wait or left > max_wait:
            raise Throttled(HOST[host], qpath, left)
        time.sleep(left + 1)

    url = HOST[host] + path
    h = {"Authorization": TOKEN, "Content-Type": "application/json"}
    for attempt in range(3):
        r = requests.request(method, url, headers=h, params=params, json=body, timeout=timeout)
        if r.status_code == 429:
            retry = int(r.headers.get("X-Ratelimit-Retry") or r.headers.get("Retry-After") or 60)
            _mark(host, qpath, retry_after=retry)
            if not wait or retry > max_wait:
                raise Throttled(HOST[host], qpath, retry)
            time.sleep(retry + 1)
            continue
        _mark(host, qpath)
        if r.status_code >= 500 and attempt < 2:
            time.sleep(5 * (attempt + 1))
            continue
        break

    if r.status_code != 200:
        raise RuntimeError("%s %s -> %s: %s" % (method, path, r.status_code, r.text[:300]))
    if not r.text.strip():
        return None
    return r.json()

# ---------------------------------------------------------------- БД

def db():
    import psycopg
    return psycopg.connect("dbname=wb_ops")

def q(sql, params=(), fetch="all"):
    with db() as c, c.cursor() as cur:
        cur.execute(sql, params)
        if cur.description is None:
            return None
        if fetch == "one":
            return cur.fetchone()
        return cur.fetchall()

def qd(sql, params=()):
    """Запрос с результатом в виде списка словарей."""
    import psycopg
    from psycopg.rows import dict_row
    with psycopg.connect("dbname=wb_ops", row_factory=dict_row) as c, c.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall() if cur.description else None

# ---------------------------------------------------------------- журнал

def run_start(script, details=None):
    if db_log:
        return db_log.agent_run_start("analyst", script, details)
    return None

def run_end(rid, status="ok", rows=None, message=None):
    if db_log and rid:
        db_log.agent_run_end(rid, status=status, rows_out=rows, message=message)

def event(action, severity="info", **kw):
    if db_log:
        db_log.log_event("analyst", action, severity=severity, component="analyst", **kw)

# ---------------------------------------------------------------- утилиты

def num(v, default=0):
    """WB отдаёт суммы то числом, то строкой, иногда с запятой."""
    if v is None or v == "":
        return default
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).replace(" ", "").replace(" ", "").replace("₽", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return default

def money(v):
    return "{:,.0f} ₽".format(round(num(v))).replace(",", " ")

def pct(v, digits=1):
    return ("{:,.%df}%%" % digits).format(num(v))

def setting(key, default=None):
    r = q("SELECT value FROM wb.setting WHERE key=%s", (key,), fetch="one")
    return r[0] if r else default

def set_setting(key, value, note=None):
    q("""INSERT INTO wb.setting (key, value, note) VALUES (%s,%s,%s)
         ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value,
           note=COALESCE(EXCLUDED.note, wb.setting.note), updated_at=now()""",
      (key, str(value), note))
