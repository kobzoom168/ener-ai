FROM python:3.11-slim

RUN apt-get update && apt-get install -y ffmpeg curl git docker.io iproute2 \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && npm install -g @openai/codex \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Visual QC needs a real headless browser in the image (the orchestrator runs
# INSIDE this container, so chromium must live here — a host install won't help).
# --with-deps pulls the OS libs/fonts chromium needs on Debian slim.
RUN python -m playwright install --with-deps chromium

COPY . .

RUN mkdir -p /app/data

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
