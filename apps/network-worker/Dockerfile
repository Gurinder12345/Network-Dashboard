FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY worker.py .

RUN groupadd --system appgroup && \
    useradd --system --gid appgroup --create-home appuser && \
    chown -R appuser:appgroup /app

USER appuser

CMD ["celery", "-A", "worker.app", "worker", "--loglevel=INFO"]
