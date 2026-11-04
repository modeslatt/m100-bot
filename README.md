# M100 Bot

Telegram-бот для мастера: дневной отчёт текстом или голосом → распознавание работ и объёмов
локальной LLM → подтверждение → запись в Google-таблицу и Битрикс24.

## Что нужно заранее

| Что | Зачем | Примечание |
|---|---|---|
| Windows 10/11, 64-бит | | команды ниже — для PowerShell |
| **Python 3.12, 64-бит** | сам бот | python.org → Windows installer (64-bit) или `winget install Python.Python.3.12`. **Не 3.13+**: под новые версии нет готовых сборок `pydantic-core`, `aiohttp`, `pyyaml`, установка падает с `Failed building wheel` |
| **Ollama** | локальная LLM | ollama.com; модель `qwen3:8b` (~5 ГБ на диске) |
| ОЗУ от 8 ГБ, лучше 16 | модель частично лежит в ОЗУ | |
| Telegram-бот | | токен у @BotFather |
| Google-таблица + сервисный аккаунт | справочник и запись объёмов | см. `.env` ниже |
| Ключ Groq (необязательно) | распознавание голосовых | без него бот принимает только текст |
| Битрикс24 (необязательно) | дублирование отчётов в CRM | |

## 1. Ollama

```powershell
ollama pull qwen3:8b
```

Переменные Ollama (один раз). Первая обязательна — без неё модель молча обрезает справочник
и «не видит» часть позиций; остальные экономят память:

```powershell
setx OLLAMA_CONTEXT_LENGTH 8192
setx OLLAMA_NUM_PARALLEL 1
setx OLLAMA_MAX_LOADED_MODELS 1
setx OLLAMA_FLASH_ATTENTION 1
setx OLLAMA_KV_CACHE_TYPE q8_0
setx OLLAMA_KEEP_ALIVE -1
```

После этого перезапустить Ollama (иконка в трее → Quit → запустить снова) и открыть **новое**
окно PowerShell. Проверка:

```powershell
ollama run qwen3:8b "привет"
ollama ps        # CONTEXT 8192, UNTIL Forever; PROCESSOR — доля CPU/GPU
```

Первый запрос после включения компьютера идёт долго (модель грузится в память, на слабом
железе до ~3 мин), дальше — быстрее: `OLLAMA_KEEP_ALIVE -1` держит модель загруженной.

## 2. Установка бота

Из папки проекта:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

`[dev]` добавляет `openpyxl`, `pytest`, `ruff` — они нужны проверочным скриптам и тестам; самому
боту хватит `pip install -e .`.

Активировать окружение не обязательно — достаточно вызывать `.\.venv\Scripts\python.exe`.
Если хочется активировать (`.\.venv\Scripts\Activate.ps1`), а PowerShell пишет «выполнение
сценариев отключено», один раз выполните `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`.

## 3. Настройка `.env`

```powershell
Copy-Item .env.example .env
```

Заполнить `.env`. Рядом с `.env` положить
`credentials.json` — ключ сервисного аккаунта Google.

### Telegram

| Переменная | Значение |
|---|---|
| `BOT_TOKEN` | @BotFather → /newbot. **Один токен — один запущенный бот**: Telegram отдаёт сообщения только одному процессу, для отладки на другом компьютере заведите второго бота |
| `ALLOWED_USER_IDS` | Telegram ID мастеров через запятую. Незнакомцу бот отвечает его ID — его и вписать. Пусто — бот отвечает всем |

### LLM (Ollama)

| Переменная | Значение |
|---|---|
| `LLM_BASE_URL` | `http://localhost:11434/v1` |
| `LLM_API_KEY` | `ollama` (Ollama ключ не проверяет, но строка должна быть непустой) |
| `LLM_MODEL` | `qwen3:8b` |
| `LLM_TIMEOUT_S` | сколько ждать ответа модели, сек. Должно быть больше первого (холодного) запроса: если модель частично на процессоре — `300` |
| `LLM_MAX_TOKENS` | `2048` — предел длины ответа |
| `LLM_DISABLE_THINKING` | `true` — без «рассуждений» qwen3, ответ примерно вдвое быстрее |
| `LLM_FALLBACK_BASE_URL`, `LLM_FALLBACK_API_KEY`, `LLM_FALLBACK_MODEL` | резервная облачная модель, если Ollama недоступна. Необязательно: **либо все три с действующим ключом, либо все пустые** |

