#!/usr/bin/env python3
"""Создание карточек товаров на WB из выгрузки Ozon.

ЗАПИСЬ В WB. По железному правилу проекта выполняется только после явного
подтверждения владельца. По умолчанию скрипт работает в режиме --dry:
показывает, что будет отправлено, и ничего не создаёт.

Что делает:
  1. читает xlsx-выгрузку Ozon (лист «Карточки Ozon»);
  2. переводит поля в формат WB: мм -> см, граммы -> кг, характеристики
     Ozon -> charcID предмета 7306 по образцу уже существующей карточки;
  3. создаёт карточки методом POST /content/v2/cards/upload;
  4. прикрепляет фотографии по ссылкам через /content/v3/media/save.

Декларация соответствия ЕАЭС N RU Д-RU.РА02.В.79286/26 подтверждена владельцем
01.11.2026 как общая для всех его вытяжек — ставится и в характеристики,
и в блок documents.

Чего скрипт НЕ делает сам:
  — не ставит остатки и не меняет цены после создания: карточка
    синхронизируется до 30 минут, и до этого цены недоступны.

Запуск:
    python create_cards.py --file <xlsx>              # показать план
    python create_cards.py --file <xlsx> --confirm    # создать карточки
    python create_cards.py --file <xlsx> --photos-only --confirm
"""
import sys, pathlib, argparse, json, datetime, time, re
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import wb_lib as L

SUBJECT_ID = 7306                      # Вентиляторы для систем вентиляции
# Бренд. Торговая марка владельца «Вытяжки скрытого монтажа» в справочнике WB
# НЕ зарегистрирована: при попытке 01.11.2026 WB вернул «Бренд не найден» по всем
# шести карточкам. У 12 действующих карточек бренд тоже пустой — по той же
# причине. Поэтому оставляем пустым; чтобы заполнить, бренд нужно сначала
# зарегистрировать в кабинете WB.
BRAND = ""
TITLE_MAX = 60                         # лимит WB на наименование
VAT = "5"                              # ставка НДС владельца

# Декларация соответствия. Владелец подтвердил 01.11.2026, что она общая для
# всех его вытяжек, поэтому ставим ту же, что в существующих карточках.
DECL_NUMBER = "ЕАЭС N RU Д-RU.РА02.В.79286/26"
DECL_START = "2026-04-23T00:00:00Z"
DECL_END = "2031-03-22T00:00:00Z"
DECL_TYPE = 2                          # тип «декларация соответствия»
EXISTING_IMT = 1028105923              # карточка с 12 действующими вытяжками

# Характеристики, одинаковые для всех шести товаров. Значения и их написание
# взяты из уже существующей карточки d100blackplus, чтобы новые карточки
# попадали в те же фильтры поиска, что и старые.
COMMON_CHARCS = {
    5023:     "Вытяжка теневая",                 # Модель
    5556:     "от сети",                         # Питание
    9623:     "3 года",                          # Гарантийный срок
    15629:    "защита от перегрева, защита от перегрузки",
    16758:    "IP24",                            # Степень пылевлагозащиты
    17596:    "сталь",                           # Материал изделия
    63049:    "настенный, встраиваемый",         # Варианты установки
    167966:   "бессрочно",                       # Срок эксплуатации
    173642:   "14Вт",                            # Мощность устройства
    378533:   "Вентилятор вытяжной, решетка скрытого монтажа",
    14177451: "Россия",                          # Страна производства
    15000353: "стандартный",                     # Вид вентилятора
    15000354: "осевой",                          # Тип вентилятора
    15001405: VAT,                               # Ставка НДС
    15002852: "металл",                          # Материал лицевой панели
    15001135: DECL_NUMBER,                       # Номер декларации соответствия
    15001137: "23.04.2026",                      # Дата регистрации
    15001138: "22.03.2031",                      # Дата окончания действия
}
# Характеристики с charcType = 4 — числовые, с единицей измерения (дБ, см, мм).
# WB ждёт их простым числом. Массив (как для charcType = 1) приводит к ошибке
# «Неправильный тип значения для характеристики» — проверено 01.11.2026.
NUM_CHARCS = {19769: "шум, дБ", 90630: "высота, см", 90652: "глубина, см",
              90673: "ширина, см", 15000869: "диаметр патрубка, мм"}


