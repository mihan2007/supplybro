"""
Анализ документов конкурса — задача «analyze» обработчика (worker).

Порядок (его задаёт код, модель отвечает только на узкие вопросы по схеме):
  1. Тексты документов из этапа 1 (без скрытого текста) — одним блоком, кэшируется у Anthropic.
  2. Параллельно со всем остальным: DeepSeek независимо цитирует критичные поля (как Шаг 5.2 старого проекта).
  3. Перечень позиций (основной источник + другие документы с теми же позициями) — первым вызовом
     (он же записывает документы в кэш); затем параллельно: поля блоков 1–7 (три вызова по группам полей,
     у каждого поля «значение + цитата + документ») и подробности позиций частями по 10
     (ТЗ, ошибки ТЗ, марка, производитель, аналог, НМЦК).
  5. Проверки кодом: цитаты найдены в документах; нумерация 1…N без пропусков; все позиции разобраны;
     количество совпадает между документами; сумма НМЦК по позициям к НМЦ.
  6. Сверка критичных полей с версией DeepSeek → ✅ совпало / 🔺 расхождение (наше подтверждено) / исправлено.
  7. Сохранение: AIExtract/analysis.json, Позиции_и_цены.xlsx, XML; затем запись в NocoDB (app/analysis/noco_write.py).
"""

from __future__ import annotations

import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .. import db
from . import prompts
from .corpus import QuoteIndex, build_documents_block, corpus_hash, load_docs
from .fields import COMPUTED, CRITICAL, DISPLAY_ORDER, GROUPS, NOT_SPECIFIED, label_of
from .llm import Claude, DeepSeek, LLMError, Usage
from .spec_xlsx import write_spec_xlsx

from tools import deepseek_extract, save_tender  # копии скриптов старого проекта, используются как библиотека
from tools.nocodb_client import parse_ru_number

try:
    from zoneinfo import ZoneInfo
    MSK = ZoneInfo("Europe/Moscow")
except Exception:  # в образе может не быть базы часовых поясов
    MSK = timezone(timedelta(hours=3), "MSK")
OUT_DIR = Path("AIExtract")
RESULT_FILE = OUT_DIR / "analysis.json"
SPEC_FILE = "Позиции_и_цены.xlsx"
SPEC_FILE_ALT = "Позиции_и_цены (SupplyBro).xlsx"   # если файл агента уже есть — его не трогаем

CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5-5")
CLAUDE_EFFORT = os.environ.get("CLAUDE_EFFORT", "medium")
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-flash")
CORPUS_LIMIT = int(os.environ.get("ANALYSIS_MAX_CHARS", "900000"))
DEEPSEEK_LIMIT = int(os.environ.get("DEEPSEEK_MAX_CHARS", "900000"))
CHUNK = int(os.environ.get("ANALYSIS_POSITIONS_PER_CALL", "10"))
PARALLEL = int(os.environ.get("ANALYSIS_PARALLEL_CALLS", "6"))
DEEPSEEK_MAX_PRIORITY = 6   # формы заявки и анкеты (приоритет 7) DeepSeek не отправляются — там нет критичных полей


class AnalysisError(RuntimeError):
    pass


def result_path(folder: Path) -> Path:
    return folder / RESULT_FILE


def load_result(folder: Path) -> dict | None:
    p = result_path(folder)
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def save_result(folder: Path, result: dict) -> None:
    (folder / OUT_DIR).mkdir(parents=True, exist_ok=True)
    tmp = result_path(folder).with_suffix(".tmp")
    tmp.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(result_path(folder))


# ---------- мелочи ----------

