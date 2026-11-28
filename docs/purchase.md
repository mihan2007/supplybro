# Закупка: прайсы, предложения, поставщики

Подробное описание для Claude. Перенесено из `CLAUDE.md` 28.11.2026 (0.45.5) без пересказа. Пункты с номером версии — как было сделано тогда; если два пункта противоречат друг другу, верен более поздний.

## Прайсы, КП, счета

- `app/pricelists.py` (0.8.0) — прайсы / КП / счета поставщиков, загрузка через чат (📎 или перетаскивание; решение
  пользователя 04.11.2026). Файл — в папку конкурса «Прайсы, счета, КП» (или существующую с /прайс/i), список и
  результаты — `AIExtract/pricelists.json` (блокировка `fcntl` в `DATA_DIR/locks`, запись через `os.replace`). Задача
  `pricelist` (очередь `pricelists`): текст — `docs.extract` (.xls/.doc — через LibreOffice), строки с номерами,
  длинный документ — только шапка, хвост и строки с артикулами / словами позиций (`PRICELIST_MAX_CHARS`); модель
  `PRICELIST_MODEL` (Haiku, effort low) отвечает по схеме: поставщик, ИНН, НДС, валюта, дата, совпадения [№, строка,
  цитата, цена, валюта, exact|partial, примечание]. Код проверяет цитату (строка / документ / нет), цену в строке,
  0,15–10× НМЦ, переводит в рубли (курс ЦБ); проблемы → ⚠ и без галочки. Позиции — из анализа, без него (с 0.20.0) — из спецификации NocoDB (`tender_positions`). Запись — задача
  `noco_purch` (очередь NocoDB) по кнопке: отмеченные позиции в колонки `nc.PROCUREMENT_PAIRS` («Отдел закупок:
  поставщик N / цена N, руб.», до 10 мест — только существующие колонки: у К939 10, у К934 3, проверено 04.11.2026) спецификации — `noco_links`, иначе ссылка из записи реестра, иначе таблица по
  названию; тот же поставщик (`_supplier_key`) — его место, иначе первое пустое, все заняты — пропуск; неполное
  совпадение — «🔴 » перед поставщиком; строка в «Отдел закупок: комментарий» без повторов; PATCH по 100. Структуру
  не меняет: нет таблицы или колонок — ошибка. Чужие цены не перезаписывает (только пустые места и место этого же
  поставщика), поэтому пишет и в спецификацию, созданную агентом — кнопка пользователя и есть подтверждение.
  Поставщика можно исправить (0.44.8): ✎ в «Предложениях» (вся колонка, `offers.rename_supplier`) и в карточке
  документа (`pricelists.rename_supplier`); `supplier_model` / `supplier_prev` / `supplier_edit`, запись в NocoDB —
  на прежнее место по `supplier_prev`.
  Поставщик ≠ наше юр. лицо (0.44.2): `own_entities` → строка «Наши организации…» в `task_prompt`; `is_own` →
  `seller_from_lines` («Поставщик / Продавец / Исполнитель…», подпись над или под названием), иначе пусто.
  API: `GET /api/clients/{id}/pricelists`, `POST …/pricelists/upload|write|remove` (право `prices`, журнал).

## «Предложения поставщиков»

- «Предложения поставщиков» (0.29.0, `app/offers.py`): данные — `pricelists.json` (цены из документов) + слой
  `AIExtract/offers.json` (cells `<file_id>|<№>` → selected / price_rub (исправлена) / edited_by; comments target
  `cell:` / `supplier:<_supplier_key>` / `pos:`; log), история — `AIExtract/history/offers_*.json`. `build` — строки
  позиций (`tender_positions`), колонки по `_supplier_key`, лучшая — min без warn; `select_best`, `edit_price`,
  `comment`, `write` (→ `pricelists.request_write(..., overrides)` → noco_purch, `MAX_SLOTS` 10; `write_noco` берёт
  overrides). Адреса `/api/clients/{id}/offers` (GET), `/offers/select|best|price|write` (право `offers`),
  `/offers/comment[/delete]` (purchase); раздел — prices. Страница: `#offers` (вкладка «Предложения»), `loadOffers` /
  `renderOffers` / `offerCell`, `#of-dlg`; `body.no-offers .need-offers`. Карточка прайса в чате — без записи.
  Пропущенные письма (0.28.1): `mail_inbox.relevant` → причина; status `skipped` (14 дней, `purge`), `STATE_VERSION`.
