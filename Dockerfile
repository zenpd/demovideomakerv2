# Demo Video Maker – production container
# Base: Playwright Python image (has Chromium + system deps pre-installed)
FROM mcr.microsoft.com/playwright/python:v1.44.0-jammy

# System packages: ffmpeg for video assembly + fonts for drawtext overlay
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    fonts-liberation \
    fonts-dejavu-core \
    fonts-freefont-ttf \
    fontconfig \
    && fc-cache -fv \
    && rm -rf /var/lib/apt/lists/*

# App directory
WORKDIR /app

# Copy & install Python deps first (layer-caching)
COPY app/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright browser (chromium only – already in base but ensure installed)
RUN playwright install chromium --with-deps 2>/dev/null || true

# Copy app source
COPY app/ .

# Create runtime directories
RUN mkdir -p /app/temp /app/output /app/static

# NOTE: In container, system ffmpeg at /usr/bin/ffmpeg is used automatically
# (the app checks for local bin/ffmpeg first, falls back to system ffmpeg)

EXPOSE 8899

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8899", "--workers", "1"]
