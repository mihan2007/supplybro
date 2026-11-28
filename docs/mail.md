# Почта: ящики, запросы цен, ответы, переписка

Подробное описание для Claude. Перенесено из `CLAUDE.md` 28.11.2026 (0.45.5) без пересказа. Пункты с номером версии — как было сделано тогда; если два пункта противоречат друг другу, верен более поздний. Отдельной вкладки «Письма» с 0.43.0 нет: раздел запросов — в «Закупке» (см. interface.md).

## Почтовые ящики

- Почта закупщика (0.27.0, пользователь 17.11.2026: «формировать письма поставщикам с запросами цен… отправлять с
  почтового ящика; свой ящик у каждого закупщика, может выбирать»; этап 2 — сервер читает ответы, привязка к конкурсу).
  `app/mail.py` — `mailboxes` (owner = логин, пароль — Fernet; ключ — `MAIL_KEY_FILE` в secrets/ → `/run/secrets/mail_key`, создаёт run.sh;
  запасной `DATA_DIR/mail.key` только для расшифровки старых, `MultiFernet`; странице только `has_password`), `PRESETS` по домену, `check_box` (SMTP + IMAP, `_explain` — причина по-русски), `send_many` (одно
  соединение, пауза 1 с, `_append_sent` — копия в «Отправленные» по IMAP, кроме Gmail). Адреса `/api/me/mailboxes`
  (GET, POST сохранить+проверить, `/preset`, `/{id}/check|default|delete`); проходная: `/api/me/` изменять — право
  `purchase`. `app/supplier_requests.py` — `supplier_emails` (client, base, Id поставщика → адрес), `mail_requests`
  (журнал: owner, ящик, кому, позиции [№], subject, message_id, tag `SB-<K934>-<id>`, status sending/sent/error);
  `drafts` (по `supplier_brands.for_spec(limit=None)`; «Кому» — адрес с сайта, иначе все почты из NocoDB (0.31.2), «Не работаем» без галочки, письмо «другому адресату»; `intro`
  / `outro_xlsx` / `outro_plain` — страница собирает текст при смене позиций), `send` (позиции — только из
  спецификации, `xlsx_bytes` — «Запрос цен К934.xlsx», заголовок `X-SupplyBro-Tag`). Адреса
  `GET …/tenders/{id}/requests`, `POST …/requests/send` и `…/requests/email` (`RULES` — purchase; раздел — prices).
  Страница: `#requests` (после спецификации, вкладка «Запросы»), `loadRequests` / `renderRequests` / `letterCard`,
  `#mail-dlg` («✉ Почтовые ящики» в шапке у кого есть purchase, «✉ Мои ящики» в разделе). Сеть: SMTP / IMAP напрямую
  (прокси Windows — HTTP, почта через него не ходит).
- Общие почтовые ящики (0.36.0; пользователь 25.11.2026: «у админа и у закупщика — одни и те же почты»):
  `users.mail_owner` (логин; '' — свои), админ-панель `#ed-mail-owner` (`fillMailOwner`), PATCH / создание —
  `admin._mail_owner_scope`, `auth._check_mail_owner` (нет цепочек, не сам себе). `mail.owner_key()` возвращает
  mail_owner — все адреса `/api/me/*`, отправка, ответы, переписка работают с общими ящиками. При назначении —
  `mail.merge_owner(src, dst)`: ящики → dst (двойник по адресу удаляется), `mail_inbox` / `mail_requests` owner → dst.
  `/api/me/mailboxes` → `shared_with`, в окне ящиков `#mail-shared`.

## Ответы поставщиков

- Ответы поставщиков (0.28.0): `app/mail_inbox.py`, поток `mail` в `worker.main` (`poller` → `due_boxes` →
  `poll_box`: INBOX readonly, BODY.PEEK, UID после `mail_inbox_state.last_uid`, первый раз — SINCE первого запроса с
  ящика). `match`: reply (refs ∈ `mail_requests.message_id`) → tag (`SB-<K934>-<id>`, `latin_tag` сверяется) →
  sender (адрес / не общий домен, один конкурс за `MATCH_DAYS`) → content (номер конкурса в тексте). Нет —
  `relevant` (адресат запросов или вложение-документ + `PRICE_WORDS`) → `mail_inbox` status unmatched + исходник
  `DATA_DIR/mail_inbox/<id>.eml`; иначе не хранится. `deliver`: `price_dir/Почта/<дата> <поставщик>/` (Письмо.txt —
  `own_text` без цитаты, письмо.eml, вложения) → `pricelists.add_files` + поле `mail` у записей → задача
  `pricelist` от имени владельца ящика. Страница: `pricelistCard` — «📧 Письмо поставщика», `#mail-inbox` в окне
  ящиков (`loadInbox` / `renderInbox`), `#rq-inbox` «Ответы поставщиков», колонка «Ответ» в журнале, `#rq-unmatched`.

