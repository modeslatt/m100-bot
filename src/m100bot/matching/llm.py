"""Вызов LLM: свободный текст мастера → список упоминаний с кандидатами.

LLM только предлагает кандидатов; окончательное решение принимает валидатор в matcher.py.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from loguru import logger
from openai import APIConnectionError, APIError, APIStatusError, APITimeoutError, AsyncOpenAI
from pydantic import ValidationError
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_fixed,
)

from m100bot.matching.models import LlmResponse
from m100bot.sheets.catalog import Catalog

if TYPE_CHECKING:
    from m100bot.config import LlmSettings

# Окно Ollama — 8192 токена (OLLAMA_CONTEXT_LENGTH); остальное нужно под ответ.
PROMPT_TOKENS_WARN = 7000
# Фиксированный seed: при temperature=0 делает ответы Ollama воспроизводимыми
LLM_SEED = 42
# Предел длины пояснения note в JSON-схеме (см. strict_json_schema)
NOTE_MAX_CHARS = 150
# HTTP-коды, при которых провайдер считаем недоступным и идём к резервному
_UNAVAILABLE_STATUS = {404, 408, 429, 500, 502, 503, 504}

SYSTEM_PROMPT = """\
Ты помощник прораба на стройке. Мастер присылает дневной отчёт о выполненных работах
разговорным языком. Твоя задача — найти в сообщении КАЖДУЮ упомянутую работу (даже без объёма
и даже если её нет в смете) и подобрать для неё позиции из справочника сметы.

Правила:
- Справочник дан в формате «строка|наименование|ед.изм.». В candidates возвращай номера строк —
  число в начале той самой строки справочника, где стоит выбранное наименование. Перепроверь номер.
- Мастер называет размер сокращённо: «6-ка», «шестёрка» = 6 (Д6,35); «9-ка», «девятка» = 9 (Д9,52 или Ф9);
  «двенашка», «12-ка» = 12 (Д12,7); «кабель 3 на 2,5» = сечение 3х2,5; «автомат на 16» = Iр=16А.
- Размер сам по себе не определяет материал. Если материал не назван ни в упоминании, ни в соседнем
  контексте того же сообщения — верни ВСЕ позиции с этим размером (медь, изоляция, капиллярная трубка и т.п.)
  и ТОЛЬКО их: кабели, автоматы, работы и прочее без этого размера в candidates не добавляй.
  А «медь 6-ку 15 м и 9-ку 15 м» — «9-ку» относится к меди из того же перечисления.
- Жаргон, который УЖЕ называет материал — медный фреонопровод (не изоляцию): «медь», «медянка»,
  «фреонка», «фреоновая труба», «трасса» (фреоновая трасса). Изоляцию добавляй, только если
  она названа явно («изоляция», «кифлекс», «утеплили»).
- Называй то, что сказано, а не похожее: «насос» — это позиция с «насос» в названии, «помпа» —
  с «помпа». Другой тип оборудования в candidates не добавляй, если мастер его не назвал.
- «Изоляция» к меди = трубка теплоизоляционная того же диаметра.
- У изоляции «9 мм» — это толщина стенки (б=9мм), она одинакова у всех трубок, а не диаметр.
- Если сказано «столько же», «на них то же» — раскрой ссылку в отдельные упоминания с тем же объёмом.
- Если подходит несколько позиций — верни ВСЕ подходящие в candidates (без ограничения количества,
  лучшую первой) и опиши сомнение в note одной короткой фразой. НЕ выбирай наугад и не отбрасывай подходящие варианты.
- Если подходящей позиции нет — всё равно верни упоминание, с candidates = [] (например,
  «заштукатурили откосы», «убрались на объекте», «ждали материал 3 часа»). Никогда не пропускай
  работу молча: мастер должен увидеть, что она не попала в смету.
- Объём и единицы бери только из текста. Если объём не назван:
  - позиция считается в «шт» и предмет назван в единственном числе («поставили автомат»,
    «повесили щит», «смонтировали насос») — qty = 1, unit = «шт»;
  - во всех остальных случаях (м, кг и т.п., или множественное число: «тянули медь»,
    «поставили хомуты») — qty = null, ничего не придумывай.
- Не объединяй разные работы в одно упоминание. «A и B по одному», «A и B по 10 м» — это два
  упоминания, у каждого свой объём.

