#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
nocodb_client.py - upserts one extracted tender record into the NocoDB table
"Реестр конкурсов" (TenderDataExtractor / StrokTenderDataExtractor project).

IMPORTANT - where this must run:
    Network access to app.nocodb.com is only allowed from Claude's cloud
    execution environment (added to the account's domain allowlist under
    Settings -> Capabilities -> Allow network egress -> Additional allowed
    domains). It is NOT reachable from the local device shell that mounts
    this project folder (separate, more restrictive network sandbox). So
    this script must be executed in the cloud environment, not via the
    local device shell - even though the file itself lives in the project
    folder for documentation/versioning purposes.

Usage:
    python nocodb_client.py --json /path/to/extracted_fields.json \
        --token-file /path/to/API_key_NocoDB.txt

    extracted_fields.json uses the same flat field names as save_tender.py
    (see FIELD_MAP / CSV_COLUMNS in save_tender.py) - the LLM only needs to
    produce ONE json dict and pass it to both save_tender.py and this script.
    This includes "internal_number" (Шаг 1a в CLAUDE.md) - save_tender.py is
    normally run first and its result JSON carries the resolved internal_number
    back, which should be merged into this JSON before calling this script, so
    the same auto-assigned/reused value ends up in NocoDB too instead of this
    script trying to assign its own.

Optionally, pass --xlsx to also sync the "Позиции и цены" specification table
(Шаг 5.1 в CLAUDE.md) into its OWN NocoDB table - one table per tender, named
exactly as the tender's internal number («Внутренний номер», e.g. "К929";
since 0.1.14 - earlier tables were "Спецификация — <номер закупки>") - and write a link to it into the main
record's "Ссылка на спецификацию" column, so the full item-level breakdown can
always be found later from the main tender row.

Prints a JSON result to stdout:
    {"action": "created"|"updated", "record_id": <int>, "purchase_number": "...",
     "verification": {"ok": bool, "checked_fields": int, "issues": [...]},
     "spec_table_url": "..." (only if --xlsx was given),
     "spec_verification": {"ok": bool, "expected_rows": int, "actual_rows": int,
                            "issues": [...]} (only if --xlsx was given)}