### Голос (Groq Whisper)

| Переменная | Значение |
|---|---|
| `STT_API_KEY` | console.groq.com → API Keys. Пусто — голосовые не принимаются (бот просит текст) |
| `STT_BASE_URL` | `https://api.groq.com/openai/v1` |
| `STT_MODEL` | `whisper-large-v3-turbo` (по умолчанию) или `whisper-large-v3` (точнее, медленнее) |
| `STT_TIMEOUT_S` | `60` |

### Google Sheets

| Переменная | Значение |
|---|---|
| `GOOGLE_CREDENTIALS_FILE` | `credentials.json`. Google Cloud → сервисный аккаунт → JSON-ключ; включить Google Sheets API; расшарить таблицу на e-mail сервисного аккаунта с правом **редактора** |
| `SPREADSHEET_ID` | часть адреса таблицы между `/d/` и `/edit`. Нужна именно **Google Таблица**: загруженный `.xlsx` откройте в Drive → «Файл → Сохранить как Google Таблицы» и возьмите ID новой |
| `WORKSHEET_NAME` | лист со сметой, `Лист1` |
| `SYNONYMS_WORKSHEET_NAME` | лист синонимов, `Синонимы` (колонки: A — фраза мастера, B — номер строки позиции, C — название позиции) |

### Битрикс24

| Переменная | Значение |
|---|---|
| `BITRIX_WEBHOOK_URL` | Битрикс24 → `https://<портал>/devops/` → Другое → Входящий вебхук, права **crm**. Пусто — Битрикс не используется |
| `BITRIX_ENTITY_TYPE_ID` | число из адреса смарт-процесса `/crm/type/<число>/` (его же покажет `try_bitrix.py check`) |

В Битриксе нужен смарт-процесс «Дневной отчёт» с пользовательскими полями — бот ищет их
**по названию** (регистр и «ё» не важны):

| Название | Тип |
|---|---|
| Дата отчёта | Дата |
| Мастер | Строка |
| Telegram ID мастера | Строка |
| Объёмы | Строка (многострочная) |
| Ссылка на таблицу | Строка |

### Прочее

| Переменная | Значение |
|---|---|
| `TIMEZONE` | `Europe/Minsk` — от неё зависит, в столбец какой даты пишется отчёт |
| `DB_PATH` | `data/bot.sqlite3` — черновики, журнал записей, очередь Битрикса |
| `LOG_LEVEL` | `INFO` |
| `LOG_FILE` | `data/bot.log` — лог ещё и в файл (UTF-8). Пусто — только в консоль |

## 4. Проверка подключений

```powershell
.\.venv\Scripts\python.exe scripts\try_llm.py "медь 6-ка 15 м"   # модель отвечает JSON, печатает время
.\.venv\Scripts\python.exe scripts\try_sheets.py                 # читает справочник, ничего не меняет
.\.venv\Scripts\python.exe scripts\try_bitrix.py check           # вебхук, смарт-процесс, поля; ничего не меняет
```

Скрипты, которые **пишут** (запускать только на копии таблицы / тестовом портале):
`try_sheets.py --write` создаёт столбец даты и пишет 1 в строку 17;
`try_bitrix.py send` создаёт тестовый элемент (удалить вручную).

## 5. Запуск

```powershell
.\.venv\Scripts\python.exe -m m100bot
```

В начале лога — строка `LLM: qwen3:8b на localhost:11434`.
Ollama должна быть запущена.

## Тесты

```powershell
.\.venv\Scripts\python.exe -m pytest                         # офлайн-тесты
.\.venv\Scripts\python.exe scripts\eval_golden.py            # точность сопоставления на эталоне
```
