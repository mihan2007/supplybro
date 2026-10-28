"""
Обращения к моделям: Claude (Anthropic API) и DeepSeek.

- Ответ Claude — один JSON-объект; схема ответа — в тексте задания (после документов), код разбирает ответ,
  приводит типы и при сбое один раз переспрашивает. Инструментов у модели нет.
  (Не output_config.format: схема в нём — часть начала запроса, и при разных схемах кэш документов
  не срабатывал — проверено 27.10.2026: из 10 вызовов 9 записывали документы в кэш заново.)
- Текст документов передаётся одним блоком с пометкой кэша: первый вызов записывает его в кэш Anthropic,
  следующие вызовы того же анализа читают из кэша (в ~10 раз дешевле).
- Повторы при перегрузке / лимитах / обрывах связи.
- Учёт токенов и стоимости ($, app/analysis/pricing.py) по каждому вызову.
- Если на счёте Anthropic закончились деньги (BillingError) и FALLBACK_TO_DEEPSEEK=1 — анализ продолжается
  на DeepSeek (FALLBACK_DEEPSEEK_MODEL), чат отвечает через DeepSeek; на странице это отмечается.

Ключи — из файлов (CLAUDE_KEY_FILE_IN_CONTAINER / DEEPSEEK_KEY_FILE_IN_CONTAINER), в журнал не выводятся.
Прокси — из HTTPS_PROXY (на компьютере это прокси Windows).
"""

from __future__ import annotations

import json
import os
import random
import re
import threading
import time
from pathlib import Path

import httpx

from .pricing import cost

CLAUDE_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"

CLAUDE_KEY_FILE = os.environ.get("CLAUDE_KEY_FILE_IN_CONTAINER", "/run/secrets/claude")
DEEPSEEK_KEY_FILE = os.environ.get("DEEPSEEK_KEY_FILE_IN_CONTAINER", "/run/secrets/deepseek")

RETRY_STATUS = {408, 409, 425, 429, 500, 502, 503, 504, 529}
MAX_ATTEMPTS = 6
FALLBACK_TO_DEEPSEEK = os.environ.get("FALLBACK_TO_DEEPSEEK", "1").strip() not in ("0", "false", "no", "")
FALLBACK_DEEPSEEK_MODEL = os.environ.get("FALLBACK_DEEPSEEK_MODEL", "deepseek-flash")


class LLMError(RuntimeError):
    """Ошибка обращения к модели — текст показывается пользователю."""


class BillingError(LLMError):
    """На счёте закончились деньги — можно переключиться на другую модель."""


class TruncatedError(LLMError):
    """Ответ не поместился в max_tokens — задание можно разбить на части."""


def _is_billing(status: int, text: str) -> bool:
    low = text.lower()
    return "credit balance" in low or "insufficient balance" in low or status == 402


def _fail(status: int, text: str, what: str) -> LLMError:
    msg = explain(status, text, what)
    return BillingError(msg) if _is_billing(status, text) else LLMError(msg)


def read_key(path: str, what: str) -> str:
    p = Path(path)
    if not p.is_file():
        raise LLMError(f"нет файла с ключом {what}: {path} (см. .env и compose.yml)")
    key = p.read_text(encoding="utf-8").strip()
    if not key:
        raise LLMError(f"файл с ключом {what} пустой: {path}")
    return key


class Usage:
    """Токены по всем вызовам одного анализа (потокобезопасно)."""

    def __init__(self):
        self.calls: list[dict] = []
        self._lock = threading.Lock()

    def add(self, **entry) -> None:
        entry.setdefault("usd", cost(entry["model"], input=entry.get("input"), output=entry.get("output"),
                                     cache_write_5m=entry.get("cache_write_5m"), cache_write_1h=entry.get("cache_write_1h"),
                                     cache_read=entry.get("cache_read"), at=time.time()))
        with self._lock:
            self.calls.append(entry)

    def totals(self) -> dict:
        out: dict[str, dict] = {}
        for c in self.calls:
            m = out.setdefault(c["model"], {"calls": 0, "input": 0, "cache_write": 0, "cache_read": 0, "output": 0,
                                            "seconds": 0.0, "usd": 0.0})
            m["calls"] += 1
            for k in ("input", "cache_write", "cache_read", "output"):
                m[k] += int(c.get(k) or 0)
            m["seconds"] = round(m["seconds"] + float(c.get("seconds") or 0), 1)
            m["usd"] = round(m["usd"] + float(c.get("usd") or 0), 5)
        return out

    def total_usd(self) -> float:
        return round(sum(float(c.get("usd") or 0) for c in self.calls), 5)