verification / spec_verification (added 2026-09-09): right after writing, this
script re-reads the record (and, with --xlsx, every row of the spec table)
back from NocoDB and compares it against what THIS run intended to send -
this is what catches the class of bug already seen twice on this project
("Файл-источник" and "Номер конкурса" both went empty in NocoDB while the
script itself printed a success result) automatically, instead of relying on
someone noticing by chance later. "ok": false means at least one field/row
came back different from what was sent (most often empty) - "issues" lists
exactly which ones. This never raises or changes the exit code by itself
(the create/update already succeeded by the time verification runs) -
callers must check "ok"/"issues" and tell the user, rather than assume
success just because the process exited cleanly.
"""

import argparse
import json
import re
import sys
import urllib.request
import urllib.error
import urllib.parse
from datetime import date

WORKSPACE_ID = "wzs963xn"
BASE_ID = "pf3o1felxit8s5b"
BASE_URL = "https://app.nocodb.com/api/v2/tables"
META_URL = "https://app.nocodb.com/api/v2/meta"
TABLE_ID = "mdbebnsirecunr5"
RECORDS_URL = f"{BASE_URL}/{TABLE_ID}/records"
SPEC_LINK_COLUMN = "Ссылка на спецификацию"

# Column order for the per-tender specification table - must match the
# "Позиции и цены" template in CLAUDE.md, Шаг 5.1.
# (title, uidt, extra column props for the create-table API call or None).
# Кол-во / НМЦК за позицию / Рыночная цена, руб. are typed Decimal/Currency
# (RU locale, comma decimal) instead of plain text; Источник (ссылка) is
# typed URL. Verified empirically against a scratch table on this base
# (2026-09-04, then deleted) before wiring this in: Decimal/Currency reject
# any non-numeric value and fail the WHOLE bulk insert (not just that
# field/row) - see NUMERIC_SPEC_COLUMNS/sanitize_spec_row below for how
# placeholders like "Не указано в документах" are handled so that stays
# safe. URL, by contrast, does NOT validate its value at the API level at
# all (an invalid or even "url1; url2" string is accepted as-is), so
# "Источник (ссылка)" needs no special handling despite sometimes holding
# two links joined by "; " (Шаг 5.1 в CLAUDE.md).
#
# "Рыночная цена (найдено в интернете)" stays plain LongText on purpose
# (reverted 2026-09-04, tender RAD260041848 exposed this): it routinely
# holds mixed-currency, approximate, free-text values straight from web
# search - e.g. "≈3 500 € за шт. (ориентировочно; ...)" or "$12 940,80
# (зарубежный дистрибьютор, ...)" - not just RUB numbers with commentary.
# Coercing THAT text into a numeric column would silently grab the first
# digit run regardless of currency (reading "3500" out of a €-quote and
# storing it as if it were 3500 RUB - very wrong). The clean, always-RUB,
# comment-free number the user wants for actual calculations goes in the
# separate "Рыночная цена, руб." column instead (added 2026-09-04) - the
# caller is responsible for converting non-RUB quotes to RUB before putting
# a value there (see Шаг 5.1 в CLAUDE.md for where that conversion happens);
# this module does not do currency conversion itself.
# isLocaleString: best-effort attempt at a RU-style comma decimal display
# for a plain Decimal column (Currency has a dedicated currency_locale meta
# that forces ru-RU for any viewer; Decimal has no confirmed equivalent -
# this key comes from a NocoDB PR referencing `meta.value.isLocaleString`
# -> `.toLocaleString(undefined, ...)`, which follows the viewer's OWN
# browser locale rather than forcing one. Not independently confirmed in
# the NocoDB UI - a check via browser was interrupted by a tool glitch
# 2026-09-04. If it doesn't show a comma for the user, drop this meta key
# without worry, the stored value/precision (dtxs=2) is correct either way.
# separator: the NocoDB UI's "Thousands and decimal separators" dropdown (screenshot from
# the user, 2026-09-07: field "НМЦК за позицию" set to "None and comma (1000000,00)" - no
# thousands separator, comma decimal point). Confirmed empirically by comparing that field's
# meta via GET /meta/tables/{id} after the user set it through the NocoDB UI and clicked
# "Update Field" - the previously-undocumented key is meta.separator, value "none_comma".
# (The isLocaleString key above does NOT control this dropdown - see the comment on it; it's
# a different, apparently cosmetic key kept only because it's harmless.) Applied to every
# existing "НМЦК за позицию"/"Рыночная цена, руб." Decimal column across all spec tables via
# a one-off PATCH /meta/columns/{id} on 2026-09-07 in addition to being baked in here for any
# spec table created from now on. "Кол-во" (QTY_DECIMAL_META) is a quantity, not a price, and
# was deliberately left on the default separator - only prices were in scope for this change.
# precision: the NocoDB UI's Decimal "Precision" dropdown (how many decimal
# digits are shown, e.g. "1.00") is driven by meta.precision, which is a
# SEPARATE setting from dtxs (the actual Postgres column scale). Setting
# dtxs alone stores the right value but the UI still showed "1.0" (1 digit)
# until meta.precision was added too - confirmed empirically 2026-09-04
# (user screenshot) by probing a scratch table and comparing to the live
# "Рыночная цена, руб." column, then fixing it via a PATCH to
# /meta/columns/{id}. Both must always be set together and kept in sync.
RU_DECIMAL_META = {"dtxp": 15, "dtxs": 2, "meta": {"isLocaleString": True, "precision": 2, "separator": "none_comma"}}
QTY_DECIMAL_META = {"dtxp": 15, "dtxs": 3, "meta": {"precision": 3}}
ANALOG_COLUMN = "Аналог допускается"
TZ_COLUMN = "Техническое задание"
ANALOG_TEXT_COLUMN = "Аналог"
DOC_ERRORS_COLUMN = "Ошибки в документации"
ANALOG_STATUS_COLUMN = "Аналог (статус)"
# --- Цены агента (добавлено 2026-10-07, версия 0.1.15, ТЗ «TZ-NocoDB 1»,
# таблица 1.1 «Поля, заполняемые агентом»; Шаг 5.1f в CLAUDE.md) ---
# Стоимость НМЦ и до трёх найденных агентом вариантов цены (руб. с НДС за
# единицу), после каждой цены - её комментарий (источник, ссылка, пояснение).
# Все цены - Decimal в одном формате (RU_DECIMAL_META), как другие денежные
# колонки. В таблице спецификации для них создаётся отдельное представление
# PRICE_VIEW_TITLE (см. ensure_price_view()) с порядком полей строго по ТЗ.
NMC_COST_COLUMN = "Стоимость НМЦ"
AGENT_PRICE_PAIRS = [(f"Цена агента {i}", f"Комментарий агента {i}") for i in (1, 2, 3)]
AGENT_PRICE_COLUMNS = tuple(p for p, _ in AGENT_PRICE_PAIRS)
AGENT_FIELDS = [NMC_COST_COLUMN] + [c for pair in AGENT_PRICE_PAIRS for c in pair]
PRICE_VIEW_TITLE = "Цены агента"
# Поля-идентификаторы позиции, которые показываются в представлении перед
# полями ТЗ (иначе непонятно, к какой позиции относится цена).
PRICE_VIEW_LEAD_COLUMNS = ["№", "Наименование позиции", "Марка/модель", "Кол-во", "Ед. изм."]
# --- Стоимость товара по рынкам (добавлено 2026-10-09, версия 0.1.16, реестр
# полей «Паспорта полей — Закупщик», поля 128-151; Шаг 5.1g в CLAUDE.md) ---
# Три рынка: РФ (кроме Avito), Avito, зарубеж (приоритет Германия, Китай, США).
# По каждому - до трёх лучших подтверждённых предложений: цена (Decimal, руб.
# с НДС за единицу, формат RU_DECIMAL_META) и ссылка на предложение (URL),
# порядок: предложение 1, ссылка 1, предложение 2, ссылка 2, ...
MARKET_OFFER_PREFIXES = [
    ("rf", "Стоимость товара в РФ"),
    ("avito", "Стоимость товара на Avito"),
    ("abroad", "Стоимость товара зарубежом"),
]
MARKET_OFFER_PAIRS = [(f"{prefix} - предложение {i}", f"{prefix} - ссылка {i}")
                      for _, prefix in MARKET_OFFER_PREFIXES for i in (1, 2, 3)]
MARKET_OFFER_PRICE_COLUMNS = tuple(p for p, _ in MARKET_OFFER_PAIRS)
MARKET_OFFER_LINK_COLUMNS = tuple(l for _, l in MARKET_OFFER_PAIRS)
MARKET_OFFER_FIELDS = [c for pair in MARKET_OFFER_PAIRS for c in pair]
MARKET_VIEW_TITLE = "Стоимость товара"
# Явные латинские имена колонок в БД. Без них NocoDB строит имя из длинного
# кириллического заголовка, обрезает его до лимита Postgres (63 байта) и
# «предложение 1» / «предложение 2» получают ОДНО имя -> HTTP 400 "The column
# already exists" (проверено 2026-10-09). Заголовок (title) - русский, как в реестре.
MARKET_OFFER_DB_NAMES = {f"{prefix} - {kind} {i}": f"{key}_{'offer' if kind == 'предложение' else 'link'}_{i}"
                         for key, prefix in MARKET_OFFER_PREFIXES for i in (1, 2, 3)
                         for kind in ("предложение", "ссылка")}
# --- Цены отдела закупок (добавлено 2026-10-12, версия 0.1.22, по просьбе
# пользователя) --- предложения, которые отдел закупок получил от
# поставщиков (КП, счета, звонки): наименование поставщика (текст) и цена
# (Decimal, руб. за единицу, RU_DECIMAL_META), плюс общий комментарий
# (условия, срок, НДС, № КП). Заполняются ВРУЧНУЮ или чат-ботом цен - не
# агентом при обработке тендера, поэтому при повторной синхронизации
# спецификации (таблица очищается и перезаполняется из xlsx) значения
# сохраняются и восстанавливаются по «№» позиции - см. read_manual_fields().
# С 0.1.25 (2026-10-16, просьба пользователя) комплектов «поставщик N / цена N»
# не 3, а PROCUREMENT_SLOTS = 10; комментарий по-прежнему один, после цены 10.
# Уже сохранённым спецификациям колонки 4-10 досоздаются только по указанию
# пользователя: --procurement-columns-only <номер> (только эти колонки) или
# --structure-only <номер> (вся структура и представления).
PROCUREMENT_SLOTS = 10
PROCUREMENT_PAIRS = [(f"Отдел закупок: поставщик {i}", f"Отдел закупок: цена {i}, руб.")
                     for i in range(1, PROCUREMENT_SLOTS + 1)]
PROCUREMENT_SUPPLIER_COLUMNS = tuple(s for s, _ in PROCUREMENT_PAIRS)
PROCUREMENT_PRICE_COLUMNS = tuple(p for _, p in PROCUREMENT_PAIRS)
PROCUREMENT_COMMENT_COLUMN = "Отдел закупок: комментарий"
PROCUREMENT_FIELDS = [c for pair in PROCUREMENT_PAIRS for c in pair] + [PROCUREMENT_COMMENT_COLUMN]
# Латинские имена колонок в БД - по той же причине, что MARKET_OFFER_DB_NAMES.
PROCUREMENT_DB_NAMES = {**{s: f"purch_supplier_{i}" for i, (s, _) in enumerate(PROCUREMENT_PAIRS, 1)},
                        **{p: f"purch_price_{i}" for i, (_, p) in enumerate(PROCUREMENT_PAIRS, 1)},
                        PROCUREMENT_COMMENT_COLUMN: "purch_comment"}
# Представление «Закупки» (роль «Закупщик», реестр полей; 0.1.16) - с 0.1.22
# первыми после PRICE_VIEW_LEAD_COLUMNS идут цены отдела закупок, затем поля
# Закупщика по реестру и 18 цен/ссылок по рынкам. Прежнее временное
# представление «Стоимость товара» (MARKET_VIEW_TITLE) с 0.1.37 не удаляется,
# а переименовывается в «Сбор цен» (COMPARE_VIEW_TITLE) - см.
# migrate_compare_view_title().
PURCHASE_VIEW_TITLE = "Закупки"
PURCHASE_VIEW_FIELDS = PROCUREMENT_FIELDS + [ANALOG_COLUMN, ANALOG_STATUS_COLUMN, "Производитель", ANALOG_TEXT_COLUMN,
                                             "Страна происхождения", DOC_ERRORS_COLUMN] + MARKET_OFFER_FIELDS
# --- Представление «Сбор цен» (0.1.26, 2026-10-16, просьба пользователя;
# до 0.1.37 называлось «Сравнение цен»; с 0.1.28 создаётся для каждой новой
# таблицы спецификации) --- с 0.1.40: позиция и 3 лучшие цены (поставщик /
# РФ / зарубеж, колонки с 0.1.39) - больше ничего (решение пользователя
# 26.10.2026); все цены строки - в основном представлении.
# Лучшие цены считают ФОРМУЛЫ NocoDB (колонки типа Formula) - пересчитываются
# сами при каждом открытии, обновлять представление после новой цены не нужно.
# Пустые цены не участвуют (MIN пропускает пустые). Цены с 🔴 (бот: не полное
# совпадение с позицией) участвуют - по решению пользователя «учитывать с
# пометкой», пометка 🔴 остаётся в имени поставщика. Проверено на временной
# тестовой таблице 2026-10-16: MIN пропускает пустые, IF без else - ошибка
# ERR_FORMULA (нужен BLANK() или ""), формула может ссылаться на формулу.
# Подсветка цветом на бесплатном тарифе NocoDB недоступна (строки - с Plus,
# ячейки - с Business, только в интерфейсе). 0.1.27-0.1.38: цена и 🏆 в одной
# ячейке (текстовые формулы-копии каждой цены). С 0.1.39 (решение
# пользователя 26.10.2026) копий и 🏆 нет: цены показываются своими числовыми
# колонками, а лучшая цена - в 3 отдельных колонках (COMPARE_BEST_COLUMNS).
# Название - «Сбор цен» (решение пользователя 21.10.2026, версия 0.1.37).
# Прежние названия этого представления переименовываются в него сами
# (migrate_compare_view_title(), по порядку списка): «Сравнение цен» (0.1.26-
# 0.1.36, К910, К939) и временное «Стоимость товара» (0.1.16, К925-К931).
COMPARE_VIEW_TITLE = "Сбор цен"
COMPARE_VIEW_OLD_TITLES = ["Сравнение цен", MARKET_VIEW_TITLE]
# С 0.1.38 (просьба пользователя 21.10.2026 «в Сборе цен - все цены из
# спецификации») видны ВСЕ 10 цен отдела закупок, каждая рядом со своим
# поставщиком. Формулы 0.1.26-0.1.37 «3 лучшие из 10» («Отдел закупок:
# лучшая цена 1-3» / «… поставщик лучшей цены 1-3») больше не нужны и
# удаляются (COMPARE_OBSOLETE_COLUMNS).
COMPARE_BEST_PRICE_COLUMNS = [f"Отдел закупок: лучшая цена {k}" for k in (1, 2, 3)]
COMPARE_BEST_SUPPLIER_COLUMNS = [f"Отдел закупок: поставщик лучшей цены {k}" for k in (1, 2, 3)]
COMPARE_MARKET_ORDER = [("РФ", "Стоимость товара в РФ", "rf"), ("зарубеж", "Стоимость товара зарубежом", "abroad"),
                        ("Avito", "Стоимость товара на Avito", "avito")]
# Как рынок называется в пометке Шага 5.1g в «Примечании» («РФ: найдено 1 из 3 [...]»)
COMPARE_NOTE_LABELS = {"rf": "РФ", "abroad": "Зарубеж", "avito": "Avito"}
# 0.1.27-0.1.38: каждая цена повторялась текстовой формулой с 🏆 («Цена
# отдела закупок 1-10», «Цена РФ / зарубеж / Avito 1-3» - 19 колонок).
# С 0.1.39 (решение пользователя 26.10.2026: «убери эти дубли и кубки») они
# не создаются и удаляются (COMPARE_OBSOLETE_COLUMNS) - их заменили 3
# колонки лучшей цены (COMPARE_BEST_COLUMNS).
COMPARE_PURCH_TEXT_COLUMNS = [f"Цена отдела закупок {i}" for i in range(1, PROCUREMENT_SLOTS + 1)]
COMPARE_MARKET_TEXT_COLUMNS = {key: [f"Цена {label} {i}" for i in (1, 2, 3)] for label, _, key in COMPARE_MARKET_ORDER}
COMPARE_OBSOLETE_COLUMNS = (["🏆 Лучшая цена", "🏆 Где лучшая цена"]
                            + COMPARE_BEST_PRICE_COLUMNS + COMPARE_BEST_SUPPLIER_COLUMNS
                            + COMPARE_PURCH_TEXT_COLUMNS
                            + [c for _, _, key in COMPARE_MARKET_ORDER for c in COMPARE_MARKET_TEXT_COLUMNS[key]])
# 0.1.39: лучшая цена позиции - по одной колонке-формуле на группу (решение
# пользователя 26.10.2026): самая низкая из 10 цен отдела закупок, самая
# низкая из 3 цен РФ, самая низкая из 3 цен зарубежом (как есть, без
# наценки). Формулы NocoDB пересчитываются сами при каждой новой цене.
# (название, ключ группы, имя колонки в БД)
COMPARE_BEST_COLUMNS = [("Лучшая цена от поставщика", "purch", "best_price_purch"),
                        ("Лучшая цена в РФ", "rf", "best_price_rf"),
                        ("Лучшая цена зарубежом", "abroad", "best_price_abroad")]
COMPARE_BEST_TITLES = [t for t, _, _ in COMPARE_BEST_COLUMNS]
ANALOG_OPTIONS = [
    {"title": "Да", "color": "#6bdc8b"},      # зелёный - аналог можно
    {"title": "Нет", "color": "#ff6b6b"},     # красный - аналог нельзя
    {"title": "Уточни", "color": "#ffd43b"},  # жёлтый - из документов неясно
]
SPEC_COLUMNS = [
    ("№", "SingleLineText", None),
    ("Наименование позиции", "LongText", None),
    # "Техническое задание" (добавлено 2026-10-01, версия 0.1.8, Шаг 5.1 в
    # CLAUDE.md) - требования ТЗ (технической части) по этой позиции
    # дословно, по одному пункту на строку ("<№ п/п> <характеристика> —
    # <требуемое значение>"). Заполняется ВСЕГДА, в обоих режимах анализа.
    # Многострочный текст, поэтому LongText.
    (TZ_COLUMN, "LongText", None),
    # "Ошибки в документации" (добавлено 2026-10-06, версия 0.1.13, Шаг 5.1e в
    # CLAUDE.md) - агентное поле: Клод анализирует ТЗ по каждой позиции и пишет
    # "❌ <ошибки>" / "✅ всё ок" / "⚠️ Не удалось проанализировать: <причина>"
    # (сомнительное - с пометкой "⚠️ Требует проверки"). Заполняется всегда.
    (DOC_ERRORS_COLUMN, "LongText", None),
    ("Марка/модель", "SingleLineText", None),
    # "Производитель" и "Страна происхождения" (добавлено 2026-09-30, версия
    # 0.1.5, Шаг 5.1 в CLAUDE.md). Страна - РОДИНА БРЕНДА (страна, где
    # компания основана/имеет штаб-квартиру), а не страна конкретного завода.
    ("Производитель", "SingleLineText", None),
    ("Страна происхождения", "SingleLineText", None),
    # "Аналог допускается" (добавлено 2026-09-30, Шаг 5.1 в CLAUDE.md) -
    # "Да" / "Нет — <причина>" / "Не указано в документах", заполняется
    # Клодом по документации тендера (без веб-поиска) в обоих режимах
    # анализа. Основание (документ, пункт) - в "Примечание". Свободный текст
    # (из-за причины после "Нет — "), поэтому SingleLineText.
    ("Аналог допускается", "SingleLineText", None),
    # "Аналог (статус)" (добавлено 2026-09-30, версия 0.1.3) - отдельное
    # цветное поле рядом с "Аналог допускается": SingleSelect "Да" (зелёный) /
    # "Нет" (красный) / "Уточни" (жёлтый), см. ANALOG_OPTIONS. Если в xlsx
    # его нет или значение не из списка - выводится из "Аналог допускается"
    # в normalize_analog_value(). NocoDB отклоняет ВСЮ пачку записей, если в
    # SingleSelect пришло значение не из списка (проверено 2026-09-30:
    # HTTP 400 "Invalid option(s)"), поэтому нормализация обязательна.
    (ANALOG_STATUS_COLUMN, "SingleSelect", {"colOptions": {"options": ANALOG_OPTIONS}}),
    # "Аналог" (добавлено 2026-10-01, версия 0.1.9, Шаг 5.1c в CLAUDE.md) -
    # результат подбора аналога со 100% соответствием ТЗ: карточка товара +
    # верификационная таблица + вердикт, либо "❌ ... не найден" с причиной.
    # Заполняется всегда. Многострочный текст, поэтому LongText.
    (ANALOG_TEXT_COLUMN, "LongText", None),
    ("Кол-во", "Decimal", QTY_DECIMAL_META),
    ("Ед. изм.", "SingleLineText", None),
    ("НМЦК за позицию", "Decimal", RU_DECIMAL_META),
    # «Рыночная цена (найдено в интернете)», «Рыночная цена, руб.» и
    # «Источник (ссылка)» убраны из стандартной структуры в 0.1.28 (решение
    # пользователя 16.10.2026): цены ищутся по рынкам - РФ / зарубеж / Avito,
    # по 3 со ссылками (Шаг 5.1g), одна общая цена их дублировала. Новые
    # таблицы этих колонок не получают; в старых (К910 и др.) они остаются и
    # при перезаливке из xlsx переносятся как есть (sanitize_spec_row передаёт
    # все колонки xlsx) - см. LEGACY_SPEC_COLUMNS.
    # "Размеры упаковки (найдено в интернете)" (добавлено 2026-09-14, Шаг 5.1b
    # в CLAUDE.md) - раньше эта колонка Позиции_и_цены.xlsx существовала
    # только локально и никогда не попадала в NocoDB (отсутствовала в этом
    # списке, поэтому create_spec_table() не создавал под неё колонку, а
    # sync_specification() молча слала лишнее поле, которое NocoDB
    # игнорировал). Текст произвольный (см. tools/calc_delivery.py про
    # ожидаемый формат "<L>x<W>x<H> см/мм, вес: ~<N> кг"), поэтому LongText,
    # как и "Рыночная цена (найдено в интернете)".
    ("Размеры упаковки (найдено в интернете)", "LongText", None),
    # "Стоимость доставки, руб. (СДЭК)" (добавлено 2026-09-14, Шаг 5.1b в
    # CLAUDE.md) - считается СКРИПТОМ tools/calc_delivery.py (реальный вызов
    # api.cdek.ru/v2/calculator/tarifflist по габаритам/весу из "Размеры
    # упаковки" и адресу поставки), а не веб-поиском/LLM, и записывается в
    # Позиции_и_цены.xlsx ДО отправки в NocoDB - sync_specification() просто
    # переносит уже посчитанное значение, как и остальные колонки xlsx.
    # Decimal/RU_DECIMAL_META по той же причине, что и другие денежные
    # колонки (см. комментарий у SPEC_COLUMNS выше).
    ("Стоимость доставки, руб. (СДЭК)", "Decimal", RU_DECIMAL_META),
    # "URL" (добавлено 2026-09-30, версия 0.1.4, Шаг 5.1 в CLAUDE.md) -
    # ссылка на страницу товара на сайте ПРОИЗВОДИТЕЛЯ (не продавца/
    # маркетплейса - те идут в "Источник (ссылка)"). Если страницу у
    # производителя найти не удалось - ячейка пустая (не плейсхолдер):
    # read_spec_xlsx() пропускает None, и в NocoDB поле остаётся пустым.
    ("URL", "URL", None),
    # Поля агента по ТЗ «TZ-NocoDB 1», таблица 1.1 (версия 0.1.15, Шаг 5.1f):
    # цена -> сразу её комментарий.
    (NMC_COST_COLUMN, "Decimal", RU_DECIMAL_META),
    ("Цена агента 1", "Decimal", RU_DECIMAL_META),
    ("Комментарий агента 1", "LongText", None),
    ("Цена агента 2", "Decimal", RU_DECIMAL_META),
    ("Комментарий агента 2", "LongText", None),
    ("Цена агента 3", "Decimal", RU_DECIMAL_META),
    ("Комментарий агента 3", "LongText", None),
    # Стоимость товара в РФ / на Avito / зарубежом (версия 0.1.16, Шаг 5.1g):
    # предложение N (Decimal, руб.) -> сразу ссылка N (URL).
] + [(c, "URL" if c in MARKET_OFFER_LINK_COLUMNS else "Decimal",
      {"column_name": MARKET_OFFER_DB_NAMES[c]} if c in MARKET_OFFER_LINK_COLUMNS
      else {**RU_DECIMAL_META, "column_name": MARKET_OFFER_DB_NAMES[c]}) for c in MARKET_OFFER_FIELDS] + [
    # Цены отдела закупок (версия 0.1.22): поставщик N -> цена N, затем комментарий.
] + [(c, "Decimal" if c in PROCUREMENT_PRICE_COLUMNS else ("LongText" if c == PROCUREMENT_COMMENT_COLUMN else "SingleLineText"),
      {**RU_DECIMAL_META, "column_name": PROCUREMENT_DB_NAMES[c]} if c in PROCUREMENT_PRICE_COLUMNS
      else {"column_name": PROCUREMENT_DB_NAMES[c]}) for c in PROCUREMENT_FIELDS] + [
    ("Примечание", "LongText", None),
]

# Колонки, убранные из стандартной структуры (0.1.28) - остаются только в
# старых таблицах спецификации.
LEGACY_SPEC_COLUMNS = ("Рыночная цена (найдено в интернете)", "Рыночная цена, руб.", "Источник (ссылка)")


def compare_view_fields(columns=None):
    """Поля представления «Сбор цен» (после «№ … Ед. изм.»).
    С 0.1.40 (решение пользователя 26.10.2026: «в Сборе цен - только колонки
    с лучшими ценами») - ТОЛЬКО 3 колонки лучшей цены (COMPARE_BEST_COLUMNS:
    от поставщика, в РФ, зарубежом). Все цены строки смотрят в основном
    представлении таблицы (и в «Закупках»).
    columns - колонки таблицы (dict/set названий). columns=None - набор новой
    таблицы. Если в таблице колонок лучшей цены ещё нет (старые таблицы без
    формул, К925-К929, К931) - чтобы представление не осталось пустым,
    показываются все имеющиеся цены числами (all_price_fields(), как в 0.1.38)
    до перевода таблицы командой --compare-view-only."""
    if columns is None:
        return list(COMPARE_BEST_TITLES)
    best = [c for c in COMPARE_BEST_TITLES if c in columns]
    return best if best else all_price_fields(columns)


def all_price_fields(columns=None):
    """Все цены спецификации по одному разу, по группам (набор «Сбора цен»
    0.1.38-0.1.39; с 0.1.40 - только запасной вариант для старых таблиц без
    колонок лучшей цены): «Стоимость НМЦ», «НМЦК за позицию»; отдел закупок -
    10 пар «поставщик N» + «цена N, руб.» и комментарий; РФ / зарубеж / Avito -
    по 3 цены со ссылками; «Цена агента 1-3» с комментариями; в старых
    таблицах - ещё «Рыночная цена, руб.» и «Источник (ссылка)»."""
    have = (lambda c: True) if columns is None else (lambda c: c in columns)

    out = [c for c in (NMC_COST_COLUMN, "НМЦК за позицию") if have(c)]
    for supplier, raw in PROCUREMENT_PAIRS:
        out += [c for c in (supplier, raw) if have(c)]
    out += [PROCUREMENT_COMMENT_COLUMN] if have(PROCUREMENT_COMMENT_COLUMN) else []
    for label, prefix, key in COMPARE_MARKET_ORDER:
        for i in (1, 2, 3):
            out += [c for c in (f"{prefix} - предложение {i}", f"{prefix} - ссылка {i}") if have(c)]
    for p, c in AGENT_PRICE_PAIRS:
        out += [x for x in (p, c) if have(x)]
    if columns is not None:
        out += [c for c in ("Рыночная цена, руб.", "Источник (ссылка)") if c in columns]
    return out


COMPARE_VIEW_FIELDS = compare_view_fields()

# Columns that must be a real number (or null) for NocoDB to accept them -
# see the comment above SPEC_COLUMNS for why this matters.
NUMERIC_SPEC_COLUMNS = ("Кол-во", "НМЦК за позицию", "Рыночная цена, руб.", "Стоимость доставки, руб. (СДЭК)",
                        NMC_COST_COLUMN) + AGENT_PRICE_COLUMNS + MARKET_OFFER_PRICE_COLUMNS + PROCUREMENT_PRICE_COLUMNS

# "НМЦК за позицию" is sometimes written as "77250.00 за ед. (154500.00 за
# 2 шт.)" (per-unit price, with the actual line total in parentheses) -
# confirmed on a real tender (32616325015, 2026-09-04). A plain first-number
# parse would silently grab the per-unit figure instead of the total the
# column name promises ("за позицию" = for the whole line). This pattern
# pulls out the parenthetical total when present.
POSITION_TOTAL_RE = re.compile(r"\(([\d\s.,]+)\s*за\s*\d+\s*(?:шт|ед)\.?\)", re.IGNORECASE)


def parse_position_total(raw):
    """Parse "НМЦК за позицию" specifically - prefers the "(Y за N шт.)"
    total over a leading per-unit figure when both are present in the text,
    otherwise behaves like parse_ru_number()."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    text = str(raw)
    match = POSITION_TOTAL_RE.search(text)
    if match:
        return parse_ru_number(match.group(1))
    return parse_ru_number(text)


