![Bannière](https://github.com/Luxx05/AccidentsFranceBot/raw/main/assets/banner.png)

<h1 align="center">🚨 Accidents France Bot</h1>
<p align="center">
  <b>Bot Telegram automatisé pour la communauté Accidents France.</b><br>
  Envoi anonyme, modération, tri automatique par type de signalement (radar / accident) et publication instantanée.
</p>

<p align="center">
  <a href="https://render.com">
    <img src="https://img.shields.io/badge/Render-Online-brightgreen?style=flat-square&logo=render&logoColor=white" alt="Render Status"/>
  </a>
  <a href="https://t.me/AccidentsFR">
    <img src="https://img.shields.io/badge/Telegram-Community-blue?style=flat-square&logo=telegram" alt="Telegram"/>
  </a>
  <a href="https://github.com/Luxx05/AccidentsFranceBot">
    <img src="https://img.shields.io/github/license/Luxx05/AccidentsFranceBot?style=flat-square" alt="License"/>
  </a>
</p>

---

## 🔧 Fonctionnalités principales

- 📸 Envoi **anonyme** de vidéos, photos et signalements d'accidents ou radars  
- 🧠 **Tri intelligent automatique** vers le bon topic :  
  - 🎥 `Vidéos & Dashcams`  
  - 📍 `Radars & Signalements`  
- 🧩 **Validation manuelle** par les administrateurs avant publication  
- 🚀 **Publication automatique** dans le groupe public après approbation  
- 🧱 **Anti-spam & anti-flood** intégré pour éviter les abus  
- ☁️ Hébergement **Render** avec système de **keep-alive** automatique  

---

## 📡 Structure du projet

| Fichier | Description |
|----------|-------------|
| `bot.py` | Code principal du bot |
| `requirements.txt` | Dépendances Python |
| `Procfile` | Démarrage Render |
| `README.md` | Documentation du projet |
| `assets/banner.png` | Bannière GitHub |

---

## ⚙️ Variables d'environnement

| Variable | Description |
|-----------|--------------|
| `BOT_TOKEN` | Token du bot Telegram (@BotFather) |
| `ADMIN_GROUP_ID` | ID du groupe admin (modération) |
| `PUBLIC_GROUP_ID` | ID du groupe public (publication) |
| `KEEP_ALIVE_URL` | URL Render utilisée pour le ping automatique |

---

## 🚀 Déploiement

1. Crée une app **Render Web Service (Free)**  
2. Connecte ton **repo GitHub**  
3. Ajoute les variables d’environnement listées ci-dessus  
4. Le bot ping automatiquement ton service toutes les 10 minutes pour rester actif  

---

## 💬 Liens utiles

- 🛰️ **Canal principal :** [@Accidents_France](https://t.me/Accidents_France)  
- 👥 **Groupe public :** [t.me/AccidentsFR](https://t.me/AccidentsFR)  
- 🤖 **Bot :** [@AccidentsFranceBot](https://t.me/AccidentsFranceBot)

---

## 🧠 À venir

- 📩 Notification automatique à l’utilisateur quand son signalement est publié  
- 📊 Statistiques hebdomadaires sur les signalements  
- 🛰️ Système de géolocalisation simplifié pour les radars et accidents  

---

<p align="center">
  <i>Projet développé pour centraliser les signalements d'accidents, radars et vidéos dashcam en France.</i><br>
  <b>Créé par Laurentiu Stoian 🇫🇷</b>
</p>
