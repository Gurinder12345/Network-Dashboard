FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY worker.py .
COPY inventory ./inventory
COPY tasks ./tasks
COPY vault ./vault
COPY db ./db

RUN groupadd --gid 10001 appgroup && \
    useradd --uid 10001 \
            --gid 10001 \
            --create-home \
            --shell /usr/sbin/nologin \
            appuser && \
    chown -R appuser:appgroup /app

USER appuser

CMD ["celery", "-A", "worker.app", "worker", "--loglevel=INFO"]