def parse_ru_number(raw):
    """Best-effort parse of one spec-table numeric cell into a float.

    Accepts an already-numeric openpyxl cell (int/float) as well as RU
    -formatted number text ("1 500 000,55", "1500000.55", "12 шт." - a
    trailing non-numeric unit is ignored - same digit-token approach as
    normalize_number() in save_tender.py). Returns None when no numeric
    token can be found at all (e.g. "Не указано в документах", "Не удалось
    определить") - the caller must make sure that original text isn't just
    silently lost, see sanitize_spec_row().
    """
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    raw = str(raw).strip()
    if not raw:
        return None

    match = re.search(r"\d(?:[\d\s.,]*\d)?", raw)
    if not match:
        return None
    cleaned = re.sub(r"\s", "", match.group(0))

    if "," in cleaned and "." in cleaned:
        if cleaned.rfind(",") > cleaned.rfind("."):
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    elif "," in cleaned:
        parts = cleaned.split(",")
        if len(parts[-1]) == 2:
            cleaned = cleaned.replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    elif cleaned.count(".") > 1:
        cleaned = cleaned.replace(".", "")
    elif cleaned.count(".") == 1:
        integer_part, _, frac_part = cleaned.partition(".")
        if len(frac_part) == 3 and len(integer_part) <= 3:
            cleaned = cleaned.replace(".", "")

    try:
        return float(cleaned)
    except ValueError:
        return None


def normalize_analog_value(raw):
    """Map an "Аналог (статус)" / "Аналог допускается" value onto one of ANALOG_OPTIONS.

    Returns (option, reason): option is "Да"/"Нет"/"Уточни"; reason is the
    text after "Нет —"/"Нет -"/"Нет:" (old 0.1.2 format) so it isn't lost -
    the caller moves it to "Примечание". Anything that isn't clearly Да/Нет
    (empty, "Не указано в документах", unexpected text) becomes "Уточни" -
    never guess a green/red verdict the documents didn't give."""
    text = str(raw or "").strip()
    if not text:
        return "Уточни", ""
    low = text.lower()
    if low == "да" or low.startswith(("да ", "да,", "да —", "да -", "да:")):
        rest = re.sub(r"^да\s*[—\-:,]?\s*", "", text, flags=re.IGNORECASE).strip()
        return "Да", rest
    if low == "нет" or low.startswith(("нет ", "нет,", "нет —", "нет -", "нет:")):
        rest = re.sub(r"^нет\s*[—\-:,]?\s*", "", text, flags=re.IGNORECASE).strip()
        return "Нет", rest
    if low.startswith("уточн"):
        return "Уточни", ""
    if low.startswith("не указано"):
        return "Уточни", ""
    return "Уточни", text


def sanitize_spec_row(row: dict) -> dict:
    """Turn one Позиции_и_цены.xlsx row into a record NocoDB will accept.

    NUMERIC_SPEC_COLUMNS have to be real numbers or null - a placeholder
    string in any one of them would make NocoDB reject the entire bulk
    insert (confirmed empirically, see the SPEC_COLUMNS comment). When a
    value can't be parsed as a number, the field is sent as null and the
    original text is preserved by appending it to "Примечание" instead of
    being silently dropped.
    """
    out = dict(row)
    extra_notes = []
    status_raw = out.get(ANALOG_STATUS_COLUMN)
    if str(status_raw or "").strip() not in {o["title"] for o in ANALOG_OPTIONS}:
        status_raw = out.get(ANALOG_COLUMN)
    out[ANALOG_STATUS_COLUMN], _ = normalize_analog_value(status_raw)
    for col in NUMERIC_SPEC_COLUMNS:
        if col not in out:
            continue
        raw = out[col]
        parser = parse_position_total if col == "НМЦК за позицию" else parse_ru_number
        value = parser(raw)
        if value is None:
            if raw is not None and str(raw).strip():
                extra_notes.append(f"{col}: {raw}")
            out[col] = None
        else:
            out[col] = value
    # ТЗ «TZ-NocoDB 1», п.3: если цена заполнена - комментарий к ней
    # обязателен. NocoDB не умеет условную обязательность, поэтому отмечаем.
    for price_col, comment_col in AGENT_PRICE_PAIRS:
        if out.get(price_col) is not None and not str(out.get(comment_col) or "").strip():
            extra_notes.append(f"⚠ {price_col} без комментария (источник/ссылка не указаны)")
    for price_col, link_col in MARKET_OFFER_PAIRS:
        if out.get(price_col) is not None and not str(out.get(link_col) or "").strip():
            extra_notes.append(f"⚠ {price_col} без ссылки")
    for supplier_col, price_col in PROCUREMENT_PAIRS:
        if out.get(price_col) is not None and not str(out.get(supplier_col) or "").strip():
            extra_notes.append(f"⚠ {price_col} без поставщика")
    if extra_notes:
        existing = str(out.get("Примечание") or "").strip()
        addition = "; ".join(extra_notes)
        out["Примечание"] = f"{existing}; {addition}" if existing else addition
    return out

# Flat field name (matches save_tender.py / all_tenders.csv columns) -> NocoDB column title.
# "purchase_number" and "tender_url" map onto the two columns that already existed
# in the table before this integration; everything else was added for this integration.
# "internal_number" (added Шаг 1a в CLAUDE.md) maps onto "Внутренний номер" - unlike the
# other columns, that one is auto-created by ensure_column() below if it's missing,
# instead of requiring the user to add it by hand in the NocoDB UI first.
# NOTE: "Есть регистрация на площадке" и "Аккредитация" are intentionally NOT mapped -
# they are manual-only tracking checkboxes and are never written by extraction.
FIELD_MAP = {
    # "payment_days_summary" is a COMPUTED, NocoDB-only convenience field (not one
    # of the 7 extraction blocks, not written to local XML/CSV by save_tender.py).
    # The LLM fills it when building the JSON passed to this script - see
    # CLAUDE.md, Шаг 7.1a "Краткий срок оплаты и цветовая метка" for the exact
    # extraction/formatting rules (MSP-specific terms, worst-case day count,
    # "N дней" / "N-M дней" format, appended " МСП" label, leading colour
    # marker: "🟢 " when an advance payment (аванс) is provided (favourable
    # for the supplier, regardless of day count - takes priority), else
    # "🔴 " when postpayment-only and > 10 days, else no marker. The marker
    # is what a manually-configured NocoDB "is like 🟢%" / "is like 🔴%"
    # colouring rule keys off of; the API cannot set cell colour itself,
    # only write the marker text).
    "internal_number": "Внутренний номер",
    "source_file": "Файл-источник",
    "extracted_date": "Дата извлечения",
    "product_name": "Наименование предмета закупки",
    "quantity": "Количество",
    "status": "Статус",
    "delivery_place": "Место поставки",
    "delivery_term": "Срок поставки",
    "procedure_type": "Тип процедуры",
    "price_reduction": "Шаг снижения цены",
    "price_reduction_date": "Дата снижения цены",
    "start_price": "Начальная цена",
    "currency": "Валюта",
    "payment_terms": "Условия оплаты",
    "bid_security": "Обеспечение заявки",
    "contract_security": "Обеспечение контракта",
    "publication_date": "Дата публикации",
    "submission_deadline": "Срок подачи заявок",
    "auction_date": "Дата аукциона",
    "results_date": "Дата подведения итогов",
    "contract_date": "Дата заключения контракта",
    "last_modified": "Дата последнего изменения",
    "organizer": "Заказчик",
    "inn": "ИНН заказчика",
    "kpp": "КПП заказчика",
    "ogrn": "ОГРН заказчика",
    "contact": "Контактное лицо заказчика",
    "address": "Адрес заказчика",
    "procurement_method": "Способ закупки",
    "platform": "Площадка",
    "platform_url": "Ссылка на площадку",
    "purchase_number": "Номер конкурса",
    "tender_url": "Ссылка на процедуру",
    "stage": "Стадия",
    "sme": "СМП/СОНО",
    "national_regime_applied": "Нацрежим - применяется",
    "national_regime_measure": "Нацрежим - мера",
    "national_regime_items": "Нацрежим - товары",
    "national_regime_basis": "Нацрежим - основание",
    "payment_days_summary": "Срок оплаты (кратко)",
    # Шаг 7.1d (0.1.12): перечень обязательных документов в составе заявки -
    # NocoDB-only поле (как payment_days_summary), в XML/CSV не пишется.
    "application_documents": "Перечень документов заявки",
}

# Columns this script will auto-create on the main "Реестр конкурсов" table if
# they're missing, instead of requiring the user to add them by hand first.
# title -> (uidt, extra column props or None). Keep this list small/deliberate -
# most FIELD_MAP columns were added by hand in the NocoDB UI when the
# integration was first built; this is only for columns introduced later that
# should just work out of the box.
#
# "Номер конкурса" (PURCHASE_NUMBER_COLUMN below) is included here because a
# live check against the production table (2026-09-07, testing the new
# internal_number feature) found it missing entirely - find_record() against
# it failed with a hard HTTP 422 ("Column alias 'Номер конкурса' not found"),
# which meant upsert() couldn't look up ANY tender by purchase number and the
# whole NocoDB sync step was broken, not just for internal_number. Confirmed
# with the user (2026-09-07) that recreating this column is the fix, rather
# than matching on some other existing column. Note: "Внутренний номер" is
# also already present in the live table as its primary/title column (pv:
# True) and already held one hand-entered value ("К897", Cyrillic К, no
# dash) plus, on other rows, old purchase/folder numbers used as makeshift
# identifiers before this column existed for that purpose - confirmed with
# the user that new auto-assigned values should still use the "K-983" format
# (Latin K + dash) from Шаг 1a в CLAUDE.md rather than matching that legacy style.
AUTO_CREATE_COLUMNS = {
    "Внутренний номер": ("SingleLineText", None),
    "Номер конкурса": ("SingleLineText", None),
    "Перечень документов заявки": ("LongText", None),
}

# Column title -> title of the column it should sit immediately after in the
# default grid view, applied only right after ensure_columns() actually
# CREATES that column (a column the user later reorders by hand is never
# touched again). Without this, the "create column" API just appends new
# columns at the very end of the table, which is where "Номер конкурса"
# landed when it was auto-created (see AUTO_CREATE_COLUMNS above) - the user
# asked (2026-09-07) for it to sit between "Внутренний номер" and "Ссылка на
# процедуру" instead (its natural spot, next to the other tender-identifying
# columns), so this makes that positioning automatic/self-healing if the
# column is ever deleted and auto-recreated again, instead of requiring the
# same manual reorder through the API a second time.
AUTO_CREATE_COLUMN_POSITION_AFTER = {
    "Номер конкурса": "Внутренний номер",
}

PURCHASE_NUMBER_COLUMN = "Номер конкурса"
SOURCE_FILE_COLUMN = "Файл-источник"
NOT_SPECIFIED = "Не указано в документах"

# Fields where a missing value should read as a plain dash "—" rather than
# NOT_SPECIFIED - see the matching DASH_FIELDS in save_tender.py for the
# full rationale (user request, 2026-09-07: no link to the procurement
# documentation -> "—", not "Не указано в документах"). Claude already
# writes "—" directly when assembling the flat JSON (same file feeds both
# save_tender.py and this script), so this is a safety net here too, in
# case that JSON still has the old placeholder or an empty value.
DASH = "—"
DASH_FIELDS = ("tender_url",)


