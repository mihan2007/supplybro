# Подготовка заявки

Подробное описание для Claude. Перенесено из `CLAUDE.md` 28.11.2026 (0.45.5) без пересказа. Пункты с номером версии — как было сделано тогда; если два пункта противоречат друг другу, верен более поздний.

## Заявка

- `app/bid/` (0.15.0) — подготовка заявки (задача `bid`, своя очередь), пользователь 05.11.2026: «кнопка подготовка
  документов должна быть на сайте, далее идёт полная подготовка, с минимумом вопросов» (инструкции агента
  PARTICIPATION.md + PREPARE_DOCS.md, шаги 7.1b–7.1d). `participant.load(client, entity)` — материалы юр. лица
  (`materials`): выжимка → `profile` (строки с паспортом / СНИЛС / датой рождения выкинуты — модели не передаются),
  карточка → `requisites` (`df.load_requisites`; паспорт — только для метки `{{ПАСПОРТ_РУКОВОДИТЕЛЯ}}`), опись →
  `library` (только существующие файлы; файлы «NNN …» без описи тоже). `run.prepare_bid`: (1) два вызова параллельно
  на общем кэше документов (`build_documents_block`, как анализ): `req_prompt` → requirements / structure / notes,
  `docs_prompt` → documents (n, title, part single/1/2/price/after_win, required yes/conditional/not_needed, kind
  form/tech/price/inventory/library/third_party/user/other, library + library_status, who, file_name, note, quote);
  цитаты проверяет `QuoteIndex`; max_tokens 64000 / 32000 (у Sonnet ответ состава ~37 тыс. токенов, из них ~24 тыс. —
  размышления); (2) `forms.outline` файлов Word документации (`.doc` → копия из `_converted` по sha256; заголовки
  «Форма / Образец / Приложение», стили заголовков и абзац после «Приложение … к письму») → `forms_prompt` →
  {doc_n, source, start, end} для документов kind form/tech/price/inventory с required=yes (кто готовит — не важно:
  форму документации сайт заполняет всегда); одна форма для двух документов — один раз (`also`); (3) по форме —
  вызов `fill_prompt` (разметка `forms.markup`: [А i] абзацы, [Т i] таблицы r/c, «(номер — автоматически)» —
  автонумерация Word) → правки par / replace / cell / cell_replace / add_rows / delete, вносит `forms.apply`
  (`replace_once` — точно или нестрого: любые подчёркивания, пробелы, неразрывные пробелы, кавычки; номера в ячейки с
  автонумерацией не пишутся; прежние «<нет данных>» сохраняют подсветку). `[[что нужно]]` → MARK + строка в GAPS;
  `{{МЕТКА}}` / `{{МЕТКА:№}}` (forms.TOKENS) — цены, НДС, прописью, паспорт — подставляет код. Опись — последней со
  списком файлов пакета. (4) `_assemble` (и `reapply` — без модели, по сохранённым правкам): прежний пакет →
  `AIExtract/history/Заявка <время>` (5 последних), копии из библиотеки (`_copy_library`, TIFF/JPG → PDF, .p7s/.sig/
  .xml/.zip не копируются), формы в `AIExtract/Заявка[/1 Первая часть|2 Вторая часть|3 Ценовое предложение|После
  победы]`, имя — `file_name` / «Форма N название» / название, повтор имени — `unique`; marks > 0 → «ЧЕРНОВИК »;
  `price_values` (процент или сумма с/без НДС → коэффициент к НМЦ; есть «Стоимость НМЦ» у всех позиций — цены
  позиций, сумма по ним; базис — `structure.price_basis`, «не указано» = без НДС; ставка — `materials.vat_rate`);
  самопроверка (wrong_values, ИНН, метки, анонимность первой части, цена > НМЦ); `Нет данных.txt`, `Перечень
  документов заявки.txt` (`docs_list_text`, формат агента), `_supplier_files` (только при полной подготовке; папка
  агента «Поставщикам» — не трогается → «Поставщикам (сайт)»; «HYDAC International» + «HYDAC» — одно письмо).
  Статусы документа: ready / copied / draft / update / urgent / manual / conditional / third_party / accountant /
  user / not_needed. Отчёт `AIExtract/bid.json` (plan, forms с правками, documents, checks, gaps, price,
  price_values, usage). `bid/noco.py` (задача `noco_bid`, очередь NocoDB, только по кнопке): перечень → «Перечень
  документов заявки», формы .docx → «ДС: декларативные формы» (прежние вложения ячейки остаются, кроме тех же имён
  без «ЧЕРНОВИК »); колонок нет — не создаёт, пишет в отчёт. `bid/money.py` — суммы и прописью. Адреса:
  `GET /bid` (`_bid_view`: без правок модели; files, usage_totals; models — только admin), `POST /bid/run?entity&model`
  (409, пока идёт подготовка документов или анализ), `POST /bid/price` {mode percent|total|none, value, vat} →
  задача bid с `reapply`, `POST /bid/noco`, `GET /bid/file?path` (только AIExtract/Заявка и Поставщикам*),
  `GET /bid/zip`. Права — `analyze` (`access.RULES`), раздел — `bid` (`BLOCK_PATHS`). Модель — `BID_MODEL`
  (0.15.1: `deepseek-flash`; DeepSeek — через `DeepSeekJSON`, остальное как у Claude), выбор администратора — из
  `BID_MODELS` (подписи с ценой — `BID_MODEL_LABEL` в app.js, localStorage `supplybro.bidModel2`); `BID_PARALLEL`,
  `BID_MAX_FORMS`. Код страхует ошибки модели: «% %», «начало / конец формы» (`forms.SERVICE_RE`), cell ↔ par по виду
  элемента, несколько номеров библиотеки (`run.library_numbers`). Страница: `#bid` — `loadBid` / `renderBid*`,
  вкладки `data-btab` (docs, need, req, files, notes), «не требуется» скрыты (`bidShowAll`), цена — форма `#bid-price` (`dataset.touched`). К933: Sonnet —
  44 документа, 12 форм, ≈ 7 мин, $1,5–2; DeepSeek Flash — 35 документов, 11 форм (тот же набор, без отдельного ПДн),
  3,5 мин, $0,11.
