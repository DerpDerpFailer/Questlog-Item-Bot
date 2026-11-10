FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Run as an unprivileged user. /app/data is created here so a fresh named
# volume inherits app ownership; a bind mount must be chowned to 1000:1000.
RUN useradd --uid 1000 --user-group --create-home app \
    && mkdir -p /app/data \
    && chown app:app /app/data

COPY . /app

USER app

CMD ["python", "-u", "bot.py"]