def explain(status: int, text: str, what: str) -> str:
    """Понятный текст ошибки API."""
    if _is_billing(status, text):
        site = "console.anthropic.com → Plans & Billing" if what == "Claude" else "platform.deepseek.com → Top up"
        return f"на счёте {what} закончились деньги — пополните баланс ({site})"
    if status in (401, 403):
        return f"{what} не принял ключ ({status}): {text[:300]}"
    return f"{what} ответил {status}: {text[:300]}"


def _post(url: str, headers: dict, body: dict, timeout: float, what: str) -> dict:
    """POST с повторами. Возвращает JSON ответа или бросает LLMError с понятным текстом."""
    last = ""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            with httpx.Client(timeout=httpx.Timeout(timeout, connect=30), trust_env=True) as http:
                r = http.post(url, headers=headers, json=body)
        except httpx.HTTPError as exc:
            last = f"нет связи с {what} ({type(exc).__name__}: {exc})"
            if attempt == MAX_ATTEMPTS:
                raise LLMError(last + ". Проверьте интернет и прокси (OUTBOUND_PROXY в .env).") from exc
            time.sleep(min(60, 4 * attempt) + random.random())
            continue
        if r.status_code == 200:
            return r.json()
        text = r.text[:400]
        if r.status_code in (401, 402, 403) or _is_billing(r.status_code, text):
            raise _fail(r.status_code, text, what)
        if r.status_code in RETRY_STATUS and attempt < MAX_ATTEMPTS:
            wait = r.headers.get("retry-after")
            try:
                pause = float(wait) if wait else min(90, 5 * 2 ** (attempt - 1))
            except ValueError:
                pause = min(90, 5 * 2 ** (attempt - 1))
            last = f"{what} ответил {r.status_code}: {text}"
            time.sleep(min(pause, 120) + random.random())
            continue
        raise _fail(r.status_code, text, what)
    raise LLMError(last or f"{what}: не удалось получить ответ")


# ---------- потоковые ответы (чат) ----------

def _open_stream(http: httpx.Client, url: str, headers: dict, body: dict, what: str):
    """Открывает потоковый ответ; повторы — только пока ответ не начался (перегрузка, лимиты)."""
    for attempt in range(1, 4):
        try:
            resp = http.send(http.build_request("POST", url, headers=headers, json=body), stream=True)
        except httpx.HTTPError as exc:
            if attempt == 3:
                raise LLMError(f"нет связи с {what} ({type(exc).__name__}). Проверьте интернет и прокси.") from exc
            time.sleep(3 * attempt)
            continue
        if resp.status_code == 200:
            return resp
        text = resp.read().decode("utf-8", "replace")[:400]
        resp.close()
        if resp.status_code in RETRY_STATUS and attempt < 3 and not _is_billing(resp.status_code, text):
            time.sleep(5 * attempt)
            continue
        raise _fail(resp.status_code, text, what)
    raise LLMError(f"{what}: не удалось получить ответ")