def apply_dash_placeholders(flat_data):
    """Normalizes DASH_FIELDS in-place: missing/empty/NOT_SPECIFIED -> "—"."""
    for flat_name in DASH_FIELDS:
        value = str(flat_data.get(flat_name) or "").strip()
        if not value or value == NOT_SPECIFIED:
            flat_data[flat_name] = DASH
    return flat_data


# Fields whose NocoDB target column is a "Ссылка на ..." URL field that (unlike
# the spec table's "Источник (ссылка)" column - see the big comment near
# SPEC_COLUMNS above) DOES enforce strict isURL validation at the API level -
# confirmed empirically 2026-09-09 on a real tender (ЗК-715-2024) whose
# Извещение had no direct link: sending tender_url = "—" (the DASH placeholder
# above, per Блок 6 в CLAUDE.md) into "Ссылка на процедуру" made the WHOLE
# create/update request fail with HTTP 400 {"msg":"Validation failed : isURL"}
# - these two were most likely hand-created in the NocoDB UI (unlike the
# API-created spec table columns), where "Validate as URL" can be ticked on.
# "platform_url" -> "Ссылка на площадку" maps the same way and would fail the
# same way if it is ever missing/placeholder text, so it gets the same guard
# even though only tender_url has hit this in practice so far.
STRICT_URL_FIELDS = ("tender_url", "platform_url")
_URL_RE = re.compile(r"^https?://", re.IGNORECASE)


def _looks_like_url(value) -> bool:
    return bool(_URL_RE.match(str(value or "").strip()))

def format_source_files(raw):
    """Mirrors save_tender.py's format_source_files(): the same " + "-joined
    string built for save_tender.py's --source-file (Шаг 7, п.3 в CLAUDE.md)
    -> one file per line ("a.docx\nb.pdf") - "Файл-источник" is a rich-text
    column here too (see save_tender.py's own docstring on this, 2026-09-07).
    Added 2026-09-08 after a user report that this column kept coming out
    empty in NocoDB: save_tender.py takes the file list via --source-file
    (a CLI flag, not a JSON key) and never needed "source_file" in the JSON
    at all, so past sessions had no reason to put it there - but this script
    only reads flat_data from --json (no equivalent CLI flag exists here),
    so the field must actually be present in that JSON for this column to
    get anything. CLAUDE.md Шаг 7 now says to include it. A single file (no
    " + " found) is returned unchanged."""
    if raw is None:
        return raw
    parts = [p.strip() for p in str(raw).split(" + ") if p.strip()]
    if len(parts) <= 1:
        return raw
    return "\n".join(parts)




# "Начальная цена" (main table, FIELD_MAP["start_price"]) is plain SingleLineText,
# not Decimal - it has to stay text because it also holds the NOT_SPECIFIED
# placeholder and occasional multi-number text ("3805861.62 руб. включая НДС
# (3119558.70 руб. без НДС)", tender 905-U310-26/3333, K-985) that a real
# Decimal column would reject outright. save_tender.py's normalize_number()
# already guarantees any actual price number inside that text is a clean
# "<digits>.<exactly two digits>" token (no thousands separator) before this
# script ever sees it - the only thing left to fix for the NocoDB copy of the
# value is the decimal point itself: the user asked (2026-09-07, same request
# that produced the Decimal "separator": "none_comma" meta above) for every
# price actually showing up in NocoDB to use a comma there instead, with no
# other dots/commas in the number. This only touches the copy of the string
# sent to NocoDB - save_tender.py's own XML/CSV output (and the on-disk JSON)
# are untouched, since nothing here mutates the source JSON file itself.
PRICE_NUMBER_RE = re.compile(r"(?<!\d)(\d+)\.(\d{2})(?!\d)")


def to_comma_decimal(text):
    """Replace every clean "<digits>.<exactly 2 digits>" token in `text` with
    "<digits>,<2 digits>" - i.e. swap the decimal point for a comma without
    touching anything else (placeholder text, ✅/🔺 markers, "руб. включая
    НДС" wording, a second number in parentheses, etc. all pass through
    unchanged). Safe to call on any string, including NOT_SPECIFIED/empty."""
    if not text:
        return text
    return PRICE_NUMBER_RE.sub(lambda m: f"{m.group(1)},{m.group(2)}", text)


# "Адрес заказчика" - user request (2026-09-08): the address itself should
# render in bold in NocoDB, but the postal index (6-digit ЯЯЯЯЯЯ, wherever it
# sits in the string - leading "123456, г. Москва..." and trailing "...,
# 123456" both occur in real tenders) must stay un-bolded. The column was
# plain LongText with no rich-text formatting until now - PATCHed via the
# Meta API (2026-09-08) to add meta.richMode=True/smartMode=False, matching
# "Наименование предмета закупки" (see product_name_formatting.md in project
# memory), so Markdown **bold** actually renders instead of showing as
# literal asterisks.
POSTAL_INDEX_RE = re.compile(r"(?<!\d)(\d{6})(?!\d)")


def bold_address_except_index(address):
    """Wraps `address` in Markdown bold, carving out the first standalone
    6-digit postal index (if any) so it stays plain text. Safe to call on
    any string, including NOT_SPECIFIED/DASH/empty (returned unchanged)."""
    if not address or address in (NOT_SPECIFIED, DASH):
        return address
    match = POSTAL_INDEX_RE.search(address)
    if not match:
        return f"**{address}**"
    before, index, after = address[:match.start()], match.group(1), address[match.end():]
    parts = []
    parts.append(f"**{before}**" if before.strip(" ,") else before)
    parts.append(index)
    parts.append(f"**{after}**" if after.strip(" ,") else after)
    return "".join(parts)


def _request(method, url, token, body=None, _retries=8):
    """HTTP 429 (ThrottlerException - лимит запросов NocoDB, ~5 в секунду на
    метаданные; впервые упёрлись 2026-10-07 при настройке представления
    «Цены агента», где на каждую колонку идёт отдельный PATCH) - повтор с
    нарастающей паузой. С 0.1.16 пауза длиннее (5, 10, ... до 60 с) и перед
    каждым запросом к метаданным - 0,3 с: при досоздании 18 колонок «Стоимость
    товара ...» во всех таблицах прежних 2-12 с не хватало (2026-10-09)."""
    import time
    data = json.dumps(body).encode("utf-8") if body is not None else None
    for attempt in range(_retries + 1):
        if url.startswith(META_URL):
            time.sleep(0.3)
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("xc-token", token)
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8")
                return resp.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", errors="replace")
            if e.code == 429 and attempt < _retries:
                time.sleep(min(60, 5 * (attempt + 1)))
                continue
            return e.code, {"error": raw}


def find_record(token, column, value):
    where = urllib.parse.quote(f"({column},eq,{value})")
    url = f"{RECORDS_URL}?where={where}&limit=1"
    status, body = _request("GET", url, token)
    if status != 200:
        raise RuntimeError(f"lookup failed: HTTP {status} {body}")
    rows = body.get("list", [])
    return rows[0] if rows else None


def get_table_columns(token, table_id):
    """Returns {column_title: column_meta_dict} for an existing table."""
    status, body = _request("GET", f"{META_URL}/tables/{table_id}", token)
    if status != 200:
        raise RuntimeError(f"get table failed: HTTP {status} {body}")
    return {c["title"]: c for c in body.get("columns", [])}


def position_column_after(token, table_id, column_id, anchor_title):
    """Moves column_id to sit immediately after the column titled
    anchor_title in the table's default grid view (see
    AUTO_CREATE_COLUMN_POSITION_AFTER). Best-effort: any failure here is
    swallowed (the column still exists and works, just wherever the create
    -column API put it) rather than failing the whole sync over cosmetics.

    Mechanics: a grid view's left-to-right column order is controlled by
    each column's OWN per-view record (GET/PATCH
    /meta/views/{viewId}/columns/{viewColumnId} - viewColumnId, not the
    column's own id, confirmed empirically 2026-09-07: PATCHing by the
    column id itself 404s with ERR_FIELD_NOT_FOUND), not by the base
    column's table-level `order` field. Orders are plain floats and don't
    need to be contiguous, so the new column is just given the midpoint
    between the anchor's order and whatever comes right after it (or
    anchor_order + 1 if the anchor is currently last).
    """
    try:
        view_id = get_default_view_id(token, table_id)
        if not view_id:
            return
        status, body = _request("GET", f"{META_URL}/views/{view_id}/columns", token)
        if status != 200:
            return
        view_columns = body.get("list", [])

        id_to_title = get_table_columns(token, table_id)  # title -> column meta
        title_to_id = {meta["id"]: title for title, meta in id_to_title.items()}

        by_column_id = {vc["fk_column_id"]: vc for vc in view_columns}
        target_vc = by_column_id.get(column_id)
        anchor_id = id_to_title.get(anchor_title, {}).get("id")
        anchor_vc = by_column_id.get(anchor_id) if anchor_id else None
        if not target_vc or not anchor_vc:
            return

        anchor_order = anchor_vc["order"]
        later_orders = [vc["order"] for vc in view_columns if vc["order"] > anchor_order]
        new_order = (anchor_order + min(later_orders)) / 2 if later_orders else anchor_order + 1

        _request("PATCH", f"{META_URL}/views/{view_id}/columns/{target_vc['id']}", token, {"order": new_order})
    except Exception:
        pass


def ensure_columns(token, table_id, titles):
    """Auto-creates any of AUTO_CREATE_COLUMNS listed in `titles` that don't
    already exist on the table - e.g. "Внутренний номер" the first time this
    script runs after Шаг 1a was added, so the user doesn't have to go add it
    by hand in the NocoDB UI first. Safe to call every run (no-op once the
    column exists). Newly-created columns listed in
    AUTO_CREATE_COLUMN_POSITION_AFTER are also moved into place (see
    position_column_after) instead of being left at the end of the table."""
    wanted = [t for t in titles if t in AUTO_CREATE_COLUMNS]
    if not wanted:
        return
    existing = get_table_columns(token, table_id)
    for title in wanted:
        if title in existing:
            continue
        uidt, extra = AUTO_CREATE_COLUMNS[title]
        col = {"title": title, "uidt": uidt}
        if extra:
            col.update(extra)
        status, body = _request("POST", f"{META_URL}/tables/{table_id}/columns", token, col)
        if status != 200:
            raise RuntimeError(f"create column '{title}' failed: HTTP {status} {body}")
        anchor_title = AUTO_CREATE_COLUMN_POSITION_AFTER.get(title)
        if anchor_title:
            # The create-column response is the TABLE's meta, not the new
            # column's (confirmed empirically 2026-09-07 on a scratch column -
            # its "id" is fk_model_id, i.e. the table id) - re-fetch columns to
            # find the new column's own id by title.
            new_column_id = get_table_columns(token, table_id).get(title, {}).get("id")
            if new_column_id:
                position_column_after(token, table_id, new_column_id, anchor_title)


def upsert(token, flat_data: dict, spec_xlsx_path: str = None, fallback_id: str = None, fx_rates: dict = None):
    """fallback_id: used to name/find the spec table when the tender has no real
    purchase_number (placeholder "Не указано в документах") - e.g. the tender's
    local folder name/id, so the spec table can still be created and found later
    even without an official notice number. Not required if the tender does have
    a real purchase_number.

    fx_rates: passed straight through to enrich_market_price_rub() - see
    convert_market_price_to_rub() for the expected shape ({"€": ..., "$": ...})."""
    purchase_number = flat_data.get("purchase_number")
    if not purchase_number:
        raise ValueError("flat_data must include 'purchase_number' for upsert matching")

    apply_dash_placeholders(flat_data)
    if flat_data.get("start_price"):
        flat_data["start_price"] = to_comma_decimal(str(flat_data["start_price"]))
    if flat_data.get("address"):
        flat_data["address"] = bold_address_except_index(str(flat_data["address"]))
    if flat_data.get("source_file"):
        flat_data["source_file"] = format_source_files(str(flat_data["source_file"]))

    ensure_columns(token, TABLE_ID, [FIELD_MAP.get(k) for k in flat_data.keys()])

    row = {}
    for flat_name, value in flat_data.items():
        col = FIELD_MAP.get(flat_name)
        if col is None:
            continue  # unknown field, skip rather than fail
        if flat_name in STRICT_URL_FIELDS and not _looks_like_url(value):
            # Don't send "—"/NOT_SPECIFIED/anything else into a strictly-
            # validating URL column - see STRICT_URL_FIELDS comment above.
            # Omitting the key just leaves that column blank in NocoDB,
            # instead of failing the entire record create/update.
            continue
        row[col] = value

    # When the purchase number itself wasn't found in the documents, matching on it
    # would collide across unrelated tenders that share the same placeholder text -
    # fall back to matching on the source file name instead in that case.
    if purchase_number == NOT_SPECIFIED:
        source_file = flat_data.get("source_file")
        existing = find_record(token, SOURCE_FILE_COLUMN, source_file) if source_file else None
    else:
        existing = find_record(token, PURCHASE_NUMBER_COLUMN, purchase_number)

    spec_table_url = None
    spec_table_id = None
    spec_safe_rows = None
    if spec_xlsx_path:
        # 0.1.14 (user request 2026-10-06): the spec table is named EXACTLY as
        # the tender's internal number («Внутренний номер», e.g. "К929").
        # Fallbacks only when there is no internal number: the real purchase
        # number, then fallback_id (tender folder name).
        internal = str(flat_data.get("internal_number") or "").strip()
        if internal and internal != NOT_SPECIFIED:
            spec_id = internal
        elif purchase_number != NOT_SPECIFIED:
            spec_id = purchase_number
        else:
            spec_id = fallback_id
        if spec_id:
            spec_rows = read_spec_xlsx(spec_xlsx_path)
            spec_rows = enrich_market_price_rub(spec_rows, fx_rates)
            existing_table_id = _table_id_from_link((existing or {}).get(SPEC_LINK_COLUMN))
            spec_table_url, spec_table_id, spec_safe_rows = sync_specification(
                token, spec_id, spec_rows, existing_table_id=existing_table_id)
            row[SPEC_LINK_COLUMN] = spec_table_url

    if existing:
        record_id = existing["Id"]
        status, body = _request("PATCH", RECORDS_URL, token, {"Id": record_id, **row})
        if status != 200:
            raise RuntimeError(f"update failed: HTTP {status} {body}")
        result = {"action": "updated", "record_id": record_id, "purchase_number": purchase_number}
    else:
        status, body = _request("POST", RECORDS_URL, token, row)
        if status != 200:
            raise RuntimeError(f"create failed: HTTP {status} {body}")
        result = {"action": "created", "record_id": body.get("Id"), "purchase_number": purchase_number}

    # Post-write verification (added 2026-09-09, user request - this project's
    # own memory already logged two silent-failure incidents where the script
    # printed success but a column ("Файл-источник", then "Номер конкурса")
    # actually stayed empty in NocoDB): re-read what was actually written and
    # compare it against what this run intended to send, instead of trusting
    # the create/update HTTP 200 alone. Best-effort - a failure of the
    # verification GET itself is reported as its own issue rather than raised,
    # since the record write already succeeded by this point.
    result["verification"] = verify_main_record(token, result["record_id"], row)

    if spec_table_url:
        result["spec_table_url"] = spec_table_url
        result["spec_verification"] = verify_spec_table(token, spec_table_id, spec_safe_rows or [])
    return result


