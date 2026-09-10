FROM python:3.12-slim

WORKDIR /app

# `mlflow[genai]` — not bare `mlflow`. The AI Gateway's provider adapters
# (anthropic, gemini, openai, ollama) and its encrypted credential store ship
# in that extra; without it the gateway REST routes answer but every
# invocation fails on a missing provider SDK.
RUN pip install --no-cache-dir \
    "mlflow[genai]==3.16.0" \
    psycopg2-binary==2.9.12

COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

EXPOSE 5000

ENTRYPOINT ["/app/entrypoint.sh"]