- «Предложения поставщиков» (0.41.0): `renderOffers` — колонки как в спецификации, фильтры `cf` scope `of`
  (`ofDefs`, `OF_CHIPS`, `ofRows`, `ofShown`), щелчок по цене — `ofToggle` (/offers/select); отмеченные среди скрытых —
  на кнопке записи и в подтверждении. Окно позиции `#op-dlg` (`openOpDlg` / `renderOpDlg` / `opEditPrice`; ← → по
  `ofShown`), открывается ✏ только из «Закупки» («Спецификация» — только данные из базы, без правки, 0.41.1). Чипы
  `OF_CHIPS` = те же шесть, что `PS_CHIPS` (интернет — из `psLast`, строка `r.ps`). Бренд / страна — `offers.build`.
- Ручной ввод и «не записано» (0.42.0): окно позиции «Закупки» — бренд / страна (`offers.edit_position` →
  `offers.json` `positions`, статус pending → requested → saved; `pending_position_edits`, `mark_positions`) и цена
  вручную (`offers.add_manual` — запись `pricelists.json` kind=manual, id `manual-<sha1>`; `remove_manual`).
  `offers.build`: у строк `pending` / `db_<поле>`, у предложений `manual`, `pos_pending`; `write` — и правки (`edits`).
  `pricelists.write_noco` → `_write_noco`: после цен PATCH «Производитель» / «Страна происхождения» по №.
  Страница: `ofFieldCell` (⏳), `#of-unsaved`, `#op-fields`, `#op-manual`. «Спецификация → Цены» — `psDb(r)` /
  `psDbRows()`: только данные NocoDB (rf / abroad с `from_noco`, поставщики `source == "NocoDB"` или `in_noco`, без
  «не проверено»), лучшая и «к НМЦ» пересчитываются (решение пользователя 26.11.2026: в спецификации — только база).

## Поставщики и бренды

- `app/supplier_brands.py` (0.26.0, пользователь 17.11.2026: «выделять позиции спецификации с брендами наших
  поставщиков»; колонка на сайте, не в NocoDB) — таблица «Поставщики» базы клиента («Бренды» + связь «Производители»,
  «Надёжность: статус», «Способ расчёта», «Сайт», почты — колонка «E-mail», несколько адресов в ячейке, `emails_of`),
  кэш в памяти `SUPPLIERS_CACHE_HOURS` (6). С 0.31.2 таблицу можно указать `nocodb.suppliers_table_id` в clients.json:
  у «Нашей» — «Поставщики» старой базы `mgf91gdj8fxail7` (в копии нет почт и половины записей).
  С 0.33.2 `load`: одно чтение за раз (`_ALOCKS`), только нужные колонки (`_wanted_fields`), копия на диске
  `DATA_DIR/suppliers/*.json` (после перезапуска — она; NocoDB не ответил / 429 — последняя копия). `main.api_spec` →
  `_spec_brands` (спрятанные в представлении колонки марки / производителя — одним запросом `fields`) → `brands`
  {rows [{i, n, level green|yellow|grey, strong, brands [{brand, strong, ids}]}], suppliers, summary}. Совпадение:
  колонки `BRAND_COL_RE` (кроме «Страна…») — strong; «Наименование позиции» — weak, «совместим… / аналог…» перед
  брендом — нет; написания — `ALIASES` / `DISPLAY`. Страница: `renderSpec(columns, rows, brands)` — колонка «Наши
  поставщики» (`brandCell`), классы строк `sb-green` / `sb-yellow` / `sb-weak`; `renderBrands` — `#spec-brands`
  («Кому писать») и `#head-brands` в шапке; `loadSpec(true)` — «↻ обновить поставщиков».
- База поставщиков (0.34.0, `app/supplier_base.py`): `#me-suppliers` (шапка, purchase) и `#rq-suppliers` → `#sup-dlg`
  (`openSuppliers` / `renderSuppliers` / `supRow` / `supForm`, список грузится раз на клиента, показ — до 200
  совпадений). Таблица — та же, что у `supplier_brands` (у «Нашей» — старая база агента), запись — сразу из сайта
  (`nc._request`: POST / PATCH / DELETE), затем `supplier_brands.patch_cache`. Колонки `FIELDS` (только существующие,
  правимых типов), выпадающие — варианты из `table_meta` (`options`). Дубли при добавлении — `need_confirm`.
  «Не работаем» — вариант статуса с этими словами, «Вернуть» — «…Требуется проверка». Удаление насовсем — право
  offers, остальное — purchase (`access.RULES`). В списке длинный текст обрезан (`cut`), правка берёт запись целиком
  (`GET …/suppliers/{Id}`).
