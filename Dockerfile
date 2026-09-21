FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    WORKCOUNT_HOST=0.0.0.0 \
    WORKCOUNT_PORT=8765 \
    WORKCOUNT_DB=/app/data/workcount.db \
    WORKCOUNT_CHROME_BINARY=/usr/bin/chromium \
    WORKCOUNT_SECURE_COOKIES=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends chromium chromium-driver fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py ./
COPY tools ./tools
COPY static ./static
RUN mkdir -p /app/data /app/templates

EXPOSE 8765
CMD ["python", "app.py"]