## Переписка

- Переписка с поставщиками (0.32.0, `app/supplier_threads.py`; пользователь 24.11.2026: «поставщики задали вопросы —
  мини-диалоговое окно с каждым, не перегружая интерфейс»). Нить = поставщик в конкурсе (`request_key`: «s<Id>» /
  «a<адрес>»; письмо — к нити своего `request_id`, иначе по адресу отправителя). `threads` (только ответившие,
  `waiting` — последнее письмо поставщика по времени, когда его увидел сайт, позже нашего и «Ответ не нужен»
  `mail_thread_state`), `messages` (текст входящих — `mail_inbox.body`, у прежних — «Письмо.txt»), `reply` (ящик
  запроса, только владелец; «Re:» + метка, In-Reply-To / References, цитата; строка `mail_requests` kind='reply' —
  ответ на неё привязывается признаком reply), `mark_done`. Страница: `#rq-threads-box` вверху «Запросов»
  (`renderThreads`, из `renderJournal`), окно `#th-dlg` (`openThread` / `renderThread` / `thMsg`), `thOpenBtn` —
  в «Ответах поставщиков», журнале и `wbDetail`. 0.32.1: письма без найденных цен в «Документах поставщиков» —
  строкой «Письма без цен» со ссылками на переписку (`plQuiet` / `quietMailRow`); картинки в тексте письма до 40 КБ
  (`mail_inbox.INLINE_IMAGE_KB`) в разбор не идут.

## Шаблоны и фильтр по брендам

- Шаблоны писем поставщикам (0.45.0, `app/mail_templates.py`): общие на клиента (`mail_templates`, история —
  `mail_template_history`), «Стандартный» — прежнее письмо 0.44.0; метки {приветствие} {поставщик} {позиции}
  {номер} {предмет} {вложение} {подпись} подставляет страница (`tplFill`, `rqApplyTpl`); шаблон и вложения — у
  каждого письма (`letterTpl`, `L.tpl` / `L.xlsx` / `L.req`, 0.45.2), `#rq-tpl` — «для всех писем»,
  редактор — `#tpl-dlg` (право purchase); «приложить реквизиты» — карточка materials.requisites юр. лица.
- Фильтр запросов по брендам (0.33.0): `drafts` отдаёт `pos_brands`; страница — `#rq-brands` (`renderRqBrands`),
  `rq.brands` (Set), `applyRqBrands` сужает `L.positions` из `L._all` (у «другому адресату» — из всех позиций),
  `rqVisible` — скрытые не показываются, не отправляются и не считаются; `wbAsk` сбрасывает фильтр. С 0.33.1
  черновики не отмечены заранее (`checked: False`).

## Вкладка «Письма» (0.35.0, с 0.43.0 — раздел «Закупки»)

- Вкладка «Письма» (0.35.0; пользователь 25.11.2026): `.jump[data-tab="mail"]` + `#mail-badge` (ждут ответа),
  `TABS` с "mail", CSS `main[data-tab="mail"]`; `#requests` — `data-group="mail pos"` (из «Закупки» убран).
  Лениво: `ensureRequests()` (из `setTab` и после `loadTender`) зовёт `loadRequests`, только если вкладка mail / all /
  pos, один раз на конкурс (`rq.for`; при смене конкурса `rq.d = null`). До открытия — `loadMailSummary()` →
  `GET /api/clients/{id}/requests/summary?internal=` (`main.api_requests_summary`: журнал, ответы, нити — без NocoDB;
  BLOCK_PATHS `^/requests` → prices) → `stg.req`, `rq.threads` (thFind берёт и их), значок. Внутри: переписка →
  `#rq-pos-box` «Позиции и поставщики» (`rqPosRows` / `renderRqPos`: L._all писем с supplier_id, журнал sent без
  reply, ответы по request_id или нити, цены `of.d.cells`; отборы `RQ_POS_FILTERS`, поиск, `rqPos.sel` → `wbAsk`,
  показ `RQ_POS_SHOW` = 150) → `details#rq-new` «Новый запрос цен» (карточки писем — только когда open; toggle →
  renderRequests) → `details#rq-inbox-box`, `#rq-journal-box`. `wbAsk` async: при необходимости грузит письма,
  раскрывает #rq-new, переходит во вкладку «Письма» (в виде «по позициям» — остаётся). Этап в шапке — «Письма».
