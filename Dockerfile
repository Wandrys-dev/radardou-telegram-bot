FROM python:3.11-slim

# Permite instalacao de pacotes via git+https
RUN apt-get update \
 && apt-get install -y --no-install-recommends git \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Cache das dependencias separadamente
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Codigo
COPY bot.py storage.py ./

# Persiste o SQLite num volume montado
ENV DATABASE_PATH=/data/bot_users.db
VOLUME ["/data"]

# Sem porta exposta — bot usa long polling, nao webhook
CMD ["python", "-u", "bot.py"]
