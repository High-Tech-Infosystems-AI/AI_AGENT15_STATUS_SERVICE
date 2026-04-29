# syntax=docker/dockerfile:1
FROM python:3.13-slim as base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_SYSTEM_PYTHON=1

# System deps
# - build-essential, curl: existing chat/status base
# - libfreetype6 + libpng + fonts-dejavu: matplotlib runtime needs for the
#   AI chatbot's chart renderer (PNG output -> S3)
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential curl \
        libfreetype6 libpng16-16 fonts-dejavu \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install uv
RUN pip install --no-cache-dir uv

# Copy and install deps first (better layer caching)
COPY pyproject.toml /app/
RUN uv pip install -r pyproject.toml

# Copy source
COPY . /app

# Expose ports:
#   8515 — Status API
#   8517 — Chat API
#   8518 — AI Chat API
#   5009 — Notification UI
EXPOSE 8515 8517 8518 5009

# Healthcheck — verify the three APIs respond. UI is non-critical.
HEALTHCHECK --interval=30s --timeout=10s --start-period=45s --retries=3 \
  CMD curl -f http://127.0.0.1:8515/health \
   && curl -f http://127.0.0.1:8517/health \
   && curl -f http://127.0.0.1:8518/health || exit 1

# Start four processes: status API + chat API + AI chat API + notification UI
COPY start.sh /app/start.sh
RUN chmod +x /app/start.sh
CMD ["/app/start.sh"]