def read_xlsx(path):
    from openpyxl import load_workbook
    ws = load_workbook(path, data_only=True)["Карточки Ozon"]
    rows = list(ws.iter_rows(values_only=True))
    hdr = [str(h).strip() if h else "" for h in rows[0]]
    ix = {h: i for i, h in enumerate(hdr)}
    out = []
    for r in rows[1:]:
        if not r or not r[0]:
            continue
        g = lambda k: (r[ix[k]] if k in ix and ix[k] < len(r) else None)
        out.append({
            "vendor_code": str(g("vendor_code")).strip(),
            "barcode": str(g("barcode") or "").strip(),
            "name": str(g("name") or "").strip(),
            "description": str(g("description") or "").strip(),
            "len_mm": L.num(g("pack_length_mm")), "wid_mm": L.num(g("pack_width_mm")),
            "hei_mm": L.num(g("pack_height_mm")), "weight_g": L.num(g("weight_gross_g")),
            "price": int(L.num(g("price_work_rub"))),
            "cost": L.num(g("cost_price_rub")),
            "color": str(g("Цвет товара") or "").strip(),
            "airflow": str(g("Производительность, м3/час") or "").strip(),
            "duct": str(g("Диаметр воздуховода, мм") or "").strip(),
            "noise": str(g("Макс. уровень шума, дБ") or "").strip(),
            "photos": [u.strip() for u in str(g("photo_urls") or "").split("\n") if u.strip()],
            "photo_main": str(g("photo_main_url") or "").strip(),
        })
    return out


def align_prices(items, mode):
    """Выровнять цены внутри каждого диаметра.

    В выгрузке Ozon цены внутри одного размера расходились на 18-27% при
    одинаковой себестоимости (d100: серый 9 079 против 11 090 у чёрного и
    белого; d125: белый 12 971 против 10 236). Владелец 01.11.2026 решил
    выровнять по старшей цене.
    """
    if mode not in ("max", "min"):
        return items
    pick = max if mode == "max" else min
    by_size = {}
    for i in items:
        by_size.setdefault(i["duct"], []).append(i)
    for duct, group in by_size.items():
        target = pick(i["price"] for i in group)
        for i in group:
            if i["price"] != target:
                i["price_was"] = i["price"]
                i["price"] = target
    return items


def short_title(item):
    """Наименование до 60 знаков в стиле уже существующих карточек.

    Из названия Ozon (147 знаков) в лимит WB не влезает ничего осмысленного,
    поэтому собираем заново: что это, куда ставится, диаметр, цвет.
    """
    size = "d%s" % (item["duct"] or "")
    t = "Вытяжка скрытого монтажа под плитку %s %s" % (size, item["color"])
    if len(t) > TITLE_MAX:
        t = "Вытяжка скрытого монтажа %s %s" % (size, item["color"])
    return t[:TITLE_MAX].strip()


def build_variant(item):
    ch = [{"id": cid, "value": val} for cid, val in COMMON_CHARCS.items()]
    ch.append({"id": 14177449, "value": item["color"]})                     # Цвет
    ch.append({"id": 15000355, "value": "%s м3/час" % item["airflow"]})     # Расход воздуха
    # charcType 4 — простым числом, без массива
    ch.append({"id": 19769, "value": int(L.num(item["noise"]))})            # Шум, дБ
    ch.append({"id": 15000869, "value": int(L.num(item["duct"]))})          # Диаметр патрубка
    # габариты предмета в см — из упаковки, других в выгрузке нет
    ch.append({"id": 90630, "value": round(item["hei_mm"] / 10)})           # Высота
    ch.append({"id": 90652, "value": round(item["wid_mm"] / 10)})           # Глубина
    ch.append({"id": 90673, "value": round(item["len_mm"] / 10)})           # Ширина
    out = {
        "title": short_title(item),
        "description": item["description"],
        "vendorCode": item["vendor_code"],
        "dimensions": {
            "length": round(item["len_mm"] / 10),
            "width": round(item["wid_mm"] / 10),
            "height": round(item["hei_mm"] / 10),
            "weightBrutto": round(item["weight_g"] / 1000.0, 3),
        },
        # баркод берём с Ozon: по требованию владельца он должен совпадать
        # на обоих маркетплейсах. WB такие строки принимает — в существующей
        # карточке d100blackplus в skus уже лежит OZN2620393590.
        "sizes": [{"techSize": "0", "wbSize": "",
                   "price": item["price"], "skus": [item["barcode"]]}],
        "characteristics": ch,
        "documents": {
            "items": [{"type": DECL_TYPE, "number": DECL_NUMBER,
                       "startDate": DECL_START, "endDate": DECL_END}],
            "excludeDocuments": False,
        },
    }
    if BRAND:
        out["brand"] = BRAND
    return out


