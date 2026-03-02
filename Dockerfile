# Dockerfile
FROM python:3.12-slim

# Set working directory
WORKDIR /app

# Copier le code
COPY . /app

# Installer les dépendances
RUN pip install --no-cache-dir -r requirements.txt

# Lancer le bot
CMD ["python", "bot.py"]
