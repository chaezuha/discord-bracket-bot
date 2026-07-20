FROM python:3.12-slim

# DejaVu fonts for the Pillow bracket renderer
RUN apt-get update \
    && apt-get install -y --no-install-recommends fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .
COPY bracketbot/ bracketbot/

RUN useradd --create-home bot \
    && mkdir -p /app/logs /app/data \
    && chown bot:bot /app/logs /app/data
USER bot

CMD ["python", "bot.py"]
