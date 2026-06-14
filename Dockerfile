FROM python:3.11-slim

RUN apt-get update && apt-get install -y ffmpeg curl git docker.io iproute2 fonts-thai-tlwg \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && npm install -g @openai/codex \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Visual QC needs a real headless browser in the image (the orchestrator runs
# INSIDE this container, so chromium must live here — a host install won't help).
# python:3.11-slim is Debian bookworm; `playwright install --with-deps` fails here
# because it references font packages (ttf-unifont, ttf-ubuntu-font-family) that
# slim has dropped — so install chromium's runtime libs ourselves, then the browser.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 libdbus-1-3 \
    libatspi2.0-0 libx11-6 libxcomposite1 libxdamage1 libxext6 libxfixes3 libxrandr2 \
    libgbm1 libxcb1 libxkbcommon0 libpango-1.0-0 libcairo2 libasound2 \
    libwayland-client0 fonts-liberation \
    && rm -rf /var/lib/apt/lists/*
RUN python -m playwright install chromium

COPY . .

RUN mkdir -p /app/data

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
