# One image serves the API and the built React app (same origin: no CORS, one free Render service).

# --- Stage 1: build the React app ---
FROM node:24-slim AS web
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# --- Stage 2: Python runtime ---
FROM python:3.14-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    STATIC_DIR=/app/static
WORKDIR /app
COPY backend/requirements.txt .
RUN pip install -r requirements.txt && useradd --system --no-create-home app
COPY backend/app app
COPY --from=web /web/dist static
USER app
EXPOSE 8000
# Render (and most hosts) pass the port in $PORT.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
