# Менеджер WB — контекст и правила

## Магазин
- Продавец: **ИП Пермин Е. В.**, ИНН 224210289602
- Торговая марка: **«Вытяжки скрытого монтажа»**
- sid: `11f9b830-900f-5487-8d6d-b40746fa2bf8`
- Свой склад продавца: **Барнаул** (officeId 11155, storeId 314296, id 181829)
- Отзывов накоплено: ~2019. Рекламных кампаний: **121** (проверено 2026-10-07).
- Товаров: **12** — d100/d125 × чёрный/белый/серый × базовая/plus.
  Предмет один: «Вентиляторы для систем вентиляции» (subjectID 7306).
- Себестоимость: Google-таблица владельца, колонка **AJ** («Итого закуп. цена»).
  ID таблицы и колонка — в `.env` (`COST_SHEET_ID`, `COST_SHEET_GID`, `COST_SHEET_COL`).
  Базовые 795/805 ₽, plus 1500/1570 ₽.
- Налоговый режим: **не подтверждён владельцем**. В расчётах стоит УСН 6%
  (`wb.setting.tax_mode`), в отчётах помечено как предположение.

## Что это
Проект-помощник по маркетплейсу Wildberries. Живёт на сервере под пользователем `wb`,
полностью изолирован от проекта Ozon (`/home/ozon`).

## Структура
- `0. orchestrator/` — оркестратор, точка входа
  - `1. date/` — исходные данные (выгрузки, справочники)
  - `2. script/` — скрипты
  - `3. work/` — рабочие/промежуточные файлы
  - `4. reports/` — готовые отчёты
- `1. analyst/` — агент-аналитик (юнит-экономика, реклама, карточки, рекомендации).
  Та же структура плюс `5. service/` с юнит-файлом Telegram-бота.
- Агенты добавляются как `N. <имя>/` с той же внутренней структурой.

## Доступ к API
Токен — `WB_API_TOKEN` в `.env` в корне проекта (права 600, вне git).
Боевой контур, действует до **2027-04-01**.

Проверка доступа: `/home/wb/oplog/venv/bin/python "0. orchestrator/2. script/wb_api_check.py"`

Проверено 2026-09-30 — работают все разделы:
Контент, Аналитика, Цены и скидки, Маркетплейс, Статистика, Продвижение,
Вопросы и отзывы, Рекомендации, Поставки, Возвраты, Документы.

### Базовые хосты
| Раздел | Хост |
|---|---|
| Общий / seller-info / тарифы | `common-api.wildberries.ru` |
| Статистика | `statistics-api.wildberries.ru` |
| **Финансы (отчёт реализации)** | `finance-api.wildberries.ru` |
| Маркетплейс | `marketplace-api.wildberries.ru` |
| Цены и скидки | `discounts-prices-api.wildberries.ru` |
| Вопросы и отзывы | `feedbacks-api.wildberries.ru` |
| Продвижение | `advert-api.wildberries.ru` |
| Аналитика | `seller-analytics-api.wildberries.ru` |
| Поставки | `supplies-api.wildberries.ru` |
| Возвраты | `returns-api.wildberries.ru` |
| Документы | `documents-api.wildberries.ru` |
| Контент | `content-api.wildberries.ru` |

### Устаревшие методы — не использовать
Проверено живыми запросами 2026-10-07, все отвечают 404:

| Было | Стало |
|---|---|
| `statistics/api/v1/supplier/stocks` | `analytics/api/v1/warehouse_remains` (отчётом) |
| `statistics/api/v5/supplier/reportDetailByPeriod` | `finance/api/finance/v1/sales-reports/detailed` |
| `analytics/api/v2/nm-report/detail` | `analytics/api/analytics/v3/sales-funnel/products` |
| `advert/adv/v1/promotion/adverts` | `advert/api/advert/v2/adverts` |
| `advert/adv/v2/fullstats` | `advert/adv/v3/fullstats` |

Перед встраиванием метода в скрипт — сверяться с OpenAPI-спецификацией, а не
с памятью: WB переносит методы между хостами и версиями без обратной совместимости.
Спецификации удобно брать отсюда (автосинхронизация с WB, без авторизации):
`https://raw.githubusercontent.com/eslazarev/wildberries-sdk/main/specs/<NN-раздел>.yaml`
(`01-general`, `02-items`, `08-promotion`, `11-analytics`, `12-reports`, `13-finances`).
Сайт `dev.wildberries.ru` на прямую загрузку отвечает HTTP 498.

### Лимиты API — главное ограничение проекта
Замерено по заголовку `X-Ratelimit-Reset` 2026-10-07. Окна измеряются **часами**,
а не минутами, и расходуются даже неудачной попыткой:

| Метод | Окно |
|---|---|
| финотчёт реализации | **~8 ч** (27 995 с) |
| продажи / заказы | **~1,7 ч** (6 095 с) |
| воронка продаж | **~24 мин** (1 444 с) |
| тарифы box | ~30 мин |
| статистика рекламы | ~1 мин |
| карточки, цены | без заметных ограничений |

Отсюда железное следствие: **данные кешируются в БД `wb_ops`, схема `wb`.**
Отчёты собираются из кеша и никогда не ходят в API напрямую. Все вызовы идут
через `wb_lib.call()`, который помнит время последнего обращения в
`1. analyst/3. work/api_state.json` и не тратит попытку впустую.
Текущее состояние окон — `python views.py` или команда `/лимиты` в боте.

### Рабочие методы на 2026-10-07
`common/api/v1/tariffs/commission`, `common/api/v1/tariffs/box`,
`content/content/v2/get/cards/list`, `prices/api/v2/list/goods/filter`,
`stats/api/v1/supplier/sales`, `stats/api/v1/supplier/orders`,
`finance/api/finance/v1/sales-reports/detailed`, `finance/api/v1/account/balance`,
`analytics/api/analytics/v3/sales-funnel/products` (+`/history`),
`analytics/api/v1/warehouse_remains`, `analytics/api/v1/paid_storage`,
`advert/adv/v1/promotion/count`, `advert/api/advert/v2/adverts`, `advert/adv/v3/fullstats`.

Не хватает прав токена: `analytics/api/analytics/v1/stocks-report/wb-warehouses`
отвечает 403 «token does not satisfy additional requirements».

## Железные правила
- Секреты только в `.env` (права 600), в git не попадают.
- Никаких обращений к `/home/ozon`, `ozon_ops`, `@ozon_permin_bot` — это чужой контур.
- Журнал изменений — в БД `wb_ops` через `/home/wb/oplog/db_log.py`.
- Перед записью в WB (цены, поставки, ответы на отзывы) — подтверждение владельца.