def stream_claude(*, model: str, system: list[dict], messages: list[dict], max_tokens: int = 4000,
                  effort: str = "", timeout: float = 300):
    """Потоковый ответ Claude: выдаёт ('text', кусок) и в конце ('usage', {...})."""
    key = read_key(CLAUDE_KEY_FILE, "Claude")
    body = {"model": model, "max_tokens": max_tokens, "stream": True, "system": system, "messages": messages}
    if effort:
        body["output_config"] = {"effort": effort}
    headers = {"x-api-key": key, "anthropic-version": ANTHROPIC_VERSION, "content-type": "application/json"}
    usage: dict = {}
    with httpx.Client(timeout=httpx.Timeout(timeout, connect=30), trust_env=True) as http:
        resp = _open_stream(http, CLAUDE_URL, headers, body, "Claude")
        try:
            for line in resp.iter_lines():
                if not line.startswith("data:"):
                    continue
                try:
                    ev = json.loads(line[5:])
                except json.JSONDecodeError:
                    continue
                kind = ev.get("type")
                if kind == "message_start":
                    usage.update((ev.get("message") or {}).get("usage") or {})
                elif kind == "content_block_delta" and (ev.get("delta") or {}).get("type") == "text_delta":
                    yield "text", ev["delta"].get("text", "")
                elif kind == "message_delta":
                    usage.update(ev.get("usage") or {})
                    if (ev.get("delta") or {}).get("stop_reason") == "max_tokens":
                        yield "text", "\n\n[ответ обрезан — слишком длинный]"
                elif kind == "error":
                    err = ev.get("error") or {}
                    raise _fail(500, err.get("message") or str(err), "Claude")
        finally:
            resp.close()
    yield "usage", _claude_usage(model, usage)


def stream_deepseek(*, model: str, messages: list[dict], max_tokens: int = 4000, timeout: float = 300):
    """Потоковый ответ DeepSeek (кэш у DeepSeek включается сам): ('text', кусок), в конце ('usage', {...})."""
    key = read_key(DEEPSEEK_KEY_FILE, "DeepSeek")
    body = {"model": model, "messages": messages, "stream": True, "max_tokens": max_tokens, "temperature": 0.2,
            "stream_options": {"include_usage": True}}
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    usage: dict = {}
    with httpx.Client(timeout=httpx.Timeout(timeout, connect=30), trust_env=True) as http:
        resp = _open_stream(http, DEEPSEEK_URL, headers, body, "DeepSeek")
        try:
            for line in resp.iter_lines():
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    ev = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                if ev.get("usage"):
                    usage = ev["usage"]
                for ch in ev.get("choices") or []:
                    piece = (ch.get("delta") or {}).get("content")
                    if piece:
                        yield "text", piece
        finally:
            resp.close()
    out = {"model": model, "input": usage.get("prompt_tokens"), "cache_read": usage.get("prompt_cache_hit_tokens"),
           "output": usage.get("completion_tokens")}
    out["usd"] = cost(model, input=out["input"], output=out["output"], cache_read=out["cache_read"], at=time.time())
    yield "usage", out


def _claude_usage(model: str, u: dict) -> dict:
    """Токены вызова Claude, с разбивкой записи в кэш на 5 минут / 1 час, и стоимость."""
    cc = u.get("cache_creation") or {}
    w_total = int(u.get("cache_creation_input_tokens") or 0)
    w1 = int(cc.get("ephemeral_1h_input_tokens") or 0)
    w5 = int(cc.get("ephemeral_5m_input_tokens") or 0) or max(w_total - w1, 0)
    out = {"model": model, "input": u.get("input_tokens"), "cache_write": w_total, "cache_write_5m": w5,
           "cache_write_1h": w1, "cache_read": u.get("cache_read_input_tokens"), "output": u.get("output_tokens")}
    out["usd"] = cost(model, input=out["input"], output=out["output"], cache_write_5m=w5, cache_write_1h=w1,
                      cache_read=out["cache_read"], at=time.time())
    return out


def _extract_json(text: str):
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```(?:json)?\s*", "", t)
        t = re.sub(r"\s*```\s*$", "", t)
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        a, b = t.find("{"), t.rfind("}")
        if a >= 0 and b > a:
            return json.loads(t[a:b + 1])
        raise


def conform(value, schema: dict, path: str = "ответ"):
    """Приводит ответ модели к схеме: числа → строки, недопустимое значение списка → первое из enum-«запасных».
    Отсутствующее обязательное поле — ошибка (ValueError), тогда модель переспрашивается."""
    kind = schema.get("type")
    if kind == "object":
        if not isinstance(value, dict):
            raise ValueError(f"{path}: ожидался объект")
        out = {}
        for key, sub in schema.get("properties", {}).items():
            if key not in value:
                if key in schema.get("required", []):
                    raise ValueError(f"{path}: нет поля «{key}»")
                continue
            out[key] = conform(value[key], sub, f"{path}.{key}")
        return out
    if kind == "array":
        if value is None:
            return []
        if not isinstance(value, list):
            raise ValueError(f"{path}: ожидался список")
        return [conform(v, schema.get("items", {}), f"{path}[{i}]") for i, v in enumerate(value)]
    if kind == "integer":
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0
    if kind == "string":
        text = "" if value is None else (str(value) if not isinstance(value, (dict, list)) else json.dumps(value, ensure_ascii=False))
        enum = schema.get("enum")
        if enum and text not in enum:
            return schema.get("x-fallback", enum[-1])
        return text
    return value