def sync_spec_only(token, spec_id: str, spec_xlsx_path: str, fx_rates: dict = None):
    """Re-fill ONLY the "Спецификация — <spec_id>" table from
    Позиции_и_цены.xlsx (+ verification), without touching the main record in
    "Реестр конкурсов" (its "Ссылка на спецификацию" stays valid - the table
    is cleared and refilled in place, not recreated). Added 2026-10-01
    (0.1.8) for re-filling an already saved tender after a spec-column
    change, when the tender's flat JSON from Шаг 7 is no longer at hand."""
    # 0.1.14: spec_id may be «Номер конкурса» or «Внутренний номер»; if the
    # main record is found, the table is named by its internal number and the
    # already linked table is reused (renamed if it still has an old title).
    rec = find_record_any(token, spec_id)
    existing_table_id = None
    title_id = spec_id
    if rec:
        internal = str(rec.get("Внутренний номер") or "").strip()
        if internal:
            title_id = internal
        existing_table_id = _table_id_from_link(rec.get(SPEC_LINK_COLUMN))
    if existing_table_id is None:
        legacy = find_table_by_title(token, _legacy_spec_table_title(spec_id))
        if legacy:
            existing_table_id = legacy["id"]
    spec_rows = read_spec_xlsx(spec_xlsx_path)
    spec_rows = enrich_market_price_rub(spec_rows, fx_rates)
    url, table_id, safe_rows = sync_specification(token, title_id, spec_rows,
                                                  existing_table_id=existing_table_id)
    if rec and rec.get(SPEC_LINK_COLUMN) != url:
        _request("PATCH", RECORDS_URL, token, {"Id": rec["Id"], SPEC_LINK_COLUMN: url})
    return {"action": "spec_only", "spec_id": title_id, "spec_table_url": url,
            "spec_verification": verify_spec_table(token, table_id, safe_rows or [])}


def _safe_title(value: str) -> str:
    # "/" (and a few other characters) are rejected by NocoDB in table titles.
    # NocoDB rejects "." in table titles (HTTP 400), e.g. "№209/01.10.26/АС/КО/Э" - fixed 2026-10-03
    return str(value).strip().replace("/", "-").replace(".", "-")


def _spec_table_title(spec_id: str) -> str:
    """0.1.14 (user request 2026-10-06): the spec table title is EXACTLY the
    tender's internal number («Внутренний номер», e.g. "К929") - no prefix."""
    return _safe_title(spec_id)


def _legacy_spec_table_title(spec_id: str) -> str:
    """Title format used before 0.1.14 - only for finding old tables."""
    return f"Спецификация — {_safe_title(spec_id)}"


def _table_id_from_link(url):
    """Extracts the table id from a «Ссылка на спецификацию» URL
    (https://app.nocodb.com/<ws>/<base>/<table_id>[/<view_id>])."""
    if not url:
        return None
    m = re.search(re.escape(BASE_ID) + r"/(m[a-z0-9]+)", str(url))
    return m.group(1) if m else None


def rename_table(token, table_id, title):
    """Renames a table (display title only). NocoDB's PATCH requires the
    physical `table_name` in the body too, so it is re-sent unchanged."""
    status, meta = _request("GET", f"{META_URL}/tables/{table_id}", token)
    if status != 200:
        raise RuntimeError(f"get table failed: HTTP {status} {meta}")
    if meta.get("title") == title:
        return meta
    status, body = _request("PATCH", f"{META_URL}/tables/{table_id}", token,
                            {"title": title, "table_name": meta["table_name"]})
    if status != 200:
        raise RuntimeError(f"rename table to '{title}' failed: HTTP {status} {body}")
    meta["title"] = title
    return meta


def find_table_by_title(token, title):
    status, body = _request("GET", f"{META_URL}/bases/{BASE_ID}/tables", token)
    if status != 200:
        raise RuntimeError(f"list tables failed: HTTP {status} {body}")
    for t in body.get("list", []):
        if t.get("title") == title:
            return t
    return None


def create_spec_table(token, title):
    columns = []
    for name, uidt, extra in SPEC_COLUMNS:
        col = {"title": name, "uidt": uidt}
        if extra:
            col.update(extra)
        columns.append(col)
    status, body = _request("POST", f"{META_URL}/bases/{BASE_ID}/tables", token,
                             {"title": title, "columns": columns})
    if status != 200:
        raise RuntimeError(f"create spec table failed: HTTP {status} {body}")
    return body


def get_default_view_id(token, table_id):
    status, body = _request("GET", f"{META_URL}/tables/{table_id}/views", token)
    if status != 200:
        raise RuntimeError(f"list views failed: HTTP {status} {body}")
    views = body.get("list", [])
    return views[0]["id"] if views else None


def list_all_records(token, table_id):
    """Все строки таблицы, постранично. NocoDB отдаёт не больше 100 строк за
    запрос, даже при limit=1000 (pageInfo.pageSize = 100; проверено
    2026-10-16 на К910) - раньше clear_table_records(), read_manual_fields()
    и verify_spec_table() на спецификациях >100 позиций видели только первые
    100 строк (0.1.25)."""
    rows, offset = [], 0
    while True:
        status, body = _request("GET", f"{BASE_URL}/{table_id}/records?limit=100&offset={offset}", token)
        if status != 200:
            raise RuntimeError(f"list records failed: HTTP {status} {body}")
        batch = body.get("list", [])
        rows.extend(batch)
        if not batch or body.get("pageInfo", {}).get("isLastPage", True):
            return rows
        offset += len(batch)


def clear_table_records(token, table_id):
    ids = [{"Id": r["Id"]} for r in list_all_records(token, table_id)]
    for i in range(0, len(ids), 100):
        status, body = _request("DELETE", f"{BASE_URL}/{table_id}/records", token, ids[i:i + 100])
        if status != 200:
            raise RuntimeError(f"clear records failed: HTTP {status} {body}")


def ensure_spec_columns(token, table_id, only=None):
    """Adds any SPEC_COLUMNS missing from an ALREADY EXISTING "Спецификация
    — ..." table (created before that column was introduced - e.g.
    "Аналог допускается", added 2026-09-30), so re-syncing an older tender
    doesn't silently drop the new field (NocoDB ignores unknown keys on
    insert - the same failure mode noted for "Размеры упаковки" above).
    Each newly created column is moved right after the SPEC_COLUMNS entry
    that precedes it (best-effort, see position_column_after). No-op once
    every column exists.

    only (0.1.25): если задан - досоздаются только колонки из этого списка
    (например PROCUREMENT_FIELDS для --procurement-columns-only), остальные
    недостающие колонки SPEC_COLUMNS не трогаются."""
    existing = get_table_columns(token, table_id)
    analog = existing.get(ANALOG_COLUMN)
    if only is None and analog and analog.get("uidt") != "SingleLineText":
        # Early 0.1.3 briefly turned "Аналог допускается" itself into a
        # SingleSelect (by mistake - the user wanted a SEPARATE status field).
        # Turn it back into plain text; the caller refills rows right after.
        status, body = _request("PATCH", f"{META_URL}/columns/{analog['id']}", token, {
            "title": ANALOG_COLUMN,
            "column_name": analog.get("column_name", ANALOG_COLUMN),
            "uidt": "SingleLineText",
        })
        if status != 200:
            raise RuntimeError(f"convert '{ANALOG_COLUMN}' back to text failed: HTTP {status} {body}")
    prev_title = None
    for name, uidt, extra in SPEC_COLUMNS:
        if name not in existing and (only is None or name in only):
            col = {"title": name, "uidt": uidt}
            if extra:
                col.update(extra)
            status, body = _request("POST", f"{META_URL}/tables/{table_id}/columns", token, col)
            if status != 200:
                raise RuntimeError(f"create spec column '{name}' failed: HTTP {status} {body}")
            if prev_title:
                new_id = get_table_columns(token, table_id).get(name, {}).get("id")
                if new_id:
                    position_column_after(token, table_id, new_id, prev_title)
        prev_title = name


def ensure_price_view(token, table_id, view_title=PRICE_VIEW_TITLE, fields=None):
    """ТЗ «TZ-NocoDB 1» (версия 0.1.15): грид-представление PRICE_VIEW_TITLE
    в таблице спецификации. Показываются только PRICE_VIEW_LEAD_COLUMNS, затем
    AGENT_FIELDS строго в порядке таблицы 1.1 ТЗ (цена -> сразу её
    комментарий); остальные колонки скрыты. С 0.1.16 та же функция строит
    и другие представления спецификации («Закупки», «Сбор цен») - view_title/fields. Создаётся, если его нет; при
    каждом запуске видимость/порядок выставляются заново (идемпотентно).
    Колонки представления адресуются по id записи view-column (не по id
    самой колонки - см. position_column_after()).

    Returns {"ok": bool, "view_id": ..., "issues": [...]} - проверка:
    перечитывает колонки представления и сравнивает видимые с ожидаемыми."""
    wanted = PRICE_VIEW_LEAD_COLUMNS + (AGENT_FIELDS if fields is None else list(fields))
    try:
        status, body = _request("GET", f"{META_URL}/tables/{table_id}/views", token)
        if status != 200:
            raise RuntimeError(f"list views failed: HTTP {status} {body}")
        view = next((v for v in body.get("list", []) if v.get("title") == view_title), None)
        if view is None:
            status, view = _request("POST", f"{META_URL}/tables/{table_id}/grids", token, {"title": view_title})
            if status != 200:
                raise RuntimeError(f"create view failed: HTTP {status} {view}")
        view_id = view["id"]

        columns = get_table_columns(token, table_id)
        id_to_title = {meta["id"]: title for title, meta in columns.items()}
        status, body = _request("GET", f"{META_URL}/views/{view_id}/columns", token)
        if status != 200:
            raise RuntimeError(f"list view columns failed: HTTP {status} {body}")
        for vc in body.get("list", []):
            title = id_to_title.get(vc["fk_column_id"])
            if title in wanted:
                patch = {"show": True, "order": wanted.index(title) + 1}
            else:
                patch = {"show": False, "order": len(wanted) + 1 + (vc.get("order") or 0)}
            if bool(vc.get("show")) != patch["show"] or vc.get("order") != patch["order"]:
                st, b = _request("PATCH", f"{META_URL}/views/{view_id}/columns/{vc['id']}", token, patch)
                if st != 200:
                    raise RuntimeError(f"update view column '{title}' failed: HTTP {st} {b}")

        status, body = _request("GET", f"{META_URL}/views/{view_id}/columns", token)
        shown = [id_to_title.get(vc["fk_column_id"]) for vc in
                 sorted(body.get("list", []), key=lambda vc: vc.get("order") or 0) if vc.get("show")]
        issues = [] if shown == wanted else [f"видимые поля: {shown}, ожидалось: {wanted}"]
        return {"ok": not issues, "view_id": view_id, "issues": issues}
    except Exception as e:
        return {"ok": False, "view_id": None, "issues": [str(e)]}


def delete_views_by_title(token, table_id, title):
    """Удаляет представления таблицы с заголовком title (например, лишнее
    «Стоимость товара», если «Сбор цен» в таблице уже есть). Возвращает число
    удалённых."""
    status, body = _request("GET", f"{META_URL}/tables/{table_id}/views", token)
    if status != 200:
        raise RuntimeError(f"list views failed: HTTP {status} {body}")
    n = 0
    for v in body.get("list", []):
        if v.get("title") == title:
            st, b = _request("DELETE", f"{META_URL}/views/{v['id']}", token)
            if st != 200:
                raise RuntimeError(f"delete view '{title}' failed: HTTP {st} {b}")
            n += 1
    return n


def migrate_compare_view_title(token, table_id):
    """0.1.37 (решение пользователя 21.10.2026): представление цен называется
    «Сбор цен» (COMPARE_VIEW_TITLE). Если его в таблице ещё нет, первое
    найденное из прежних названий (COMPARE_VIEW_OLD_TITLES: «Сравнение цен»,
    затем «Стоимость товара») ПЕРЕИМЕНОВЫВАЕТСЯ в него - не удаляется и не
    создаётся заново (набор полей при этом не меняется; полный набор «Сбор
    цен» ставит ensure_compare_table()). Если «Сбор цен» уже есть, лишнее
    «Стоимость товара» удаляется (как до 0.1.37), «Сравнение цен» не
    трогается. Возвращает {"renamed": прежнее название или None,
    "deleted": число удалённых}."""
    status, body = _request("GET", f"{META_URL}/tables/{table_id}/views", token)
    if status != 200:
        raise RuntimeError(f"list views failed: HTTP {status} {body}")
    views = body.get("list", [])
    renamed = None
    if not any(v.get("title") == COMPARE_VIEW_TITLE for v in views):
        for old in COMPARE_VIEW_OLD_TITLES:
            view = next((v for v in views if v.get("title") == old), None)
            if view is not None:
                st, b = _request("PATCH", f"{META_URL}/views/{view['id']}", token, {"title": COMPARE_VIEW_TITLE})
                if st != 200:
                    raise RuntimeError(f"rename view '{old}' -> '{COMPARE_VIEW_TITLE}' failed: HTTP {st} {b}")
                renamed = old
                break
    deleted = delete_views_by_title(token, table_id, MARKET_VIEW_TITLE)
    return {"renamed": renamed, "deleted": deleted}


def has_view(token, table_id, title):
    status, body = _request("GET", f"{META_URL}/tables/{table_id}/views", token)
    return status == 200 and any(v.get("title") == title for v in body.get("list", []))


