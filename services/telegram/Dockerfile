FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY agentic_blogger/ agentic_blogger/
COPY config/ config/
COPY scripts/ scripts/

USER nobody

CMD ["python", "-m", "agentic_blogger.ingestion.telegram_bot"]