class Claude:
    def __init__(self, model: str, usage: Usage, effort: str = ""):
        self.model = model
        self.usage = usage
        self.effort = effort
        self.key = read_key(CLAUDE_KEY_FILE, "Claude")
        # Если на счёте Anthropic закончились деньги — дальше все вызовы идут в DeepSeek (см. json_call).
        self.fallback: str = ""
        self.fallback_reason: str = ""
        self._lock = threading.Lock()
        # Кэш ответов задачи (app/jobctl.py): после паузы, отмены, ошибки или перезапуска обработчика те же вопросы
        # по тем же документам повторно не оплачиваются. None — без кэша.
        self.cache = None

    def _switch(self, reason: str) -> bool:
        if not FALLBACK_TO_DEEPSEEK:
            return False
        with self._lock:
            if not self.fallback:
                self.fallback, self.fallback_reason = FALLBACK_DEEPSEEK_MODEL, reason
        return True

    def _deepseek_json(self, system: str, documents: str | None, task: str, schema: dict, label: str,
                       max_tokens: int, timeout: float) -> dict:
        """Тот же вызов через DeepSeek (запасной путь). Документы — в системном сообщении: начало у всех вызовов
        одинаковое, и кэш DeepSeek срабатывает сам."""
        key = read_key(DEEPSEEK_KEY_FILE, "DeepSeek")
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        sys_text = system + ("\n\n" + documents if documents else "")
        for attempt in (1, 2):
            body = {"model": self.fallback, "temperature": 0, "max_tokens": min(max_tokens * 2, 64000),
                    "response_format": {"type": "json_object"},
                    "messages": [{"role": "system", "content": sys_text}, {"role": "user", "content": task}]}
            t0 = time.monotonic()
            data = _post(DEEPSEEK_URL, headers, body, timeout, "DeepSeek")
            u = data.get("usage") or {}
            self.usage.add(model=self.fallback, label=label + (" (повтор)" if attempt == 2 else ""),
                           input=u.get("prompt_tokens"), cache_read=u.get("prompt_cache_hit_tokens"),
                           output=u.get("completion_tokens"), seconds=round(time.monotonic() - t0, 1))
            if ((data.get("choices") or [{}])[0]).get("finish_reason") == "length":
                raise TruncatedError(f"{label}: ответ DeepSeek обрезан (не хватило max_tokens)")
            try:
                text = data["choices"][0]["message"]["content"]
                return conform(_extract_json(text), schema)
            except (KeyError, IndexError, ValueError, json.JSONDecodeError) as exc:
                if attempt == 2:
                    raise LLMError(f"{label}: ответ DeepSeek не по схеме ({exc})") from exc
                task += f"\n\nВНИМАНИЕ: предыдущий ответ не удалось разобрать ({exc}). Ответь строго одним JSON-объектом."
        raise LLMError(f"{label}: нет ответа")

    def json_call(self, *, system: str, documents: str | None, prompt: str, schema: dict, label: str,
                  max_tokens: int = 16000, timeout: float = 900) -> dict:
        """Один вызов Claude с ответом одним JSON-объектом по схеме.

        documents — текст документов (кэшируется: должен быть одинаковым у всех вызовов одного анализа).
        prompt — задание этого вызова (после документов, не кэшируется); схема ответа дописывается к нему.
        """
        task = (prompt + "\n\nФормат ответа: только один JSON-объект — без пояснений до и после, без ```. "
                "Схема (JSON Schema):\n" + json.dumps(schema, ensure_ascii=False))
        key = None
        if self.cache is not None:
            key = self.cache.key("claude", self.model, self.effort, system, documents, task, max_tokens)
            hit = self.cache.get(key)
            if hit is not None:
                self.usage.add(model=self.model, label=label + " (готовый ответ — не оплачен)", usd=0.0, cached=True)
                return hit
        if self.fallback:
            answer = self._deepseek_json(system, documents, task, schema, label, max_tokens, timeout)
        else:
            try:
                answer = self._claude_json(system, documents, task, schema, label, max_tokens, timeout)
            except BillingError as exc:
                if not self._switch(str(exc)):
                    raise
                answer = self._deepseek_json(system, documents, task, schema, label, max_tokens, timeout)
        if key:
            self.cache.put(key, answer)
        return answer

    def _claude_json(self, system: str, documents: str | None, task: str, schema: dict, label: str,
                     max_tokens: int, timeout: float) -> dict:
        headers = {"x-api-key": self.key, "anthropic-version": ANTHROPIC_VERSION, "content-type": "application/json"}
        for attempt in (1, 2):
            content = []
            if documents:
                content.append({"type": "text", "text": documents, "cache_control": {"type": "ephemeral"}})
            content.append({"type": "text", "text": task})
            body = {
                "model": self.model,
                "max_tokens": max_tokens,
                "system": [{"type": "text", "text": system}],
                "messages": [{"role": "user", "content": content}],
            }
            if self.effort:
                body["output_config"] = {"effort": self.effort}
            t0 = time.monotonic()
            data = _post(CLAUDE_URL, headers, body, timeout, "Claude")
            u = _claude_usage(self.model, data.get("usage") or {})
            u.pop("model")
            self.usage.add(model=self.model, label=label if attempt == 1 else label + " (повтор)",
                           seconds=round(time.monotonic() - t0, 1), stop=data.get("stop_reason"), **u)
            if data.get("stop_reason") == "max_tokens":
                raise TruncatedError(f"{label}: ответ модели обрезан (не хватило max_tokens={max_tokens})")
            if data.get("stop_reason") == "refusal":
                raise LLMError(f"{label}: модель отказалась отвечать")
            text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text").strip()
            try:
                return conform(_extract_json(text), schema)
            except (ValueError, json.JSONDecodeError) as exc:
                if attempt == 2:
                    raise LLMError(f"{label}: ответ модели не по схеме ({exc}): {text[:200]}") from exc
                task += (f"\n\nВНИМАНИЕ: предыдущий ответ не удалось разобрать ({exc}). "
                         "Ответь строго одним JSON-объектом по схеме, со всеми обязательными полями.")
        raise LLMError(f"{label}: нет ответа")