def refresh_script_views(token, table_id, titles=None):
    """0.1.37: NocoDB показывает каждую НОВУЮ колонку во всех представлениях
    таблицы. Поэтому после создания колонок (формулы «Сбор цен», колонки
    отдела закупок 4-10 и т.п.) уже настроенные «Цены агента» / «Закупки» /
    «Сбор цен» получали лишние поля (найдено 21.10.2026 в К926, К931, К939).
    Заново выставляет видимость и порядок тех из них, что в таблице уже есть
    (новые не создаёт). titles - какие проверять (по умолчанию все три).
    «Сбор цен» строится по колонкам, которые в таблице есть
    (compare_view_fields(columns), 0.1.38): в старых таблицах без формул -
    все имеющиеся цены числами, новые колонки не создаются.
    Возвращает {title: result ensure_price_view()}."""
    status, body = _request("GET", f"{META_URL}/tables/{table_id}/views", token)
    if status != 200:
        return {}
    have = {v.get("title") for v in body.get("list", [])}
    columns = get_table_columns(token, table_id)
    specs = {PRICE_VIEW_TITLE: AGENT_FIELDS, PURCHASE_VIEW_TITLE: PURCHASE_VIEW_FIELDS,
             COMPARE_VIEW_TITLE: compare_view_fields(columns)}
    results = {}
    for title in (titles or list(specs)):
        if title in have:
            results[title] = ensure_price_view(token, table_id, title, specs[title])
            if not results[title].get("ok"):
                print(f"WARNING: представление «{title}»: {results[title].get('issues')}", file=sys.stderr)
    return results


def ensure_spec_views(token, table_id):
    """Представления таблицы спецификации: «Цены агента» (0.1.15) и
    «Закупки» (0.1.16; с 0.1.22 - с ценами отдела закупок); прежние
    «Сравнение цен» / «Стоимость товара» переименовываются в «Сбор цен»
    (0.1.37, migrate_compare_view_title()). Ошибки - WARNING в stderr,
    синхронизацию не прерывают. Возвращает {title: result}."""
    results = {}
    try:
        results[f"{COMPARE_VIEW_TITLE} (название)"] = {"ok": True, **migrate_compare_view_title(token, table_id)}
    except Exception as e:
        results[f"{COMPARE_VIEW_TITLE} (название)"] = {"ok": False, "issues": [str(e)]}
    results[PRICE_VIEW_TITLE] = ensure_price_view(token, table_id)
    results[PURCHASE_VIEW_TITLE] = ensure_price_view(token, table_id, PURCHASE_VIEW_TITLE, PURCHASE_VIEW_FIELDS)
    for title, res in results.items():
        if not res.get("ok"):
            print(f"WARNING: представление «{title}»: {res.get('issues')}", file=sys.stderr)
    return results


def read_manual_fields(token, table_id):
    """Перед очисткой таблицы спецификации (повторная синхронизация) читает
    поля, которые заполняются вручную/чат-ботом, а не агентом
    (PROCUREMENT_FIELDS), чтобы они не потерялись. Ключ - «№» позиции.
    Возвращает {№: {поле: значение}} только для строк, где что-то заполнено."""
    saved = {}
    for r in list_all_records(token, table_id):
        values = {c: r.get(c) for c in PROCUREMENT_FIELDS if r.get(c) not in (None, "")}
        key = str(r.get("№") or "").strip()
        if values and key:
            saved[key] = values
    return saved


def restore_manual_fields(rows, manual):
    """Возвращает ручные поля (см. read_manual_fields) в строки xlsx по «№»,
    если в самом xlsx они пустые (значение из xlsx имеет приоритет)."""
    if not manual:
        return rows
    out = []
    for r in rows:
        r = dict(r)
        for col, val in manual.get(str(r.get("№") or "").strip(), {}).items():
            if r.get(col) in (None, ""):
                r[col] = val
        out.append(r)
    return out


def find_spec_table_id(token, spec_id):
    """id таблицы спецификации по «Внутренний номер» / «Номер конкурса» /
    заголовку таблицы (ссылка из «Ссылка на спецификацию», иначе по названию)."""
    rec = find_record_any(token, spec_id)
    table_id = _table_id_from_link(rec.get(SPEC_LINK_COLUMN)) if rec else None
    if table_id is None:
        for title in (_spec_table_title(spec_id), _legacy_spec_table_title(spec_id)):
            t = find_table_by_title(token, title)
            if t:
                table_id = t["id"]
                break
    if table_id is None:
        raise RuntimeError(f"таблица спецификации не найдена: {spec_id}")
    return table_id


def ensure_procurement_columns(token, spec_id):
    """--procurement-columns-only (0.1.25): в уже сохранённой спецификации
    досоздать ТОЛЬКО недостающие колонки отдела закупок (PROCUREMENT_FIELDS:
    поставщик/цена 1-PROCUREMENT_SLOTS и комментарий) - каждая встаёт в
    основном представлении сразу после предыдущей (поставщик 4 - после
    «цена 3, руб.», ... комментарий - последним). Другие недостающие колонки
    и строки не трогаются; представления не создаются - уже имеющиеся
    «Закупки» / «Цены агента» / «Сбор цен» обновляются (refresh_script_views,
    0.1.37), чтобы новые колонки встали куда нужно и не появились лишние.
    Проверка: все колонки есть, и в основном представлении они идут подряд
    в порядке PROCUREMENT_FIELDS."""
    table_id = find_spec_table_id(token, spec_id)
    before = set(get_table_columns(token, table_id))
    ensure_spec_columns(token, table_id, only=PROCUREMENT_FIELDS)
    columns = get_table_columns(token, table_id)
    created = [c for c in PROCUREMENT_FIELDS if c in columns and c not in before]
    missing = [c for c in PROCUREMENT_FIELDS if c not in columns]
    issues = [f"нет колонки: {c}" for c in missing]

    order_ok = None
    view_id = get_default_view_id(token, table_id)
    if view_id and not missing:
        id_to_title = {meta["id"]: title for title, meta in columns.items()}
        status, body = _request("GET", f"{META_URL}/views/{view_id}/columns", token)
        if status == 200:
            ordered = [id_to_title.get(vc["fk_column_id"]) for vc in
                       sorted(body.get("list", []), key=lambda vc: vc.get("order") or 0)]
            got = [t for t in ordered if t in PROCUREMENT_FIELDS]
            start = ordered.index(PROCUREMENT_FIELDS[0])
            order_ok = got == PROCUREMENT_FIELDS and \
                ordered[start:start + len(PROCUREMENT_FIELDS)] == PROCUREMENT_FIELDS
            if not order_ok:
                issues.append(f"порядок колонок в основном представлении: {got}")

    views = refresh_script_views(token, table_id)
    return {"action": "procurement_columns_only", "table_id": table_id, "slots": PROCUREMENT_SLOTS,
            "created_columns": created, "missing_columns": missing, "order_ok": order_ok,
            "views": views, "issues": issues, "ok": not issues}


def _formula_ref(title):
    return "{" + title + "}"


def compare_formula_columns():
    """[(title, formula_raw, meta, column_name)] формульных колонок
    представления «Сбор цен». С 0.1.39 (решение пользователя 26.10.2026) -
    только 3 колонки лучшей цены (COMPARE_BEST_COLUMNS), без 🏆 и без
    текстовых копий каждой цены:
    - «Лучшая цена от поставщика» - самая низкая из 10 «Отдел закупок: цена N,
      руб.» (пусто - поставщики цен ещё не давали);
    - «Лучшая цена в РФ» / «Лучшая цена зарубежом» - самая низкая из 3
      «… - предложение N» этого рынка, зарубеж - как есть, без наценки;
      если цены нет, а по рынку искали (пометка Шага 5.1g «РФ: найдено N из
      3 [...]» / «РФ: не найдено [...]» в «Примечании») - «не найдено»
      (0.1.28, просьба пользователя: иначе непонятно, искали или нет), не
      искали - пусто.
    Значение - текстом «38 164,00» (как раньше в «Сборе цен»). Пересчитываются
    сами при каждой новой цене."""
    def rub_text(x):
        # «38 164,00»: 2 знака, пробел между разрядами, запятая (проверено на
        # тестовой таблице 16.10.2026: REGEX_REPLACE с \1 и (?=...) работает)
        return (f'REPLACE(REGEX_REPLACE(CONCAT(ROUND({x}, 2), ""), "(\\d)(?=(\\d{{3}})+(\\.|$))", "\\1 "), '
                f'".", ",")')

    groups = {"purch": [_formula_ref(p) for _, p in PROCUREMENT_PAIRS]}
    for _, prefix, key in COMPARE_MARKET_ORDER:
        groups[key] = [_formula_ref(f"{prefix} - предложение {i}") for i in (1, 2, 3)]

    cols = []
    for title, key, column_name in COMPARE_BEST_COLUMNS:
        best = "MIN(" + ", ".join(groups[key]) + ")"
        if key == "purch":
            empty = '""'
        else:
            # REGEX_MATCH даёт 1/0 - сравнивать через «== 1» (проверено 16.10.2026)
            empty = (f'IF(REGEX_MATCH({{Примечание}}, "{COMPARE_NOTE_LABELS[key]}: (найдено|не найдено)") == 1, '
                     f'"не найдено", "")')
        cols.append((title, f"IF({best} > 0, {rub_text(best)}, {empty})", None, column_name))
    return cols


def ensure_compare_view(token, spec_id):
    """--compare-view-only (0.1.26): в уже сохранённой спецификации -
    1) досоздать недостающие колонки отдела закупок и цен по рынкам
       (PROCUREMENT_FIELDS + MARKET_OFFER_FIELDS; другие колонки и строки не
       трогаются), 2) создать/обновить формульные колонки лучших цен
       (compare_formula_columns; с 0.1.39 - 3 колонки «Лучшая цена …», а
       устаревшие формулы, в т.ч. 19 текстовых копий цен с 🏆, удаляются -
       COMPARE_OBSOLETE_COLUMNS), 3) создать/обновить представление
       «Сбор цен» (после «№ … Ед. изм.» - все цены, compare_view_fields();
       прежнее «Сравнение цен» / «Стоимость товара» переименовывается в него),
       4) с 0.1.37 - убрать новые колонки из «Цены агента» / «Закупки»
       (refresh_script_views). Основное представление не меняется (новые
       колонки появляются в нём в конце)."""
    return ensure_compare_table(token, find_spec_table_id(token, spec_id))


def ensure_compare_table(token, table_id):
    """То же, что ensure_compare_view(), но по id таблицы. С 0.1.28 вызывается
    и из sync_specification() - для НОВЫХ таблиц спецификации и для таблиц,
    где «Сбор цен» уже есть (решение пользователя 16.10.2026: новые
    конкурсы - как К910)."""
    migrate_compare_view_title(token, table_id)
    before = set(get_table_columns(token, table_id))
    ensure_spec_columns(token, table_id, only=PROCUREMENT_FIELDS + MARKET_OFFER_FIELDS)
    columns = get_table_columns(token, table_id)
    issues = [f"нет колонки: {c}" for c in PROCUREMENT_FIELDS + MARKET_OFFER_FIELDS if c not in columns]
    formulas = []
    if not issues:
        for title, formula, meta, column_name in compare_formula_columns():
            body = {"title": title, "uidt": "Formula", "formula_raw": formula, "column_name": column_name}
            if meta:
                body["meta"] = meta
            existing = columns.get(title)
            if existing is None:
                st, b = _request("POST", f"{META_URL}/tables/{table_id}/columns", token, body)
                action = "created"
            elif (existing.get("colOptions") or {}).get("formula_raw") != formula:
                st, b = _request("PATCH", f"{META_URL}/columns/{existing['id']}", token, body)
                action = "updated"
            else:
                st, b, action = 200, {}, "unchanged"
            if st != 200:
                issues.append(f"формула «{title}»: HTTP {st} {b}")
            formulas.append({"title": title, "action": action if st == 200 else "error"})
            columns = get_table_columns(token, table_id)
        # Устаревшие формулы - только после обновления тех, что на них
        # ссылались (с 0.1.38 «Цена отдела закупок 1-3» больше не ссылается
        # на «лучшую цену»), и только если обновление прошло без ошибок.
        if not issues:
            for title in COMPARE_OBSOLETE_COLUMNS:
                if title in columns:
                    st, b = _request("DELETE", f"{META_URL}/columns/{columns[title]['id']}", token)
                    formulas.append({"title": title, "action": "deleted" if st == 200 else f"delete error HTTP {st}"})
            columns = get_table_columns(token, table_id)
    view = ensure_price_view(token, table_id, COMPARE_VIEW_TITLE, compare_view_fields(columns)) if not issues else \
        {"ok": False, "issues": ["представление не настроено - см. issues выше"]}
    if not view.get("ok"):
        issues += view.get("issues", [])
    # 0.1.37: созданные выше колонки (формулы, недостающие цены) NocoDB
    # показал и в «Цены агента» / «Закупки» - убрать их оттуда.
    others = refresh_script_views(token, table_id, [PRICE_VIEW_TITLE, PURCHASE_VIEW_TITLE])
    created = [c for c in get_table_columns(token, table_id) if c not in before]
    issues += [f"не удалена колонка: {c}" for c in COMPARE_OBSOLETE_COLUMNS if c in get_table_columns(token, table_id)]
    return {"action": "compare_view_only", "table_id": table_id, "created_columns": created,
            "formulas": formulas, "view": view, "other_views": others, "issues": issues, "ok": not issues}


def ensure_spec_structure(token, spec_id):
    """--structure-only (0.1.22): только структура уже сохранённой таблицы
    спецификации - досоздать недостающие колонки SPEC_COLUMNS и настроить
    представления, НЕ трогая строки. spec_id - «Внутренний номер» / «Номер
    конкурса» / заголовок таблицы."""
    table_id = find_spec_table_id(token, spec_id)
    before = set(get_table_columns(token, table_id))
    ensure_spec_columns(token, table_id)
    created = [c for c in get_table_columns(token, table_id) if c not in before]
    missing = [name for name, _, _ in SPEC_COLUMNS if name not in get_table_columns(token, table_id)]
    views = ensure_spec_views(token, table_id)
    # 0.1.37: если в таблице есть «Сбор цен» (в т.ч. переименованное из
    # «Стоимость товара») - довести его до полного набора полей и формул.
    if has_view(token, table_id, COMPARE_VIEW_TITLE):
        try:
            cmp_res = ensure_compare_table(token, table_id)
            views[COMPARE_VIEW_TITLE] = {"ok": cmp_res["ok"], "issues": cmp_res["issues"]}
        except Exception as e:
            views[COMPARE_VIEW_TITLE] = {"ok": False, "issues": [str(e)]}
    return {"action": "structure_only", "table_id": table_id, "created_columns": created,
            "missing_columns": missing, "views": views,
            "ok": not missing and all(v.get("ok") for v in views.values())}


