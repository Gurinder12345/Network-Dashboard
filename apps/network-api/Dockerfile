FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

RUN groupadd --gid 10001 appgroup && \
    useradd --uid 10001 \
            --gid 10001 \
            --create-home \
            --shell /usr/sbin/nologin \
            appuser && \
    chown -R appuser:appgroup /app

USER appuser

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
