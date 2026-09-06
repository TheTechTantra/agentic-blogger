FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY alembic.ini .
COPY agentic_blogger/ agentic_blogger/
COPY migrations/ migrations/
COPY config/ config/
COPY scripts/ scripts/
COPY assets/ assets/

USER nobody

CMD ["python", "-m", "agentic_blogger.orchestrator.worker"]
