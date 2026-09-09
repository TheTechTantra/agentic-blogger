FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir \
    mlflow==3.16.0 \
    psycopg2-binary==2.9.12

COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

EXPOSE 5000

ENTRYPOINT ["/app/entrypoint.sh"]