def _plural(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def _num(text) -> float | None:
    return parse_ru_number(text) if str(text or "").strip() else None


def _fmt_qty(value: float | None, raw: str) -> str:
    if value is None:
        return str(raw or "").strip()
    return str(int(value)) if float(value).is_integer() else f"{value:.3f}".rstrip("0").replace(".", ",")


def _unit(text: str) -> str:
    return str(text or "").strip().rstrip(".").lower()


def _clean_n(n) -> str:
    return str(n or "").strip().rstrip(".")


def _n_int(n) -> int | None:
    m = re.match(r"^\s*(\d+)", str(n or ""))
    return int(m.group(1)) if m else None


def _money_values(text: str) -> list[float]:
    """Все суммы вида 1234567.89 из текста поля «Начальная цена»."""
    return [float(m) for m in re.findall(r"(?<![\d.])(\d{3,}\.\d{2})(?![\d])", str(text or ""))]


# ---------- шаги ----------

def _deepseek(documents_text: str, usage: Usage) -> dict:
    ds = DeepSeek(DEEPSEEK_MODEL, usage)
    return ds.json_call(system=deepseek_extract.SYSTEM_PROMPT,
                        user=deepseek_extract.build_user_prompt(documents_text[:DEEPSEEK_LIMIT]),
                        label="DeepSeek: критичные поля")


def _group(claude: Claude, docs_block: str, today: str, group) -> dict:
    """Поля одной группы блоков 1–7."""
    title, keys = group
    ans = claude.json_call(system=prompts.SYSTEM, documents=docs_block, prompt=prompts.group_prompt(keys, today),
                           schema=prompts.group_schema(keys), label=f"Поля: {title}", max_tokens=16000)
    return {k: ans.get(k) or {"value": NOT_SPECIFIED, "quote": "", "file": ""} for k in keys}


def _inventory(claude: Claude, docs_block: str, today: str) -> dict:
    return claude.json_call(system=prompts.SYSTEM, documents=docs_block,
                            prompt=prompts.INVENTORY_PROMPT.format(today=today),
                            schema=prompts.INVENTORY_SCHEMA, label="Позиции: перечень", max_tokens=48000)


def _details(claude: Claude, docs_block: str, today: str, main_source: str, part: list[dict], label: str) -> list[dict]:
    items = "\n".join(f"{p['n']}. {p['name']} — {p['qty']} {p['unit']}" for p in part)
    ans = claude.json_call(system=prompts.SYSTEM, documents=docs_block,
                           prompt=prompts.DETAIL_PROMPT.format(today=today, main_source=main_source, items=items),
                           schema=prompts.DETAIL_SCHEMA, label=label, max_tokens=40000)
    return ans.get("positions") or []


def _compare(claude: Claude, docs_block: str, today: str, ours: dict, theirs: dict) -> dict:
    keys = [k for k in CRITICAL if k in ours]
    items = []
    for k in keys:
        items.append(f"### {k} — «{label_of(k)[1]}»\nНаше значение: {ours[k]}\n"
                     f"Версия другой модели: {theirs.get(k) or 'не найдено в переданном тексте'}")
    return claude.json_call(system=prompts.SYSTEM, documents=docs_block,
                            prompt=prompts.COMPARE_PROMPT.format(today=today, items="\n\n".join(items)),
                            schema=prompts.compare_schema(keys), label="Перепроверка критичных полей",
                            max_tokens=16000)


# ---------- проверки кодом ----------

def _check_positions(inv: dict, details: dict[str, dict], start_price: str) -> tuple[list[dict], list[str]]:
    """Собирает позиции и проверяет полноту. Возвращает (строки, замечания)."""
    checks = []
    main = inv.get("positions") or []
    total = inv.get("total_positions")
    if isinstance(total, int) and total != len(main):
        checks.append(f"⚠ В основном перечне указано {total} поз., выписано {len(main)}.")
    nums = [_n_int(p.get("n")) for p in main]
    if main and all(n is not None for n in nums):
        expected = list(range(nums[0], nums[0] + len(nums)))
        if nums != expected:
            missing = sorted(set(expected) - set(nums))
            dup = sorted({n for n in nums if nums.count(n) > 1})
            msg = "⚠ Нумерация позиций не подряд"
            if missing:
                msg += f"; пропущены № {', '.join(map(str, missing[:20]))}"
            if dup:
                msg += f"; повторяются № {', '.join(map(str, dup[:20]))}"
            checks.append(msg + ".")
        else:
            checks.append(f"✅ Нумерация позиций {nums[0]}…{nums[-1]} без пропусков ({len(nums)} поз.).")
    elif main:
        checks.append("⚠ Номера позиций не числовые — нумерацию проверить не удалось.")

    # Сверка количества между документами (Шаг 5.1a).
    discrepancies: dict[str, list[str]] = {}
    main_src = inv.get("main_source") or "основной перечень"
    main_by_n = {_clean_n(p.get("n")): p for p in main}
    for src in inv.get("other_sources") or []:
        name = src.get("source") or "другой документ"
        other = {_clean_n(p.get("n")): p for p in src.get("positions") or []}
        diffs = 0
        for n, p in main_by_n.items():
            o = other.get(n)
            if not o:
                discrepancies.setdefault(n, []).append(f"⚠ Расхождение: позиции нет в документе «{name}»")
                diffs += 1
                continue
            a, b = _num(p.get("qty")), _num(o.get("qty"))
            if a is not None and b is not None and abs(a - b) > 1e-6:
                discrepancies.setdefault(n, []).append(
                    f"⚠ Расхождение: {main_src} — {_fmt_qty(a, p.get('qty'))} {_unit(p.get('unit'))}, "
                    f"{name} — {_fmt_qty(b, o.get('qty'))} {_unit(o.get('unit'))}")
                diffs += 1
        extra = [n for n in other if n not in main_by_n]
        if extra:
            checks.append(f"⚠ В документе «{name}» есть позиции, которых нет в основном перечне: № {', '.join(extra[:20])}.")
        checks.append(f"✅ Состав и количество совпадают с документом «{name}»." if not diffs and not extra
                      else f"⚠ Расхождения с документом «{name}»: {diffs} поз. (см. «Примечание» позиций).")

    rows, no_detail = [], []
    for p in main:
        n = _clean_n(p.get("n"))
        d = details.get(n)
        if d is None:
            no_detail.append(n)
            d = {}
        qty = _num(p.get("qty"))
        note = "; ".join(x for x in [str(d.get("note") or "").strip(), *discrepancies.get(n, [])] if x)
        rows.append({
            "№": n,
            "Наименование позиции": (d.get("name") or p.get("name") or "").strip(),
            "Техническое задание": (d.get("tz") or "").strip(),
            "Ошибки в документации": (d.get("doc_errors") or "").strip(),
            "Марка/модель": (d.get("brand_model") or "").strip(),
            "Производитель": (d.get("manufacturer") or "").strip(),
            "Страна происхождения": (d.get("country") or "").strip(),
            "Аналог допускается": (d.get("analog_allowed") or "").strip(),
            "Аналог (статус)": d.get("analog_status") or "Уточни",
            "Кол-во": qty if qty is not None else (p.get("qty") or ""),
            "Ед. изм.": (p.get("unit") or "").strip(),
            "НМЦК за позицию": (d.get("nmck_position") or "").strip() or NOT_SPECIFIED,
            "Стоимость НМЦ": (d.get("nmc_unit") or "").strip(),
            "Примечание": note,
        })
    if no_detail:
        checks.append(f"⚠ Не удалось разобрать подробно позиции № {', '.join(no_detail[:30])}.")

    # Сумма НМЦК по позициям к НМЦ.
    sums = [parse_ru_number(re.sub(r"\(.*?\)", "", str(r["НМЦК за позицию"]))) for r in rows]
    if rows and all(s is not None for s in sums):
        total_sum = round(sum(sums), 2)
        prices = _money_values(start_price)
        if prices:
            close = [p for p in prices if p and abs(total_sum - p) / p <= 0.005]
            fmt = f"{total_sum:,.2f}".replace(",", " ").replace(".", ",")
            checks.append(f"✅ Сумма НМЦК по позициям {fmt} совпадает с начальной ценой." if close
                          else f"⚠ Сумма НМЦК по позициям {fmt} не совпадает с начальной ценой ({start_price}).")
    elif rows and any(s is not None for s in sums):
        checks.append("⚠ НМЦК указана не у всех позиций — сумму с НМЦ сравнить нельзя.")
    return rows, checks


def _compose_product(title: str, rows: list[dict]) -> str:
    title = (title or "").strip()
    if not rows:
        return title or NOT_SPECIFIED
    if len(rows) == 1:
        return rows[0]["Наименование позиции"] or title
    lines = []
    for r in rows:
        qty = r["Кол-во"]
        q = _fmt_qty(qty if isinstance(qty, (int, float)) else _num(qty), str(qty))
        lines.append(f"{r['№']}) {r['Наименование позиции']} - {q} {_unit(r['Ед. изм.'])}".rstrip())
    head = f"{title}\n\n" if title and title != NOT_SPECIFIED else ""
    return head + "Список позиций:\n" + "\n".join(lines)


def _compose_quantity(rows: list[dict], source: str) -> str:
    if not rows:
        return NOT_SPECIFIED
    n = len(rows)
    text = f"{n} {_plural(n, 'позиция', 'позиции', 'позиций')}"
    units = {_unit(r["Ед. изм."]) for r in rows}
    qtys = [r["Кол-во"] for r in rows]
    if len(units) == 1 and all(isinstance(q, (int, float)) for q in qtys):
        text += f", всего {_fmt_qty(sum(qtys), '')} {units.pop()}"
    elif len(units) > 1:
        text += f" (единицы измерения разные: {', '.join(sorted(u for u in units if u))})"
    return text + (f" ({source})" if source else "")


def _apply_markers(values: dict, verdicts: dict) -> dict[str, dict]:
    """✅ / 🔺 / исправление по итогам сверки с DeepSeek. Возвращает сведения о проверке по полям."""
    info = {}
    for k, v in (verdicts or {}).items():
        if k not in values or not isinstance(v, dict):
            continue
        verdict = v.get("verdict")
        final = (v.get("final_value") or "").strip() or values[k]
        their = (v.get("their_version") or "").strip()
        before = values[k]
        if k == "quantity":
            final = before  # количество собрано кодом из позиций — не переписываем, только отмечаем
        if verdict == "match":
            values[k] = f"✅ {final}"
        elif verdict == "ours_confirmed" or (verdict == "theirs_correct" and k == "quantity"):
            values[k] = (f"🔺 {final} (расхождение: по версии DeepSeek — «{their[:300]}»; проверено повторно "
                         f"по исходнику, оставлено как у Claude)")
        else:
            values[k] = final
        info[k] = {"verdict": verdict, "before": before, "their_version": their, "comment": v.get("comment") or ""}
    return info


# ---------- главная функция ----------

def analyze(client_id: str, folder_name: str, folder: Path, progress) -> tuple[str, dict]:
    """Анализ документов конкурса. Возвращает (итог одной строкой, результат)."""
    started = time.time()
    today = datetime.now(MSK).strftime("%d.%m.%Y")
    docs = load_docs(client_id, folder_name, folder)
    if not docs:
        raise AnalysisError("нет документов с извлечённым текстом — сначала загрузите и обработайте документы")
    docs_block, used, skipped = build_documents_block(docs, CORPUS_LIMIT)
    usage = Usage()
    claude = Claude(CLAUDE_MODEL, usage, CLAUDE_EFFORT)
    checks: list[str] = []
    if skipped:
        checks.append("⚠ Не поместились в анализ (слишком большой объём): " + ", ".join(d.base for d in skipped))

    pool = ThreadPoolExecutor(max_workers=PARALLEL + 1)
    ds_docs = [d for d in used if d.priority <= DEEPSEEK_MAX_PRIORITY] or used
    ds_block, _, _ = build_documents_block(ds_docs, DEEPSEEK_LIMIT)
    deepseek_future = pool.submit(_deepseek, ds_block, usage)

    try:
        # 1. Перечень позиций — первым вызовом: он записывает документы в кэш, остальные вызовы читают из кэша.
        progress("Перечень позиций (документы передаются модели)", 0.05)
        inv = _inventory(claude, docs_block, today)
        main = [p for p in inv.get("positions") or [] if str(p.get("name") or "").strip()]
        inv["positions"] = main

        # 2. Параллельно: поля блоков 1–7 (три группы) и позиции подробно частями.
        progress(f"Поля конкурса и позиции подробно (0 из {len(main)})", 0.2)
        group_futs = [pool.submit(_group, claude, docs_block, today, g) for g in GROUPS]
        parts = [main[i:i + CHUNK] for i in range(0, len(main), CHUNK)]
        part_futs = [(pool.submit(_details, claude, docs_block, today, inv.get("main_source") or "", part,
                                  f"Позиции {part[0]['n']}–{part[-1]['n']}"), part) for part in parts]
        details: dict[str, dict] = {}
        done = 0
        for fut, part in part_futs:
            for d in fut.result():
                details[_clean_n(d.get("n"))] = d
            done += len(part)
            progress(f"Позиции подробно: {done} из {len(main)}", 0.2 + 0.5 * done / max(len(main), 1))
        raw_fields: dict[str, dict] = {}
        for f in group_futs:
            raw_fields.update(f.result())
        # Повтор для позиций, которых не оказалось в ответах (один раз).
        missing = [p for p in main if _clean_n(p.get("n")) not in details]
        if missing:
            progress(f"Повтор для {len(missing)} поз.", 0.72)
            for i in range(0, len(missing), CHUNK):
                part = missing[i:i + CHUNK]
                for d in _details(claude, docs_block, today, inv.get("main_source") or "", part,
                                  f"Позиции (повтор) {part[0]['n']}–{part[-1]['n']}"):
                    details[_clean_n(d.get("n"))] = d

        values = {k: (v.get("value") or "").strip() or NOT_SPECIFIED for k, v in raw_fields.items()}
        rows, pos_checks = _check_positions(inv, details, values.get("start_price", ""))
        checks.extend(pos_checks)
        general = [ln.strip() for ln in str(inv.get("tz_general") or "").splitlines() if ln.strip()]
        if general:
            checks.append("⚠ Общие замечания по ТЗ: " + "; ".join(general))
        values["product_name"] = _compose_product(values.pop("product_title", ""), rows)
        values["quantity"] = _compose_quantity(rows, inv.get("main_source") or "")

        # 3. Перепроверка критичных полей с DeepSeek.
        progress("Перепроверка критичных полей (DeepSeek)", 0.76)
        verification = {}
        try:
            theirs = deepseek_future.result()
            ds_fields = theirs if isinstance(theirs, dict) else {}
            verdicts = _compare(claude, docs_block, today, {k: values[k] for k in CRITICAL if k in values}, ds_fields)
            verification = _apply_markers(values, verdicts)
            for k, v in verification.items():
                if v["verdict"] == "theirs_correct":
                    checks.append(f"⚠ «{label_of(k)[1]}» исправлено после перепроверки: было «{v['before'][:150]}».")
                elif v["verdict"] == "ours_confirmed":
                    checks.append(f"🔺 «{label_of(k)[1]}»: расхождение с DeepSeek, оставлено значение Claude.")
        except LLMError as exc:
            checks.append(f"⚠ Перепроверка DeepSeek не выполнена: {exc}")
    finally:
        pool.shutdown(wait=False, cancel_futures=True)

    # 4. Проверка цитат кодом.
    progress("Проверка цитат", 0.8)
    qi = QuoteIndex(used)
    field_rows = []
    bad_quotes = []
    for key in DISPLAY_ORDER:
        block, label = label_of(key)
        raw = raw_fields.get(key) or {}
        if key in COMPUTED:
            q_status, q_doc, quote, file = "computed", None, "", ""
        else:
            quote, file = (raw.get("quote") or "").strip(), (raw.get("file") or "").strip()
            q_status, q_doc = qi.find(quote, file)
            plain = re.sub(r"^(✅|🔺)\s*", "", values.get(key, ""))
            if q_status == "empty" and (plain.startswith(NOT_SPECIFIED) or plain in ("—", "Не предусмотрено")):
                q_status = "none"
            if q_status in ("not_found", "empty"):
                bad_quotes.append(label)
        field_rows.append({"key": key, "block": block, "label": label, "value": values.get(key, NOT_SPECIFIED),
                           "quote": quote, "file": q_doc or file, "quote_status": q_status,
                           "check": verification.get(key)})
    if bad_quotes:
        checks.append("⚠ Цитата не найдена в документах (проверить вручную): " + ", ".join(bad_quotes))
    else:
        checks.append("✅ Цитаты всех заполненных полей найдены в документах.")

    # 5. Сохранение файлов.
    progress("Сохранение файлов", 0.84)
    internal = _internal_number(folder_name)
    flat = {k: values.get(k, NOT_SPECIFIED) for k in DISPLAY_ORDER}
    flat["internal_number"] = internal
    flat["source_file"] = " + ".join(d.base for d in used)
    flat["extracted_date"] = datetime.now(MSK).strftime("%Y-%m-%d")
    previous = load_result(folder) or {}
    outputs = _save_files(folder, flat, rows, previous.get("outputs") or {})

    result = {
        "version": 1,
        "analyzed_at": time.time(),
        "analyzed_at_text": datetime.now(MSK).strftime("%d.%m.%Y %H:%M"),
        "seconds": round(time.time() - started),
        "models": {"claude": CLAUDE_MODEL, "effort": CLAUDE_EFFORT, "deepseek": DEEPSEEK_MODEL},
        "corpus_hash": corpus_hash(docs),
        "documents": [{"name": d.name, "chars": len(d.text), "used": d in used} for d in docs],
        "internal_number": internal,
        "flat": flat,
        "fields": field_rows,
        "positions": rows,
        "inventory": {"main_source": inv.get("main_source"), "total_positions": inv.get("total_positions"),
                      "other_sources": [s.get("source") for s in inv.get("other_sources") or []],
                      "tz_general": inv.get("tz_general") or "", "notes": inv.get("notes") or ""},
        "checks": checks,
        "usage": {"totals": usage.totals(), "calls": usage.calls},
        "outputs": outputs,
        "noco": previous.get("noco") if previous.get("internal_number") == internal else None,
    }
    save_result(folder, result)
    summary = (f"Поля: {sum(1 for r in field_rows if not r['value'].startswith(NOT_SPECIFIED))} из {len(field_rows)} заполнено; "
               f"позиций: {len(rows)}; замечаний: {sum(1 for c in checks if not c.startswith('✅'))}")
    return summary, result


def _internal_number(folder_name: str) -> str:
    from ..tenders import norm_number
    return norm_number(folder_name) or folder_name


def _save_files(folder: Path, flat: dict, rows: list[dict], prev_outputs: dict) -> dict:
    out_dir = folder / OUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    outputs = {}
    # Таблица позиций: файл агента (если он уже есть и сделан не сайтом) не перезаписываем.
    spec_name = SPEC_FILE
    if (out_dir / SPEC_FILE).exists() and prev_outputs.get("spec_xlsx") != SPEC_FILE:
        spec_name = SPEC_FILE_ALT
    write_spec_xlsx(out_dir / spec_name, rows)
    outputs["spec_xlsx"] = spec_name
    # XML — как у агента (tools/save_tender.py).
    fields = dict(flat)
    save_tender.apply_dash_placeholders(fields)
    fields["start_price"] = save_tender.normalize_number(fields.get("start_price"))
    fields["source_file"] = save_tender.format_source_files(fields.get("source_file"))
    xml = save_tender.build_xml(fields, fields["source_file"], flat["extracted_date"])
    ok, err = save_tender.validate_xml(xml)
    if ok:
        name = save_tender.build_filename(fields, flat["extracted_date"])
        (out_dir / name).write_text(xml, encoding="utf-8")
        outputs["xml"] = name
    else:
        outputs["xml_error"] = err
    return outputs
