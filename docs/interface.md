# Страница конкурса: вкладки и разделы

Подробное описание для Claude. Перенесено из `CLAUDE.md` 28.11.2026 (0.45.5) без пересказа. Пункты с номером версии — как было сделано тогда; если два пункта противоречат друг другу, верен более поздний. Актуальный набор вкладок — «Вкладки с 0.43.0».

## Вкладки сейчас

- Вкладки с 0.43.0 (пользователь 26.11.2026): `TABS` = tender, spec, purchase, web, bid, chat; `#prices` —
  data-group `web` («Интернет»); вкладки `mail` нет (`setTab("mail")` → purchase), `#requests` — в «Закупке»
  (с 0.45.3 — первым, CSS order 1; «Предложения» — order 2) с подвкладками `#rq-subtabs` (`data-sub` threads / pos / new / sent на `#requests`, скрытие — CSS,
  `rqSub`, `rqSubSync`, localStorage `sb.rq.sub`); загрузка писем — `requestsWanted()` = «Закупка» и `rqInView`
  (IntersectionObserver или выбор подвкладки). Поле `#of-drop` под таблицей: щелчок → `#of-files`, drop, Ctrl+V
  (`pastedFiles`: картинка → «Скриншот <время>.png»; текст — только при фокусе на поле) → `uploadPricelists`.