def sync_specification(token, purchase_number: str, spec_rows: list, existing_table_id: str = None):
    """Ensure a per-tender spec table exists, (re)fill it with spec_rows.

    spec_rows: list of dicts keyed by the SPEC_COLUMNS titles (e.g. one dict per
    row of Позиции_и_цены.xlsx, as read by openpyxl with the header row as keys).
    Re-running this for the same purchase_number replaces the table's contents
    rather than duplicating rows - safe to re-run after a correction.

    Returns (url, table_id, safe_rows) - table_id and the sanitized rows that
    were actually sent (see sanitize_spec_row) are returned alongside the url
    so the caller (upsert(), see verify_spec_table() above) can re-read the
    table afterwards and verify it landed correctly, without re-deriving what
    was sent from scratch.
    """
    title = _spec_table_title(purchase_number)
    table = None
    manual = {}
    created_now = False
    if existing_table_id:
        # Table already linked from the main record - reuse it (and bring an
        # old "Спецификация — ..." title in line with the internal number).
        try:
            table = rename_table(token, existing_table_id, title)
        except RuntimeError:
            table = None
    if table is None:
        table = find_table_by_title(token, title)
    if table is None:
        table = create_spec_table(token, title)
        created_now = True
    else:
        ensure_spec_columns(token, table["id"])
        manual = read_manual_fields(token, table["id"])
        clear_table_records(token, table["id"])

    table_id = table["id"]
    spec_rows = restore_manual_fields(spec_rows or [], manual)
    safe_rows = [sanitize_spec_row(r) for r in spec_rows] if spec_rows else []
    if safe_rows:
        # NocoDB's bulk-insert endpoint caps a single request at 100 records
        # (HTTP 422 ERR_MAX_PAYLOAD_LIMIT_EXCEEDED) - found 2026-09-09 on a
        # real 116-position tender. Split into batches to stay under that cap.
        NOCODB_MAX_BATCH = 100
        for i in range(0, len(safe_rows), NOCODB_MAX_BATCH):
            batch = safe_rows[i:i + NOCODB_MAX_BATCH]
            status, body = _request("POST", f"{BASE_URL}/{table_id}/records", token, batch)
            if status != 200:
                raise RuntimeError(f"insert spec rows failed: HTTP {status} {body}")

    ensure_spec_views(token, table_id)
    # «Сбор цен» (Шаг 5.1j): новые таблицы - всегда; старые - только если
    # представление в них уже есть (К910, К939; с 0.1.37 - и переименованное
    # из «Стоимость товара» в К925-К931: при перезаливке оно получает полный
    # набор полей). ensure_compare_table() в конце сам убирает новые колонки
    # из «Цены агента» / «Закупки» (0.1.37).
    has_compare = has_view(token, table_id, COMPARE_VIEW_TITLE)
    if created_now or has_compare:
        try:
            cmp_res = ensure_compare_table(token, table_id)
            if not cmp_res["ok"]:
                print(f"WARNING: представление «{COMPARE_VIEW_TITLE}»: {cmp_res['issues']}", file=sys.stderr)
        except Exception as e:
            print(f"WARNING: представление «{COMPARE_VIEW_TITLE}»: {e}", file=sys.stderr)

    view_id = get_default_view_id(token, table_id)
    url = f"https://app.nocodb.com/{WORKSPACE_ID}/{BASE_ID}/{table_id}"
    if view_id:
        url += f"/{view_id}"
    return url, table_id, safe_rows


def read_spec_xlsx(xlsx_path: str):
    """Read Позиции_и_цены.xlsx into one dict per actual item row.

    Skips the trailing "Итого НМЦК:" totals row and any other non-item row
    (a blank separator row, or a sheet-level note row that only fills the
    "№"/"Примечание" cells with free text) - a real position always has
    "Наименование позиции" filled, so that's what's used to tell a real row
    from a summary/note row (confirmed against real tenders' xlsx files
    2026-09-04: RAD260041848's "Итого НМЦК:" row, 9594163's blank row and
    its trailing "Примечание: ..." note row all lack it). NocoDB's per
    -tender spec table is meant to be an item list, not a place for the
    tender-level total or free-standing notes.
    """
    import openpyxl
    wb = openpyxl.load_workbook(xlsx_path, data_only=True)
    ws = wb.worksheets[0]
    rows = list(ws.iter_rows(values_only=True))
    headers = rows[0]
    records = []
    for r in rows[1:]:
        rec = {h: v for h, v in zip(headers, r) if v is not None and h is not None}
        if rec and str(rec.get("Наименование позиции") or "").strip():
            records.append(rec)
    return records


# Currency symbol -> what it's called in the conversion note. Extend this
# (and pass the matching --<code>-rate on the CLI, lower-cased) if a tender
# ever quotes a market price in some other currency.
MARKET_PRICE_CURRENCIES = {"€": "EUR", "$": "USD"}


def convert_market_price_to_rub(raw_text, rates: dict = None):
    """Derive the clean "Рыночная цена, руб." value + a conversion note (or
    None, None) from the free-text "Рыночная цена (найдено в интернете)"
    cell.

    "Рыночная цена (найдено в интернете)" is deliberately left as plain text
    (see the SPEC_COLUMNS comment) because it routinely mixes currencies and
    commentary - e.g. "≈3 500 € за шт. (ориентировочно; ...)" or "$12 940,80
    (зарубежный дистрибьютор, ...)", not just RUB numbers with a comment.
    This function is what actually does the currency-aware parsing so that
    work doesn't have to be redone by hand for every tender (added
    2026-09-04, after doing it as a one-off for tender RAD260041848).

    rates: {"€": <RUB per EUR>, "$": <RUB per USD>, ...} - today's CBR rate
    for whichever foreign currencies actually show up in this tender's data
    (look them up with a web search, e.g. "курс доллара евро ЦБ РФ сегодня",
    and pass them via --usd-rate/--eur-rate). A currency symbol found in the
    text with no matching rate in `rates` is left unconverted (returns None
    for the value, with a note explaining why) rather than guessed at -
    silently treating "$12 940,80" as 12940.80 RUB would be badly wrong.

    Returns (rub_value_or_None, note_or_None).
    """
    if raw_text is None:
        return None, None
    text = str(raw_text).strip()
    if not text:
        return None, None
    lowered = text.lower()
    if lowered.startswith("не удалось определить") or lowered.startswith("не указано"):
        return None, None

    rates = rates or {}
    currency_symbol = None
    for sym in MARKET_PRICE_CURRENCIES:
        if sym in text:
            currency_symbol = sym
            break

    value = parse_ru_number(text)
    if value is None:
        return None, None

    if currency_symbol is None:
        # No foreign-currency symbol found - already RUB (₽/руб. or a bare number).
        return round(value, 2), None

    rate = rates.get(currency_symbol)
    if rate is None:
        return None, (
            f"Рыночная цена, руб.: не переведено — курс {currency_symbol}/руб не передан "
            f"(--{MARKET_PRICE_CURRENCIES[currency_symbol].lower()}-rate) для \"{text}\""
        )

    rub = round(value * rate, 2)
    note = (
        f"Рыночная цена, руб.: переведено из {value:.2f} {currency_symbol} по курсу ЦБ РФ "
        f"на {date.today().strftime('%d.%m.%Y')} (1 {currency_symbol} = {rate} руб.)"
    )
    return rub, note


def enrich_market_price_rub(records: list, rates: dict = None) -> list:
    """Fill in "Рыночная цена, руб." (and a conversion note in "Примечание")
    on each spec row, derived from its "Рыночная цена (найдено в интернете)"
    text - see convert_market_price_to_rub(). Does not mutate the input."""
    out = []
    for rec in records:
        rec = dict(rec)
        if "Рыночная цена (найдено в интернете)" in rec:  # только старые xlsx (0.1.28)
            rub_value, note = convert_market_price_to_rub(rec.get("Рыночная цена (найдено в интернете)"), rates)
            rec["Рыночная цена, руб."] = rub_value
            if note:
                existing = str(rec.get("Примечание") or "").strip()
                rec["Примечание"] = f"{existing}; {note}" if existing else note
        # «Цена агента N» должна быть в рублях; если агент всё же записал её
        # в €/$ - переводим тем же курсом, пояснение - в «Комментарий агента N».
        for price_col, comment_col in AGENT_PRICE_PAIRS:
            raw = rec.get(price_col)
            if isinstance(raw, str) and any(sym in raw for sym in MARKET_PRICE_CURRENCIES):
                value, cnote = convert_market_price_to_rub(raw, rates)
                rec[price_col] = value
                if cnote:
                    cnote = cnote.replace("Рыночная цена, руб.", price_col)
                    existing = str(rec.get(comment_col) or "").strip()
                    rec[comment_col] = f"{existing}\n{cnote}" if existing else cnote
        # «Стоимость товара ... - предложение N» тоже только в рублях; цену в
        # €/$ (обычно зарубеж) переводим, пояснение - в «Примечание».
        for price_col in MARKET_OFFER_PRICE_COLUMNS:
            raw = rec.get(price_col)
            if isinstance(raw, str) and any(sym in raw for sym in MARKET_PRICE_CURRENCIES):
                value, cnote = convert_market_price_to_rub(raw, rates)
                rec[price_col] = value
                if cnote:
                    cnote = cnote.replace("Рыночная цена, руб.", price_col)
                    existing = str(rec.get("Примечание") or "").strip()
                    rec["Примечание"] = f"{existing}; {cnote}" if existing else cnote
        out.append(rec)
    return out


def _normalize_compare(value):
    """Normalize a value (from either the row we sent or NocoDB's response) for
    equality comparison in verify_main_record/verify_spec_table below -
    stringifies, normalizes newlines, strips surrounding whitespace. None and
    empty string both collapse to "" so "field wasn't sent" and "field is
    literally blank" compare the same way."""
    if value is None:
        return ""
    text = str(value).replace("\r\n", "\n").replace("\r", "\n")
    return text.strip()


def _values_match(expected, actual, numeric=False):
    """True if `expected` (what this run intended to send) and `actual` (what
    NocoDB now holds) should be considered the same value. numeric=True
    compares as floats with a small tolerance (Decimal columns round-trip
    through JSON as numbers, not strings) instead of exact text."""
    if numeric:
        try:
            e = float(expected) if expected not in (None, "") else None
        except (TypeError, ValueError):
            e = None
        try:
            a = float(actual) if actual not in (None, "") else None
        except (TypeError, ValueError):
            a = None
        if e is None and a is None:
            return True
        if e is None or a is None:
            return False
        return abs(e - a) < 0.005
    return _normalize_compare(expected) == _normalize_compare(actual)


def verify_main_record(token, record_id, expected_row):
    """Re-fetches the just-written record from "Реестр конкурсов" (Шаг 7.1 в
    CLAUDE.md) and compares every column this run actually sent (`expected_row`
    - the same dict passed to the create/update request) against what NocoDB
    now holds. Added 2026-09-09 after this project's memory already recorded
    two incidents where a column silently stayed empty in NocoDB even though
    the script itself reported success ("Файл-источник" on 2026-09-07,
    "Номер конкурса" missing as a column entirely, also found 2026-09-07) -
    this makes that class of bug visible automatically on every run instead of
    waiting for someone to notice by chance.

    Best-effort: if the verification GET itself fails (network hiccup, etc.)
    that's reported as a single issue rather than raised - the record write
    already succeeded by the time this runs, so a failed *check* shouldn't be
    reported as a failed *write*.

    Returns {"ok": bool, "checked_fields": int, "issues": [...]}. Each issue
    is {"column": ..., "expected": ..., "actual": ..., "problem": "пусто в
    NocoDB" | "значение не совпадает"}.
    """
    try:
        status, actual_record = _request("GET", f"{RECORDS_URL}/{record_id}", token)
    except Exception as e:
        return {"ok": False, "checked_fields": 0, "issues": [
            {"column": None, "expected": None, "actual": None,
             "problem": f"не удалось перечитать запись для проверки: {e}"}
        ]}
    if status != 200:
        return {"ok": False, "checked_fields": 0, "issues": [
            {"column": None, "expected": None, "actual": None,
             "problem": f"не удалось перечитать запись для проверки: HTTP {status} {actual_record}"}
        ]}

    issues = []
    for col, expected_value in expected_row.items():
        actual_value = actual_record.get(col)
        if not _values_match(expected_value, actual_value):
            issues.append({
                "column": col,
                "expected": expected_value,
                "actual": actual_value,
                "problem": "пусто в NocoDB" if _normalize_compare(actual_value) == "" else "значение не совпадает",
            })
    return {"ok": not issues, "checked_fields": len(expected_row), "issues": issues}


def verify_spec_table(token, table_id, expected_rows):
    """Re-fetches every row just written into a "Спецификация — ..." table
    (Шаг 7.1 в CLAUDE.md) and compares it against `expected_rows` (the
    already-sanitized rows sync_specification() actually sent - see
    sanitize_spec_row()): row count, "Наименование позиции", and the numeric
    columns (Кол-во / НМЦК за позицию / Рыночная цена, руб.). Matched by
    insertion order (NocoDB assigns Id sequentially within a bulk insert -
    the same assumption clear_table_records() already relies on for cleanup).
    Added 2026-09-09 alongside verify_main_record() - see its docstring for
    why.

    Returns {"ok": bool, "expected_rows": int, "actual_rows": int or None,
    "issues": [...]}. A row-count mismatch is reported on its own (row-by-row
    comparison is skipped in that case, since positions can no longer be
    trusted to line up). Each per-row issue is {"row": <1-based index>,
    "item": <"Наименование позиции">, "column": ..., "expected": ...,
    "actual": ..., "problem": ...}.
    """
    try:
        status, body = 200, {"list": list_all_records(token, table_id)}
    except Exception as e:
        return {"ok": False, "expected_rows": len(expected_rows), "actual_rows": None, "issues": [
            {"row": None, "item": None, "column": None, "expected": None, "actual": None,
             "problem": f"не удалось перечитать таблицу спецификации для проверки: {e}"}
        ]}
    if status != 200:
        return {"ok": False, "expected_rows": len(expected_rows), "actual_rows": None, "issues": [
            {"row": None, "item": None, "column": None, "expected": None, "actual": None,
             "problem": f"не удалось перечитать таблицу спецификации для проверки: HTTP {status} {body}"}
        ]}

    actual_rows = sorted(body.get("list", []), key=lambda r: r.get("Id", 0))
    issues = []
    if len(actual_rows) != len(expected_rows):
        issues.append({
            "row": None, "item": None, "column": None, "expected": None, "actual": None,
            "problem": f"количество строк не совпадает: отправлено {len(expected_rows)}, найдено в NocoDB {len(actual_rows)}",
        })
    else:
        checked_columns = ("Наименование позиции", TZ_COLUMN, DOC_ERRORS_COLUMN, ANALOG_TEXT_COLUMN, "Производитель", "Страна происхождения", "Аналог допускается", ANALOG_STATUS_COLUMN, "URL") + tuple(c for _, c in AGENT_PRICE_PAIRS) + MARKET_OFFER_LINK_COLUMNS + PROCUREMENT_SUPPLIER_COLUMNS + (PROCUREMENT_COMMENT_COLUMN,) + NUMERIC_SPEC_COLUMNS
        for idx, (expected, actual) in enumerate(zip(expected_rows, actual_rows), start=1):
            item_name = expected.get("Наименование позиции") or f"строка {idx}"
            for col in checked_columns:
                expected_value = expected.get(col)
                actual_value = actual.get(col)
                numeric = col in NUMERIC_SPEC_COLUMNS
                if not _values_match(expected_value, actual_value, numeric=numeric):
                    issues.append({
                        "row": idx,
                        "item": item_name,
                        "column": col,
                        "expected": expected_value,
                        "actual": actual_value,
                        "problem": "пусто в NocoDB" if _normalize_compare(actual_value) == "" else "значение не совпадает",
                    })
    return {"ok": not issues, "expected_rows": len(expected_rows), "actual_rows": len(actual_rows), "issues": issues}

