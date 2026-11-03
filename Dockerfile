# Образ бота для сервера. LLM на сервере — облачная (Groq), Ollama в образ не входит.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Europe/Minsk

WORKDIR /app

# Зависимости отдельным слоем: правка кода не пересобирает их заново.
# tzdata — база часовых поясов для zoneinfo (в slim-образе её нет).
COPY pyproject.toml ./
COPY src ./src
RUN pip install . tzdata

# Не root. data/ — SQLite и лог, монтируется томом (см. docker-compose.yml).
RUN useradd --create-home --uid 1000 bot && mkdir -p /app/data && chown bot:bot /app/data
USER bot

# .env и credentials.json в образ не кладём: они монтируются при запуске
CMD ["python", "-m", "m100bot"]
