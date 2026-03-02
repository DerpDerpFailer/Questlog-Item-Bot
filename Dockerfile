FROM python:3.12-slim

# Install Node.js
RUN apt-get update && apt-get install -y curl && \
    curl -fsSL https://deb.nodesource.com/setup_22.x | bash - && \
    apt-get install -y nodejs && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Node dependencies
COPY package.json .
RUN npm install

# Copy source files
COPY . /app

# Écrire l'entrypoint directement — évite tout problème de CRLF ou permissions
RUN printf '#!/bin/sh\nset -e\necho "=== [1/2] Refreshing TLDB item list ==="\nnode /app/fetch_items.mjs\necho "=== [2/2] Starting Discord bot ==="\nexec python /app/bot.py\n' > /app/entrypoint.sh

CMD ["/bin/sh", "/app/entrypoint.sh"]