# --- Файлы для поставщиков (добавлено 2026-09-30, версия 0.1.6, Шаг 7.1b в CLAUDE.md) ---
# Attachment-колонка в "Реестр конкурсов": по одному .docx на производителя
# (tools/make_supplier_files.py) - пользователь рассылает их поставщикам.
# Загрузка в два шага: файл -> POST /api/v2/storage/upload (multipart),
# ответ (список объектов {path/url, title, mimetype, size, ...}) записывается
# в колонку PATCH-ем записи. Повторная синхронизация ЗАМЕНЯЕТ весь набор
# файлов в ячейке (а не дописывает), чтобы не копились старые версии.
SUPPLIER_FILES_COLUMN = "Файлы для поставщиков"
STORAGE_UPLOAD_URL = "https://app.nocodb.com/api/v2/storage/upload"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def ensure_attachment_column(token, table_id=TABLE_ID, title=SUPPLIER_FILES_COLUMN):
    if title in get_table_columns(token, table_id):
        return
    status, body = _request("POST", f"{META_URL}/tables/{table_id}/columns", token,
                            {"title": title, "uidt": "Attachment"})
    if status != 200:
        raise RuntimeError(f"create column '{title}' failed: HTTP {status} {body}")


def upload_file(token, path):
    """Uploads one local file to NocoDB storage; returns the attachment object(s) list."""
    import os
    import uuid
    boundary = "----nc" + uuid.uuid4().hex
    filename = os.path.basename(path)
    mime = DOCX_MIME if filename.lower().endswith(".docx") else "application/octet-stream"
    with open(path, "rb") as f:
        payload = f.read()
    quoted = urllib.parse.quote(filename)
    head = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
            f"filename=\"{quoted}\"; filename*=UTF-8''{quoted}\r\nContent-Type: {mime}\r\n\r\n").encode("utf-8")
    body = head + payload + f"\r\n--{boundary}--\r\n".encode("utf-8")
    req = urllib.request.Request(STORAGE_UPLOAD_URL, data=body, method="POST")
    req.add_header("xc-token", token)
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"upload '{filename}' failed: HTTP {e.code} {e.read().decode('utf-8', 'replace')}")
    items = result if isinstance(result, list) else [result]
    for it in items:
        it["title"] = filename  # keep the real (Cyrillic) brand name, not the percent-encoded one
        it.setdefault("mimetype", mime)
    return items


def attach_files(token, record_id, paths, column=SUPPLIER_FILES_COLUMN):
    """Replaces the attachment cell `column` of record `record_id` with `paths`
    (creating the Attachment column if missing) and verifies by re-reading the
    record. Returns {"ok", "files", "issues"}."""
    ensure_attachment_column(token, title=column)
    attachments = []
    for p in paths:
        attachments.extend(upload_file(token, p))
    status, body = _request("PATCH", RECORDS_URL, token, {"Id": record_id, column: attachments})
    if status != 200:
        raise RuntimeError(f"attach files failed: HTTP {status} {body}")
    status, rec = _request("GET", f"{RECORDS_URL}/{record_id}", token)
    got = rec.get(column) or [] if status == 200 else []
    if isinstance(got, str):
        try:
            got = json.loads(got)
        except ValueError:
            got = []
    got_titles = sorted(a.get("title", "") for a in got)
    want_titles = sorted(a["title"] for a in attachments)
    issues = [] if got_titles == want_titles else [
        {"expected": want_titles, "actual": got_titles, "problem": "набор файлов в NocoDB не совпадает с отправленным"}]
    return {"ok": not issues, "column": column, "files": want_titles, "issues": issues}


def attach_supplier_files(token, record_id, paths):
    """Шаг 7.1b: файлы для поставщиков -> колонка «Файлы для поставщиков»."""
    return attach_files(token, record_id, paths, SUPPLIER_FILES_COLUMN)


# Шаг 7.1c (0.1.10): заполненные декларативные формы участника (Word)
DECLARATIVE_FORMS_COLUMN = "ДС: декларативные формы"


def attach_declarative_forms(token, record_id, paths):
    """Шаг 7.1c: заполненные декларативные формы -> колонка «ДС: декларативные формы»."""
    return attach_files(token, record_id, paths, DECLARATIVE_FORMS_COLUMN)


def find_record_any(token, key):
    """Finds a «Реестр конкурсов» record by «Номер конкурса» or, failing that,
    by «Внутренний номер» (Latin K / Cyrillic К both tried)."""
    rec = find_record(token, PURCHASE_NUMBER_COLUMN, key)
    if rec:
        return rec
    variants = {key, key.replace("K", "К", 1), key.replace("К", "K", 1)}
    for v in variants:
        rec = find_record(token, "Внутренний номер", v)
        if rec:
            return rec
    return None


APPLICATION_DOCS_COLUMN = "Перечень документов заявки"


def set_application_documents(token, record_id, text):
    """Шаг 7.1d: записать перечень обязательных документов заявки в колонку
    «Перечень документов заявки» (LongText) существующей записи и перечитать
    её для проверки. Колонка создаётся автоматически, если её нет."""
    ensure_columns(token, TABLE_ID, [APPLICATION_DOCS_COLUMN])
    row = {APPLICATION_DOCS_COLUMN: text}
    status, body = _request("PATCH", RECORDS_URL, token, {"Id": record_id, **row})
    if status != 200:
        raise RuntimeError(f"update failed: HTTP {status} {body}")
    return verify_main_record(token, record_id, row)


def _docx_in(folder):
    import os
    return sorted(os.path.join(folder, n) for n in os.listdir(folder)
                  if n.lower().endswith(".docx") and not n.startswith("~$"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", required=False, help="Path to the flat extracted-fields JSON (required unless --spec-only)")
    parser.add_argument("--spec-only", required=False, help="«Внутренний номер» или «Номер конкурса» (или fallback id) - re-fill ONLY the tender's spec table (named by the internal number) from --xlsx, main record untouched except «Ссылка на спецификацию»")
    parser.add_argument("--token-file", required=True, help="Path to the file containing the NocoDB API token")
    parser.add_argument("--xlsx", required=False, help="Path to Позиции_и_цены.xlsx - if given, also syncs it into its own NocoDB table and links it from the main record")
    parser.add_argument("--fallback-id", required=False, help="Identifier to name the spec table by when the tender has no real purchase_number (e.g. the tender's local folder name)")
    parser.add_argument("--usd-rate", required=False, type=float, help="Today's CBR RUB-per-USD rate (look it up first) - needed only if some position's Рыночная цена is quoted in $")
    parser.add_argument("--eur-rate", required=False, type=float, help="Today's CBR RUB-per-EUR rate (look it up first) - needed only if some position's Рыночная цена is quoted in €")
    parser.add_argument("--structure-only", required=False, help="«Внутренний номер» / «Номер конкурса» уже сохранённого тендера - ТОЛЬКО досоздать недостающие колонки спецификации и настроить представления («Цены агента», «Закупки»; «Сбор цен» - если оно в таблице есть, 0.1.37), строки не трогаются (0.1.22)")
    parser.add_argument("--procurement-columns-only", required=False, help="«Внутренний номер» / «Номер конкурса» уже сохранённого тендера - ТОЛЬКО досоздать недостающие колонки отдела закупок (поставщик/цена 1-10, комментарий), без других колонок и представлений; строки не трогаются (0.1.25)")
    parser.add_argument("--compare-view-only", required=False, help="«Внутренний номер» / «Номер конкурса» уже сохранённого тендера - досоздать колонки отдела закупок и цен по рынкам (РФ/зарубеж/Avito), формулы лучших цен и представление «Сбор цен» (до 0.1.37 - «Сравнение цен», переименовывается само); строки не трогаются (0.1.26)")
    parser.add_argument("--forms-only", required=False, help="«Номер конкурса» или «Внутренний номер» уже сохранённого тендера - ТОЛЬКО загрузить --declarative-forms-dir в колонку «ДС: декларативные формы», без JSON и без upsert")
    parser.add_argument("--declarative-forms-dir", required=False, help="Folder with filled declarative forms (.docx, Шаг 7.1c) - uploaded into the \"ДС: декларативные формы\" attachment column, replacing previous files")
    parser.add_argument("--docs-list-only", required=False, help="«Номер конкурса» или «Внутренний номер» уже сохранённого тендера - ТОЛЬКО записать текст из --docs-list-file в колонку «Перечень документов заявки» (Шаг 7.1d), без JSON и без upsert")
    parser.add_argument("--docs-list-file", required=False, help="UTF-8 .txt с перечнем обязательных документов заявки (Шаг 7.1d)")
    parser.add_argument("--supplier-files-dir", required=False, help="Folder with per-brand .docx files (tools/make_supplier_files.py) - uploaded into the \"Файлы для поставщиков\" attachment column, replacing previous files")
    args = parser.parse_args()

    with open(args.token_file, "r", encoding="utf-8") as f:
        token = f.read().strip()

    fx_rates = {}
    if args.usd_rate:
        fx_rates["$"] = args.usd_rate
    if args.eur_rate:
        fx_rates["€"] = args.eur_rate

    if args.structure_only:
        res = ensure_spec_structure(token, args.structure_only)
        if not res["ok"]:
            print("WARNING: структура спецификации настроена не полностью - см. \"missing_columns\"/\"views\".", file=sys.stderr)
        print(json.dumps(res, ensure_ascii=False))
        return

    if args.procurement_columns_only:
        res = ensure_procurement_columns(token, args.procurement_columns_only)
        if not res["ok"]:
            print("WARNING: колонки отдела закупок настроены не полностью - см. \"issues\".", file=sys.stderr)
        print(json.dumps(res, ensure_ascii=False))
        return

    if args.compare_view_only:
        res = ensure_compare_view(token, args.compare_view_only)
        if not res["ok"]:
            print(f"WARNING: представление «{COMPARE_VIEW_TITLE}» настроено не полностью - см. \"issues\".", file=sys.stderr)
        print(json.dumps(res, ensure_ascii=False))
        return

    if args.docs_list_only:
        if not args.docs_list_file:
            parser.error("--docs-list-only requires --docs-list-file")
        with open(args.docs_list_file, "r", encoding="utf-8") as f:
            text = f.read().strip()
        rec = find_record_any(token, args.docs_list_only)
        if not rec:
            print(json.dumps({"error": f"record not found: {args.docs_list_only}"}, ensure_ascii=False))
            sys.exit(1)
        res = {"action": "docs_list_only", "record_id": rec["Id"],
               "verification": set_application_documents(token, rec["Id"], text)}
        if not res["verification"].get("ok", True):
            print("WARNING: «Перечень документов заявки» verification failed - see \"verification\".", file=sys.stderr)
        print(json.dumps(res, ensure_ascii=False))
        return

    if args.forms_only:
        if not args.declarative_forms_dir:
            parser.error("--forms-only requires --declarative-forms-dir")
        rec = find_record_any(token, args.forms_only)
        if not rec:
            print(json.dumps({"error": f"record not found: {args.forms_only}"}, ensure_ascii=False))
            sys.exit(1)
        res = attach_declarative_forms(token, rec["Id"], _docx_in(args.declarative_forms_dir))
        res["record_id"] = rec["Id"]
        if not res["ok"]:
            print("WARNING: declarative forms verification failed - see \"issues\".", file=sys.stderr)
        print(json.dumps(res, ensure_ascii=False))
        return

    if args.spec_only:
        if not args.xlsx:
            parser.error("--spec-only requires --xlsx")
        result = sync_spec_only(token, args.spec_only, args.xlsx, fx_rates or None)
        if not result["spec_verification"].get("ok", True):
            print("WARNING: specification table verification failed - see \"spec_verification\".", file=sys.stderr)
        print(json.dumps(result, ensure_ascii=False))
        return
    if not args.json:
        parser.error("--json is required (unless --spec-only)")

    with open(args.json, "r", encoding="utf-8") as f:
        flat_data = json.load(f)

    result = upsert(token, flat_data, spec_xlsx_path=args.xlsx, fallback_id=args.fallback_id, fx_rates=fx_rates or None)

    if args.supplier_files_dir:
        import os
        paths = sorted(os.path.join(args.supplier_files_dir, n) for n in os.listdir(args.supplier_files_dir)
                       if n.lower().endswith(".docx") and not n.startswith("~$"))
        if paths:
            result["supplier_files"] = attach_supplier_files(token, result["record_id"], paths)
            if not result["supplier_files"]["ok"]:
                print("WARNING: supplier files verification failed - see \"supplier_files\" in the JSON output.", file=sys.stderr)

    if args.declarative_forms_dir:
        paths = _docx_in(args.declarative_forms_dir)
        if paths:
            result["declarative_forms"] = attach_declarative_forms(token, result["record_id"], paths)
            if not result["declarative_forms"]["ok"]:
                print("WARNING: declarative forms verification failed - see \"declarative_forms\" in the JSON output.", file=sys.stderr)

    verification = result.get("verification") or {}
    if not verification.get("ok", True):
        print(
            f"WARNING: post-write verification found {len(verification.get('issues', []))} "
            "issue(s) in the main record - see \"verification\" in the JSON output below.",
            file=sys.stderr,
        )
    spec_verification = result.get("spec_verification")
    if spec_verification and not spec_verification.get("ok", True):
        print(
            f"WARNING: post-write verification found {len(spec_verification.get('issues', []))} "
            "issue(s) in the specification table - see \"spec_verification\" in the JSON output below.",
            file=sys.stderr,
        )

    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