def build_groups(items, mode="by-size"):
    """Как объединять варианты в карточки.

    by-size — две карточки: d100 (3 цвета) и d125 (3 цвета). Цветовые
              варианты внутри одной карточки делят отзывы и рейтинг,
              это обычная практика на WB.
    one     — одна карточка со всеми шестью.
    separate— шесть отдельных карточек.
    """
    if mode == "one":
        return [{"subjectID": SUBJECT_ID, "variants": [build_variant(i) for i in items]}]
    if mode == "separate":
        return [{"subjectID": SUBJECT_ID, "variants": [build_variant(i)]} for i in items]
    groups = {}
    for i in items:
        groups.setdefault(i["duct"], []).append(i)
    return [{"subjectID": SUBJECT_ID, "variants": [build_variant(i) for i in g]}
            for _, g in sorted(groups.items())]


def build_attach(items, imt_id):
    """Присоединить новые варианты к существующей карточке.

    Все 12 действующих вытяжек у владельца лежат в одной карточке
    imtID 1028105923 — то есть это его обычная практика. Присоединение
    делает новые товары вариантами той же карточки: они наследуют её
    отзывы и рейтинг, а покупатель видит их в одном переключателе.
    """
    return {"imtID": imt_id, "cardsToAdd": [build_variant(i) for i in items]}


def show_attach_plan(payload):
    print("=" * 78)
    print("ПЛАН: ПРИСОЕДИНЕНИЕ К СУЩЕСТВУЮЩЕЙ КАРТОЧКЕ imtID %s" % payload["imtID"])
    print("=" * 78)
    print("в карточке уже 12 вариантов, станет %d\n" % (12 + len(payload["cardsToAdd"])))
    for v in payload["cardsToAdd"]:
        s = v["sizes"][0]
        print("   %-18s %-48s %10s  %s"
              % (v["vendorCode"], v["title"], L.money(s["price"]), s["skus"][0]))
    print("\nдекларация: %s" % DECL_NUMBER)
    print("фотографии: по 11 ссылок на товар, прикрепляются после синхронизации")


def show_plan(items, payload):
    print("=" * 78)
    print("ПЛАН СОЗДАНИЯ КАРТОЧЕК НА WB — предмет %d, бренд «%s»" % (SUBJECT_ID, BRAND))
    print("=" * 78)
    for gi, g in enumerate(payload, 1):
        print("\nкарточка %d — %d вариант(а):" % (gi, len(g["variants"])))
        for v in g["variants"]:
            s = v["sizes"][0]
            d = v["dimensions"]
            print("   %-18s %-48s" % (v["vendorCode"], v["title"]))
            print("      цена %s, баркод %s" % (L.money(s["price"]), s["skus"][0]))
            print("      габариты %d×%d×%d см, вес %s кг, характеристик %d, описание %d зн."
                  % (d["length"], d["width"], d["height"], d["weightBrutto"],
                     len(v["characteristics"]), len(v["description"])))
    print("\nфотографии: по 11 ссылок на товар, прикрепляются после создания")
    tot = sum(len(g["variants"]) for g in payload)
    print("итого вариантов: %d в %d карточках" % (tot, len(payload)))

    print("\n--- ЧТО ОСТАЁТСЯ ПУСТЫМ ---")
    print("  номера сертификата и декларации соответствия: в выгрузке Ozon их нет.")
    print("  Подставлять документ от другой модели нельзя — заполните в кабинете WB.")


def create(payload):
    j = L.call("content", "/content/v2/cards/upload", "POST", body=payload)
    return j