"""


def format_catalog(catalog: Catalog) -> str:
    return "\n".join(f"{p.row}|{p.short_name}|{p.unit}" for p in catalog.positions)


class LlmError(RuntimeError):
    """LLM недоступна или стабильно отвечает мусором; текст пригоден для показа мастеру."""


class InvalidLlmJson(ValueError):
    """Ответ LLM не разобрался как LlmResponse (повторяемая ошибка)."""


def strict_json_schema(model: type[LlmResponse]) -> dict[str, Any]:
    """JSON-схема pydantic-модели в виде, пригодном для strict structured output:
    все свойства обязательны, лишние запрещены, без default/title."""
    schema = copy.deepcopy(model.model_json_schema())

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            node.pop("default", None)
            node.pop("title", None)
            if node.get("type") == "object" and "properties" in node:
                node["required"] = list(node["properties"])
                node["additionalProperties"] = False
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(schema)
    # qwen3 иногда «зацикливается» в пояснении и пишет до лимита токенов. Ollama строит
    # по схеме грамматику, поэтому maxLength физически обрывает такое пояснение.
    note = schema.get("$defs", {}).get("LlmMention", {}).get("properties", {}).get("note")
    if note:
        for branch in note.get("anyOf", []):
            if branch.get("type") == "string":
                branch["maxLength"] = NOTE_MAX_CHARS
    return schema


def parse_response(content: str | None) -> LlmResponse:
    if not content:
        raise InvalidLlmJson("пустой ответ")
    text = content.strip()
    # Некоторые модели всё равно оборачивают JSON в ```json ... ```
    if text.startswith("```"):
        text = text.strip("`").removeprefix("json").strip()
    try:
        return LlmResponse.model_validate_json(text)
    except ValidationError as e:
        raise InvalidLlmJson(str(e)) from e


class LlmUnavailable(LlmError):
    """Провайдер не отвечает или недоступен — повод переключиться на резервный."""


@dataclass(frozen=True)
class LlmProvider:
    base_url: str
    api_key: str
    model: str
    # Ollama + qwen3: reasoning_effort="none" отключает режим рассуждений (проверено на 0.40.13;
    # think=false в теле /v1 и «/no_think» в промпте эта версия игнорирует).
    disable_thinking: bool = False
    role: str = "основная"

    @property
    def name(self) -> str:
        return f"{self.role} ({self.model})"  # в лог — роль и модель, не URL и не ключ


class LlmExtractor:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        *,
        timeout: float = 120.0,
        max_tokens: int = 2048,  # с запасом: самый длинный ответ эталона < 1000 токенов
        attempts: int = 2,  # 1 попытка + 1 повтор (на CPU таймаут дорог)
        retry_wait: float = 1.0,
        disable_thinking: bool = False,
        fallback: LlmProvider | None = None,
        client: AsyncOpenAI | None = None,
        fallback_client: AsyncOpenAI | None = None,
    ) -> None:
        self._max_tokens = max_tokens
        self._attempts = attempts
        self._retry_wait = retry_wait
        self._schema = strict_json_schema(LlmResponse)

        def make(p: LlmProvider, injected: AsyncOpenAI | None) -> tuple[LlmProvider, AsyncOpenAI]:
            # Встроенные повторы SDK выключены — повторами управляет tenacity.
            c = injected or AsyncOpenAI(
                base_url=p.base_url, api_key=p.api_key or "none", timeout=timeout, max_retries=0
            )
            return p, c

        primary = LlmProvider(base_url, api_key, model, disable_thinking)
        self._providers = [make(primary, client)]
        if fallback is not None:
            self._providers.append(make(replace(fallback, role="резервная"), fallback_client))

    @classmethod
    def from_settings(cls, s: LlmSettings) -> LlmExtractor:
        fallback = None
        if s.llm_fallback_base_url and s.llm_fallback_model:
            fallback = LlmProvider(
                s.llm_fallback_base_url, s.llm_fallback_api_key, s.llm_fallback_model
            )
        return cls(
            s.llm_base_url,
            s.llm_api_key,
            s.llm_model,
            timeout=s.llm_timeout_s,
            max_tokens=s.llm_max_tokens,
            disable_thinking=s.llm_disable_thinking,
            fallback=fallback,
        )

    def _messages(self, text: str, catalog: Catalog) -> list[dict[str, str]]:
        user = f"Справочник сметы:\n{format_catalog(catalog)}\n\nСообщение мастера:\n{text}"
        return [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user},
        ]

    async def _call(
        self,
        provider: LlmProvider,
        client: AsyncOpenAI,
        messages: list[dict[str, str]],
        attempt: int = 1,
    ) -> LlmResponse:
        extra: dict[str, Any] = {}
        if provider.disable_thinking:
            extra["reasoning_effort"] = "none"
        resp = await client.chat.completions.create(
            model=provider.model,
            messages=messages,  # type: ignore[arg-type]
            temperature=0,
            # без seed Ollama на одном и том же запросе иногда отвечает по-разному;
            # повтор — с другим seed, иначе модель повторит тот же «зацикленный» ответ
            seed=LLM_SEED + attempt - 1,
            max_tokens=self._max_tokens,
            response_format={
                "type": "json_schema",
                "json_schema": {"name": "llm_response", "strict": True, "schema": self._schema},
            },
            **extra,
        )
        self._log_usage(provider, resp.usage)
        choice = resp.choices[0] if resp.choices else None
        if choice is not None and getattr(choice, "finish_reason", None) == "length":
            # модель «зациклилась» и упёрлась в лимит — JSON будет оборван
            logger.warning(
                "LLM {}: ответ обрезан по max_tokens={}", provider.name, self._max_tokens
            )
        return parse_response(choice.message.content if choice else None)

    @staticmethod
    def _log_usage(provider: LlmProvider, usage: Any) -> None:
        prompt = getattr(usage, "prompt_tokens", None)
        completion = getattr(usage, "completion_tokens", None)
        logger.info("LLM {}: промпт {} ток., ответ {} ток.", provider.name, prompt, completion)
        if isinstance(prompt, int) and prompt > PROMPT_TOKENS_WARN:
            logger.warning(
                "LLM {}: промпт {} токенов > {} — близко к окну контекста, часть справочника "
                "может обрезаться (проверь OLLAMA_CONTEXT_LENGTH)",
                provider.name,
                prompt,
                PROMPT_TOKENS_WARN,
            )

    async def extract(self, text: str, catalog: Catalog) -> LlmResponse:
        """Фраза мастера → черновые упоминания с кандидатами.

        У каждого провайдера: повторы при таймауте и невалидном JSON. Если основной
        недоступен (нет соединения, таймаут, 404/429/5xx) и задан резервный — идём к нему.
        Иначе бросаем LlmError с текстом для мастера.
        """
        messages = self._messages(text, catalog)
        for i, (provider, client) in enumerate(self._providers):
            try:
                return await self._extract_with(provider, client, messages)
            except LlmUnavailable:
                if i + 1 < len(self._providers):
                    logger.warning(
                        "LLM {} недоступна — переключаюсь: {}",
                        provider.name,
                        self._providers[i + 1][0].name,
                    )
                    continue
                raise
        raise AssertionError("unreachable")

    async def _extract_with(
        self, provider: LlmProvider, client: AsyncOpenAI, messages: list[dict[str, str]]
    ) -> LlmResponse:
        retrying = AsyncRetrying(
            stop=stop_after_attempt(self._attempts),
            wait=wait_fixed(self._retry_wait),
            retry=retry_if_exception_type((APITimeoutError, InvalidLlmJson)),
            reraise=True,
            before_sleep=lambda rs: logger.warning(
                "LLM {}: попытка {} не удалась ({!r}), повторяю",
                provider.name,
                rs.attempt_number,
                rs.outcome.exception() if rs.outcome else "?",
            ),
        )
        try:
            async for attempt in retrying:
                with attempt:
                    number = attempt.retry_state.attempt_number
                    result = await self._call(provider, client, messages, number)
                    logger.info("LLM {}: {} упоминаний", provider.name, len(result.mentions))
                    return result
        except APITimeoutError as e:
            logger.error("LLM {}: нет ответа после {} попыток", provider.name, self._attempts)
            raise LlmUnavailable("Сервис распознавания не отвечает. Попробуйте позже.") from e
        except InvalidLlmJson as e:
            logger.error(
                "LLM {}: невалидный JSON {} раз подряд: {}", provider.name, self._attempts, e
            )
            raise LlmError("Не удалось разобрать отчёт. Попробуйте переформулировать.") from e
        except APIConnectionError as e:
            logger.error("LLM {}: нет соединения ({})", provider.name, type(e).__name__)
            raise LlmUnavailable("Сервис распознавания недоступен. Попробуйте позже.") from e
        except APIStatusError as e:
            logger.error("LLM {}: HTTP {}: {}", provider.name, e.status_code, e.message)
            msg = "Сервис распознавания недоступен. Попробуйте позже."
            if e.status_code in _UNAVAILABLE_STATUS:
                raise LlmUnavailable(msg) from e
            raise LlmError(msg) from e
        except APIError as e:
            logger.error("LLM {}: ошибка API {}: {}", provider.name, type(e).__name__, e.message)
            raise LlmError("Сервис распознавания недоступен. Попробуйте позже.") from e
        raise AssertionError("unreachable")