class DeepSeek:
    def __init__(self, model: str, usage: Usage):
        self.model = model
        self.usage = usage
        self.key = read_key(DEEPSEEK_KEY_FILE, "DeepSeek")
        self.cache = None          # кэш ответов задачи (см. Claude.cache)

    def json_call(self, *, system: str, user: str, label: str, timeout: float = 600) -> dict:
        key = None
        if self.cache is not None:
            key = self.cache.key("deepseek", self.model, system, user)
            hit = self.cache.get(key)
            if hit is not None:
                self.usage.add(model=self.model, label=label + " (готовый ответ — не оплачен)", usd=0.0, cached=True)
                return hit
        answer = self._json_call(system=system, user=user, label=label, timeout=timeout)
        if key:
            self.cache.put(key, answer)
        return answer

    def _json_call(self, *, system: str, user: str, label: str, timeout: float = 600) -> dict:
        body = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": 0,
            "response_format": {"type": "json_object"},
        }
        headers = {"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"}
        t0 = time.monotonic()
        data = _post(DEEPSEEK_URL, headers, body, timeout, "DeepSeek")
        u = data.get("usage") or {}
        self.usage.add(model=self.model, label=label, input=u.get("prompt_tokens"),
                       cache_read=u.get("prompt_cache_hit_tokens"), output=u.get("completion_tokens"),
                       seconds=round(time.monotonic() - t0, 1))
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError) as exc:
            raise LLMError(f"{label}: неожиданный ответ DeepSeek") from exc
        try:
            return json.loads(content)
        except json.JSONDecodeError as exc:
            raise LLMError(f"{label}: ответ DeepSeek — не JSON: {content[:200]}") from exc