- 0.38.0: вид «По позициям» (0.31.0, #workbench) и «☰ одной страницей» (вкладка all) удалены — пользователь
  26.11.2026: «работают некорректно, раздувают код». TABS = tender, purchase, mail, bid, chat; ensureRequests — только
  вкладка mail; «Запросить по выбранным» — `askPositions` (таблица «Позиции и поставщики»). Описания 0.31.0 ниже —
  история.
- Вкладки с 0.39.0: tender (паспорт → анализ → документы; `#docs.has-docs` — order 10, без документов — наверху),
  spec (`#spec`, `data-group="spec"`), purchase (предложения, цены), mail, bid, chat. Подвкладка анализа — «Позиции
  из документов».
- Вкладки (0.30.0, пользователь 18.11.2026 — новый интерфейс; прежнее решение «страница одна» заменено, старый вид —
  «☰ одной страницей»): `#jump-wrap` под `#head`, `.jump[data-tab]` tender / purchase / bid / chat / all;
  `section[data-group]` (docs, analysis, card → tender; spec → tender и purchase (0.30.1, `~=`); offers, requests,
  prices → purchase; bid; chat; порядок во «Закупке» — CSS `order`);
  скрытие — CSS по `main[data-tab]`; `setTab` / `goTo(id)` / `tabsNav()` (MutationObserver на hidden), localStorage
  `sb.tab`, по умолчанию `defaultTab()` (no-upload + purchase → purchase). Строка этапов `#head-stages` —
  `stg` (docs, analysis, req, offers, bid, brands) из загрузчиков разделов, `renderStages`. Карточки прайсов —
  `renderSupplierDocs` в `#of-docs` (не в чате), загрузка ещё и `#of-files`. До 0.30.0 — порядок ниже.

## Разделы

- «Паспорт конкурса» (0.38.2): раздел `#card` (было «Данные из NocoDB») стоит в разметке между `#docs` и `#analysis` —
  во вкладке «Конкурс»: документы → паспорт → анализ → спецификация.
- «Спецификация» (0.40.0): `#spec-mode` Позиции / Цены (`specModeInit` из showTender, `setSpecMode`, `sb.spec.mode`;
  `.spec-pos-only` / `.spec-prices-only`); «Цены» — `#ps-wrap` / `renderPsTable` (бывш. «Все цены», данные `psLast`
  из `/prices/summary`, грузит `loadPrices` → `loadPriceSummary`). Фильтры колонок: `cf` (ps / spec / chips / q),
  `cfTh` (кнопка «▾»), `cfOpen` (всплывающий список, значения по строкам без своего фильтра), `cfPass`, `psDefs`,
  `PS_CHIPS`, `specDefs` (`SPEC_NO_FILTER`), `renderSpecFiltered` (строки с `_i` — индекс для «Наших поставщиков»),
  `cfReset` при смене конкурса. Сервер: `SPEC_ALWAYS` / `_spec_extra` (Производитель, Страна происхождения, НМЦ за ед.),
  `price_summary` — `maker`, `country`.
- «НМЦ за ед.» (0.39.1): `main._spec_nmc` в `api_spec` — «Стоимость НМЦ» под этим названием после «Ед. изм.» в любом
  представлении (скрытая — дочитывается по Id); исходная колонка из ответа убирается, чтобы не было двух.
- Высота строк спецификации (0.29.1): `#spec-rh` (1 / 2 = 3 строки / 3 = 6 строк / all), localStorage `sb.spec.rh`,
  классы `rh-*` на `#spec-table`, содержимое ячейки — `td > .cell` (line-clamp), `title` — `cellText(v)`, щелчок —
  `.cell.open`.
- Поля анализа во всю высоту (0.38.1): `#an-fields` при ширине > 760 px — без max-height / overflow, `th` прилипает
  под вкладками (`top: var(--sticky-h)`). Спецификацию так делать не надо (пользователь 26.11.2026: в «Закупке» —
  «верни как было»).

## Панель «Конкурсы»

- Панель «Конкурсы» (0.13.0, пользователь 04.11.2026: «вместо выпадающего списка при входе — панель с тендерами
  выбранного лица, чтобы открывать в отдельной вкладке; номер, заказчик, наименование оборудования, минимум сведений»).
  `/api/clients/{id}/tenders` читает ещё «Заказчик», «Начальная цена», «Валюта», «Статус конкурса (выиграли -
  проиграли)» (только существующие колонки) и добавляет к каждой записи `tender_list.brief(...)`: `customer` (без
  «Заказчик:», полного названия в скобках, организатора), `subject` (без ОКПД2 / КТРУ и «Список позиций»),
  `stage_short`, `deadline_at` {date, time, local}, `price` {value, currency, vat}, `result`, и `*_full` для подсказок;
  «Не указано…» — пусто (`NOT_FOUND`). `app.js`: `showTender(null)` → `#tlist` и `#pickers.mode-list` (поле «Конкурс»
  и «☰ Все конкурсы» скрыты), с конкурсом — `mode-tender`; `renderTenderList()` — фильтр `#tl-filter` (data — есть
  заказчик / предмет / номер или папка; open — срок подачи впереди; all), поиск `#tl-search` (`tlNorm`: k→к, ё→е),
  сортировка `tlSort` (no / price / deadline, без значения — внизу), localStorage `sb.tl.filter` / `sb.tl.sort`;
  строка — `tr[data-id]`, номер — `a.tl-no` с `target=_blank`, щелчок по строке — `window.open(tlHref(t))`
  (`?client&entity&tender`). `document.title` — номер · оборудование. Телефон — строки карточками (CSS grid-areas).
  Выпадающий список `#tender` остался на странице конкурса (смена в этой же вкладке).
  0.13.1 (пользователь: «жму на конкурс — карточка не открывается»: новая вкладка ~12 с читала весь реестр, 940
  записей, и была пустой): `loadClients` при `?tender=` — `openFast()` → `GET /api/clients/{id}/tender-brief?id=`
  (одна запись NocoDB или папка, `_noco_item` / `_folder_item` — те же строки, что в списке) → `loadTender()`, список
  грузится одновременно; `state.opened` — какой конкурс загружен, `loadTenders` не грузит его второй раз (кроме
  `state.fresh`); `#opening` — «Открываю конкурс…». `NocoClient.records`: все страницы через одно соединение
  `httpx.AsyncClient` (`_get(..., http)`; новое соединение через прокси — ~1 с на запрос: 940 записей 14 с → 5 с),
  `PARALLEL_PAGES` = 1 (NocoDB на 10 запросов сразу отвечает 429 — проверено), шаг — сколько NocoDB реально отдал,
  дочитывание без totalRows, без повторов по Id; `_get` на 429 ждёт 1,5 / 3 / 4,5 с и повторяет (`RETRY_429`);
  `cache=True` — список реестра в `_cache` на `CACHE_SECONDS` (fresh — заново). «/» и «/user» —
  `_page_html()`: `app.js` / `app.css` с `?v=<версия>`, `Cache-Control: no-cache`.
- `app/tender_list.py` (0.13.0) — короткие поля для панели «Конкурсы» из развёрнутых полей реестра (только разбор
  текста, без NocoDB).

## Ранние вкладки-переходы (0.6.5) и кнопка «наверх» — вкладки устарели, кнопка действует

- Вкладки-переходы (0.6.5, панель — 0.6.6): `#jump-wrap` (подложка цвета фона, `position: sticky`, `top:
  var(--topbar-h)`) с `nav#jump-nav.panel` — панель под выбором конкурса, при прокрутке прилипает к шапке (класс
  `stuck` — тень). Ссылки `.jump[data-target]` на разделы `main > section` («top» — к началу страницы). Блок
  `jumpNav()` в конце `app.js`: `--topbar-h` и `--sticky-h` (шапка + панель) — из ResizeObserver; прокрутка
  `scrollIntoView` (отступ — `scroll-margin-top`), подсветка по положению (`pinned` — после нажатия, до прокрутки
  пользователем), вкладка скрыта вместе с разделом (MutationObserver на `hidden`), адрес не меняется. Телефон
  (≤ 760px): вкладки переносятся в две строки, шапка — `flex-wrap`. Не путать с `.tabs/.tab` внутри «Анализа».
  Решение пользователя 03.11.2026: страница одна, вкладки только переходят к разделам — не делать отдельные страницы;
  вкладки — заметной панелью, как блоки с данными.
  Кнопка «наверх» (0.17.3, бэклог Notion, пункт 1): `#to-top` (index.html, после `main`), `.to-top` в app.css —
  fixed справа внизу (z-index 40, под `.toast`), появляется при `scrollY` ≥ max(400, 0,8 экрана), блок `toTop()`
  после `jumpNav()` в app.js; `main` снизу 84px (телефон — 68px), чтобы кнопка не закрывала «Спросить» в чате.
