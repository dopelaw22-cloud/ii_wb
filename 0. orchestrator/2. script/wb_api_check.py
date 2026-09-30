#!/usr/bin/env python3
"""Проверка доступа к WB API: ping по всем разделам + информация о продавце.
Токен берётся из .env в корне проекта. Запуск:
    /home/wb/oplog/venv/bin/python "0. orchestrator/2. script/wb_api_check.py"
"""
import sys, json, base64, datetime, pathlib
import requests

ROOT = pathlib.Path(__file__).resolve().parents[2]


def load_env(path=ROOT / ".env"):
    env = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


HOSTS = {
    "Общий":            "https://common-api.wildberries.ru",
    "Статистика":       "https://statistics-api.wildberries.ru",
    "Маркетплейс":      "https://marketplace-api.wildberries.ru",
    "Цены и скидки":    "https://discounts-prices-api.wildberries.ru",
    "Вопросы и отзывы": "https://feedbacks-api.wildberries.ru",
    "Продвижение":      "https://advert-api.wildberries.ru",
    "Аналитика":        "https://seller-analytics-api.wildberries.ru",
    "Поставки":         "https://supplies-api.wildberries.ru",
    "Возвраты":         "https://returns-api.wildberries.ru",
    "Документы":        "https://documents-api.wildberries.ru",
    "Контент":          "https://content-api.wildberries.ru",
}


def token_info(tok):
    p = tok.split(".")[1]
    p += "=" * (-len(p) % 4)
    d = json.loads(base64.urlsafe_b64decode(p))
    exp = datetime.datetime.fromtimestamp(d["exp"])
    left = (exp - datetime.datetime.now()).days
    return d, exp, left


def main():
    env = load_env()
    tok = env.get("WB_API_TOKEN", "")
    if not tok:
        sys.exit("WB_API_TOKEN пуст — проверь .env")
    d, exp, left = token_info(tok)
    print("Токен: продавец sid=%s · истекает %s (осталось %d дн.)" % (d.get("sid"), exp.strftime("%Y-%m-%d"), left))
    print("Контур: %s\n" % ("тестовый" if d.get("t") else "боевой"))

    h = {"Authorization": tok}
    ok = fail = 0
    for name, base in HOSTS.items():
        try:
            r = requests.get(base + "/ping", headers=h, timeout=20)
            body = r.text[:70].replace("\n", " ")
            if r.status_code == 200:
                ok += 1
                mark = "OK "
            else:
                fail += 1
                mark = "нет"
            print("  %s %-18s %s  %s" % (mark, name, r.status_code, body))
        except Exception as e:
            fail += 1
            print("  ERR %-18s %s: %s" % (name, type(e).__name__, str(e)[:60]))

    print("\nДоступно разделов: %d, недоступно: %d" % (ok, fail))

    try:
        r = requests.get("https://common-api.wildberries.ru/api/v1/seller-info", headers=h, timeout=20)
        print("\nseller-info [%s]: %s" % (r.status_code, r.text[:300]))
    except Exception as e:
        print("\nseller-info: ошибка %s" % e)


if __name__ == "__main__":
    main()
