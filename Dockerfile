FROM python:3.13-alpine

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HTML_DIR=/app/html \
    DATA_DIR=/app/data \
    PORT=80

COPY app/server.py /app/server.py
COPY app/html /app/html
RUN mkdir -p /app/data

EXPOSE 80
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD wget -q -O - http://127.0.0.1/healthz >/dev/null || exit 1

ENTRYPOINT ["python", "-u", "/app/server.py"]