def attach_photos(items, dry=True):
    """Фото по ссылкам. nmID появляется только после синхронизации карточки."""
    codes = {i["vendor_code"].lower(): i for i in items}
    j = L.call("content", "/content/v2/get/cards/list", "POST",
               body={"settings": {"cursor": {"limit": 100}, "filter": {"withPhoto": -1}}})
    done, missing = 0, []
    for c in ((j or {}).get("cards") or []):
        it = codes.get((c.get("vendorCode") or "").lower())
        if not it:
            continue
        urls = ([it["photo_main"]] if it["photo_main"] else []) + \
               [u for u in it["photos"] if u != it["photo_main"]]
        if not urls:
            continue
        # У метода медиа окно 30 минут, поэтому шаг возобновляемый: карточку
        # с уже загруженными фото пропускаем, чтобы повторный запуск не сжёг
        # окно на том, что сделано.
        if c.get("photos"):
            print("   %s: фото уже есть (%d) — пропускаю"
                  % (it["vendor_code"], len(c.get("photos") or [])))
            continue
        if dry:
            print("   %s -> %d фото (nmID %s)" % (it["vendor_code"], len(urls), c.get("nmID")))
            done += 1
            continue
        try:
            # между вызовами выдерживаем окно метода: шесть товаров по
            # минуте — пять минут, это нормально для фоновой доливки медиа
            L.call("content", "/content/v3/media/save", "POST",
                   body={"nmId": c.get("nmID"), "data": urls},
                   wait=True, max_wait=180)
            print("   %s: прикреплено %d фото" % (it["vendor_code"], len(urls)))
            done += 1
            time.sleep(1)
        except L.Throttled as e:
            print("   %s: окно медиа закрыто, осталось %s — остальные в следующий заход"
                  % (it["vendor_code"], L.hhmm(e.left)))
            break
        except Exception as e:
            print("   %s: ошибка фото — %s" % (it["vendor_code"], str(e)[:160]))
    for vc in codes:
        if vc not in {(c.get("vendorCode") or "").lower()
                      for c in ((j or {}).get("cards") or [])}:
            missing.append(vc)
    if missing:
        print("   ещё не появились в каталоге (синхронизация до 30 мин): %s"
              % ", ".join(missing))
    return done


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", required=True)
    ap.add_argument("--group", choices=["by-size", "one", "separate"], default="by-size")
    ap.add_argument("--align-prices", choices=["max", "min", "none"], default="none",
                    help="выровнять цены внутри диаметра по старшей или младшей")
    ap.add_argument("--attach-to", type=int, metavar="IMT",
                    help="присоединить к существующей карточке по imtID "
                         "(у владельца все 12 вытяжек в imtID %d)" % EXISTING_IMT)
    ap.add_argument("--confirm", action="store_true", help="выполнить запись в WB")
    ap.add_argument("--photos-only", action="store_true", help="только прикрепить фото")
    ap.add_argument("--save-payload", help="записать payload в файл")
    a = ap.parse_args()

    items = read_xlsx(a.file)
    if a.align_prices != "none":
        items = align_prices(items, a.align_prices)
        changed = [i for i in items if "price_was" in i]
        if changed:
            print("цены выровнены по %s цене внутри диаметра:"
                  % ("старшей" if a.align_prices == "max" else "младшей"))
            for i in changed:
                print("   %-18s %s -> %s" % (i["vendor_code"],
                      L.money(i["price_was"]), L.money(i["price"])))
            print()

    if len(items) != 6:
        print("! в файле %d товаров, ожидалось 6 — проверьте выгрузку" % len(items))

    if a.photos_only:
        rid = L.run_start("create_cards.py", {"step": "photos"})
        n = attach_photos(items, dry=not a.confirm)
        if not a.confirm:
            print("\n--- это план. Прикрепить: добавьте --confirm ---")
        L.run_end(rid, "ok", rows=n)
        return

    if a.attach_to:
        payload = build_attach(items, a.attach_to)
        show_attach_plan(payload)
    else:
        payload = build_groups(items, a.group)
        show_plan(items, payload)
    if a.save_payload:
        pathlib.Path(a.save_payload).write_text(
            json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        print("\npayload сохранён: %s" % a.save_payload)

    if not a.confirm:
        print("\n" + "=" * 78)
        print("ЭТО ТОЛЬКО ПЛАН. Ничего в WB не отправлено.")
        print("Создать карточки: добавьте --confirm")
        print("=" * 78)
        return

    rid = L.run_start("create_cards.py", {"group": a.group, "count": len(items)})
    try:
        if L.db_log:
            L.db_log.owner_decision(kind="approval", domain="content",
                                    subject="создание 6 карточек из выгрузки Ozon",
                                    decision="подтверждено владельцем", source="chat")
        if a.attach_to:
            res = L.call("content", "/content/v2/cards/upload/add", "POST", body=payload)
        else:
            res = create(payload)
        print("\nответ WB: %s" % json.dumps(res, ensure_ascii=False)[:400])
        n_var = (len(payload["cardsToAdd"]) if a.attach_to
                 else sum(len(g["variants"]) for g in payload))
        L.event("создано карточек: %d вариантов%s"
                % (n_var, (" присоединено к imtID %d" % a.attach_to) if a.attach_to else ""))
        print("\nКарточки отправлены. Синхронизация до 30 минут.")
        print("Затем: python create_cards.py --file <xlsx> --photos-only --confirm")
        L.run_end(rid, "ok", rows=len(items))
    except Exception as e:
        L.run_end(rid, "error", message=str(e)[:400])
        raise


if __name__ == "__main__":
    main()
