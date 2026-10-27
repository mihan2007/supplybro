"""
Обращения к моделям: Claude (Anthropic API) и DeepSeek.

- Ответ Claude — один JSON-объект; схема ответа — в тексте задания (после документов), код разбирает ответ,
  приводит типы и при сбое один раз переспрашивает. Инструментов у модели нет.
  (Не output_config.format: схема в нём — часть начала запроса, и при разных схемах кэш документов
  не срабатывал — проверено 27.10.2026: из 10 вызовов 9 записывали документы в кэш заново.)
- Текст документов передаётся одним блоком с пометкой кэша: первый вызов записывает его в кэш Anthropic,
  следующие вызовы того же анализа читают из кэша (в ~10 раз дешевле).
- Повторы при перегрузке / лимитах / обрывах связи.
- Учёт токенов по каждому вызову — для журнала и стоимости.

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

CLAUDE_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"

CLAUDE_KEY_FILE = os.environ.get("CLAUDE_KEY_FILE_IN_CONTAINER", "/run/secrets/claude")
DEEPSEEK_KEY_FILE = os.environ.get("DEEPSEEK_KEY_FILE_IN_CONTAINER", "/run/secrets/deepseek")

RETRY_STATUS = {408, 409, 425, 429, 500, 502, 503, 504, 529}
MAX_ATTEMPTS = 6


class LLMError(RuntimeError):
    """Ошибка обращения к модели — текст показывается пользователю."""


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
        with self._lock:
            self.calls.append(entry)

    def totals(self) -> dict:
        out: dict[str, dict] = {}
        for c in self.calls:
            m = out.setdefault(c["model"], {"calls": 0, "input": 0, "cache_write": 0, "cache_read": 0, "output": 0,
                                            "seconds": 0.0})
            m["calls"] += 1
            for k in ("input", "cache_write", "cache_read", "output"):
                m[k] += int(c.get(k) or 0)
            m["seconds"] = round(m["seconds"] + float(c.get("seconds") or 0), 1)
        return out


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
        if r.status_code in (401, 403):
            raise LLMError(f"{what} не принял ключ ({r.status_code}): {text}")
        if r.status_code in RETRY_STATUS and attempt < MAX_ATTEMPTS:
            wait = r.headers.get("retry-after")
            try:
                pause = float(wait) if wait else min(90, 5 * 2 ** (attempt - 1))
            except ValueError:
                pause = min(90, 5 * 2 ** (attempt - 1))
            last = f"{what} ответил {r.status_code}: {text}"
            time.sleep(min(pause, 120) + random.random())
            continue
        raise LLMError(f"{what} ответил {r.status_code}: {text}")
    raise LLMError(last or f"{what}: не удалось получить ответ")


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

    def json_call(self, *, system: str, documents: str | None, prompt: str, schema: dict, label: str,
                  max_tokens: int = 16000, timeout: float = 900) -> dict:
        """Один вызов Claude с ответом одним JSON-объектом по схеме.

        documents — текст документов (кэшируется: должен быть одинаковым у всех вызовов одного анализа).
        prompt — задание этого вызова (после документов, не кэшируется); схема ответа дописывается к нему.
        """
        task = (prompt + "\n\nФормат ответа: только один JSON-объект — без пояснений до и после, без ```. "
                "Схема (JSON Schema):\n" + json.dumps(schema, ensure_ascii=False))
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
            u = data.get("usage") or {}
            self.usage.add(model=self.model, label=label if attempt == 1 else label + " (повтор)",
                           input=u.get("input_tokens"), cache_write=u.get("cache_creation_input_tokens"),
                           cache_read=u.get("cache_read_input_tokens"), output=u.get("output_tokens"),
                           seconds=round(time.monotonic() - t0, 1), stop=data.get("stop_reason"))
            if data.get("stop_reason") == "max_tokens":
                raise LLMError(f"{label}: ответ модели обрезан (не хватило max_tokens={max_tokens})")
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

    def json_call(self, *, system: str, user: str, label: str, timeout: float = 600) -> dict:
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
