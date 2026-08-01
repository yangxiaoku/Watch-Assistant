FROM node:24-alpine AS frontend-build
WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim AS runtime
ARG WATCH_ASSISTANT_RELEASE=unknown
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    FRONTEND_DIST_DIR=/app/frontend/dist \
    WATCH_ASSISTANT_RELEASE=$WATCH_ASSISTANT_RELEASE
WORKDIR /app
COPY pyproject.toml ./
COPY src/ ./src/
RUN pip install --no-cache-dir . \
    && useradd --system --uid 10001 --home /app watch-assistant \
    && mkdir -p /data /app/frontend/dist \
    && chown -R watch-assistant:watch-assistant /data /app
COPY --from=frontend-build --chown=watch-assistant:watch-assistant /build/frontend/dist/ /app/frontend/dist/
COPY --chown=watch-assistant:watch-assistant config/ /app/config/
USER watch-assistant
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health', timeout=3)"
CMD ["uvicorn", "watch_assistant.app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
