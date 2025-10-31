FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt .
RUN python -m pip install --upgrade pip && \
    pip install -r requirements.txt

# Copie tout (si demain tu ajoutes des modules/fichiers, pas besoin de toucher le Dockerfile)
COPY . .

CMD ["python", "bot.py"]