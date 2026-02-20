# ============================================================
#  NSE Options Flask Server — Docker Image
#  Uses Playwright Chromium for NSE cookie extraction
# ============================================================
FROM python:3.11-slim

# Install system dependencies required by Playwright Chromium
RUN apt-get update && apt-get install -y --no-install-recommends \
    # Chromium runtime deps
    libnss3 \
    libnspr4 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libdbus-1-3 \
    libxkbcommon0 \
    libatspi2.0-0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libpango-1.0-0 \
    libcairo2 \
    libasound2 \
    libwayland-client0 \
    # Fonts (for page rendering)
    fonts-liberation \
    fonts-noto-color-emoji \
    # Utilities
    curl \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy requirements first (Docker layer cache)
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir \
    pandas \
    requests \
    flask \
    flask-cors \
    playwright \
    gunicorn

# Install Playwright Chromium browser
RUN playwright install chromium

# Copy application code
COPY nse_server.py .
COPY NseUtility.py .

# Environment variables
ENV PYTHONUNBUFFERED=1
ENV PORT=5000

# Expose Flask port
EXPOSE 5000

# Health check
HEALTHCHECK --interval=60s --timeout=10s --start-period=120s --retries=3 \
    CMD curl -f http://localhost:5000/api/health || exit 1

# Run with gunicorn for production (Flask dev server is not production-ready)
# Using 1 worker because the background fetcher thread is shared state
CMD ["python", "nse_server.py"]
