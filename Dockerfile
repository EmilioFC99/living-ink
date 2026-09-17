# ==============================================================================
# Living Ink — Dockerfile
# Production-ready, non-root, slim container image
# ==============================================================================

FROM ghcr.io/astral-sh/uv:0.6.14 AS uv_bin

FROM python:3.11-slim-bookworm AS runtime

# System runtime dependencies (libcairo2 needed for cairosvg stroke rendering)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libcairo2 \
    ca-certificates \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy uv binary from astral-sh official distribution
COPY --from=uv_bin /uv /bin/uv

# Set working directory
WORKDIR /app

# Configure Python and Living Ink environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH" \
    LIVING_INK_CONFIG_DIR="/app/config" \
    LIVING_INK_DATA_DIR="/app/data"

# Step 1: Copy project metadata and lockfile to leverage Docker layer caching
COPY pyproject.toml uv.lock README.md ./

# Step 2: Install dependencies into /app/.venv without installing the project yet
RUN uv sync --frozen --no-dev --no-install-project

# Step 3: Copy application source code
COPY src/ ./src/

# Step 4: Install the living-ink CLI package
RUN uv sync --frozen --no-dev

# Step 5: Security — non-root user and persistent directory permissions
RUN groupadd -r -g 1000 livingink && \
    useradd -r -u 1000 -g livingink -d /app -s /bin/bash livingink && \
    mkdir -p /app/config /app/data /vault && \
    chown -R livingink:livingink /app /vault

USER livingink

# Mount points for configuration, state cache, and Obsidian vault
VOLUME ["/app/config", "/app/data", "/vault"]

# Standard entrypoint: living-ink subcommands (sync, setup, status)
ENTRYPOINT ["living-ink"]
CMD ["sync"]
