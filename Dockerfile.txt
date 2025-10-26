# On force la version de Python stable pour la lib telegram
FROM python:3.11-slim

# On crée un dossier de travail dans le conteneur
WORKDIR /app

# On copie les dépendances et on les installe
COPY requirements.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# On copie le code du bot
COPY bot.py bot.py

# Empêche Python de buffer les logs (tu vois tout en live dans Render)
ENV PYTHONUNBUFFERED=1

# Commande lancée par Render
CMD ["python", "bot.py"]

