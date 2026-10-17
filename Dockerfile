FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY pyproject.toml ./
COPY server ./server
COPY ingest ./ingest
COPY admin ./admin
RUN pip install --no-cache-dir .

# The read-only book database is built offline (see README) and shipped inside the image.
COPY data/books.db ./data/books.db

RUN useradd --create-home --uid 1000 app && mkdir -p /data/state && chown -R app /data/state
USER app

ENV BOOKS_DB_PATH=/app/data/books.db STATE_DB_URL=sqlite:////data/state/state.db PORT=8000
EXPOSE 8000
HEALTHCHECK --interval=60s --timeout=5s --start-period=10s \
  CMD python -c "import os,urllib.request; urllib.request.urlopen(f'http://127.0.0.1:8000/{os.environ[\"SECRET_TOKEN\"]}/health', timeout=4)" || exit 1
CMD ["python", "-m", "server.main"]
