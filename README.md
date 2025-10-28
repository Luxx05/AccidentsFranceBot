<p align="center">
  🇫🇷 <b>Français</b> │ 🇬🇧 <a href="./README_EN.md">English</a>
</p>

![Bannière](https://github.com/Luxx05/AccidentsFranceBot/raw/main/assets/banner.png)

<h1 align="center">🚨 Accidents France Bot</h1>
<p align="center">
  <b>Bot Telegram automatisé pour la communauté Accidents France.</b><br>
  Envoi anonyme, modération, tri automatique (radars / accidents) et publication instantanée.
</p>

<p align="center">
  <a href="https://render.com">
    <img src="https://img.shields.io/badge/Render-Online-brightgreen?style=flat-square&logo=render&logoColor=white" alt="Render Status"/>
  </a>
  <a href="https://t.me/AccidentsFR">
    <img src="https://img.shields.io/badge/Telegram-Communauté-blue?style=flat-square&logo=telegram" alt="Telegram"/>
  </a>
  <a href="https://github.com/Luxx05/AccidentsFranceBot">
    <img src="https://img.shields.io/github/license/Luxx05/AccidentsFranceBot?style=flat-square" alt="License"/>
  </a>
</p>

---

## 🔧 Fonctionnalités principales

- 📸 Envoi **anonyme** de vidéos, photos et signalements d'accidents  
- 🧠 **Tri intelligent** vers le bon topic :  
  - 🎥 `Vidéos & Dashcams`  
  - 📍 `Radars & Signalements`  
- 🧩 **Validation manuelle** par les administrateurs avant publication  
- 🚀 **Publication automatique** dans le groupe public  
- 🧱 **Anti-flood** et protection contre le spam intégrés  
- ☁️ Hébergement sur **Render** avec système de **keep-alive**

---

## 📡 Structure du projet

| Fichier | Description |
|----------|-------------|
| `bot.py` | Script principal du bot |
| `requirements.txt` | Dépendances Python |
| `Procfile` | Configuration Render |
| `README.md` | Documentation du projet |
| `assets/banner.png` | Bannière GitHub |

---

## ⚙️ Variables d'environnement

| Variable | Description |
|-----------|--------------|
| `BOT_TOKEN` | Token du bot Telegram (@BotFather) |
| `ADMIN_GROUP_ID` | ID du groupe privé de modération |
| `PUBLIC_GROUP_ID` | ID du groupe public |
| `KEEP_ALIVE_URL` | URL Render pour le ping automatique |

---

## 🚀 Déploiement

1. Crée un **Render Web Service (Free)**  
2. Connecte ton **repo GitHub**  
3. Ajoute les variables d'environnement listées ci-dessus  
4. Le bot s’auto-ping toutes les 10 minutes pour rester actif  

---

## 💬 Liens utiles

- 🛰️ **Canal principal :** [@Accidents_France](https://t.me/Accidents_France)  
- 👥 **Groupe public :** [t.me/AccidentsFR](https://t.me/AccidentsFR)  
- 🤖 **Bot :** [@AccidentsFranceBot](https://t.me/AccidentsFranceBot)

---

## 🧠 À venir

- 📩 Notification automatique à l’auteur après publication  
- 📊 Statistiques hebdomadaires sur les signalements  
- 🛰️ Géolocalisation simplifiée des radars et accidents  

---

<p align="center">
  <i>Projet développé pour centraliser les signalements d'accidents, radars et dashcams en France.</i><br>
  <b>Créé par L.S 🇫🇷</b>
</p>
