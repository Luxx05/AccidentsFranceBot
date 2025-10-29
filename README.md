<p align="center">
  🇫🇷 <b>Français</b> │ 🇬🇧 <a href="./README_EN.md">English</a>
</p>

![Bannière](https://github.com/Luxx05/AccidentsFranceBot/raw/main/assets/banner.png)

<h1 align="center">🚨 Accidents France Bot</h1>
<p align="center">
  <b>Bot Telegram automatisé pour la communauté Accidents France.</b><br>
  Soumission anonyme, modération avancée, tri automatique par topic et base de données persistante.
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

### 👤 Soumission (via le Bot privé)
- 📸 Envoi **anonyme** de vidéos, photos, albums et signalements.
- 📬 **Notification à l'auteur** lorsque son signalement est approuvé et publié.
- ⛔ **Notification à l'auteur** si son signalement est rejeté et qu'il est "muté".
- 🛡️ **Vérification anti-mute** : Le bot refuse les soumissions d'un utilisateur "muté".
- 🧱 **Anti-flood** simple pour les soumissions privées.

### 🛡️ Groupe Admin
- 🧩 **Validation manuelle** par les administrateurs avant publication.
- ✏️ **Bouton "Modifier"** pour réécrire un texte avant publication (gère l'anonymat admin).
- 🔇 **Bouton "Rejeter & Muter 1h"** pour rejeter un signalement et empêcher l'auteur de soumettre pendant 1h.
- ❌ **Commande `/cancel`** pour annuler une modification en cours.
- 🚀 **Raccourci admin `/deplacer`** : Publie un message directement depuis le groupe admin vers le bon topic public.
- 🧹 **Nettoyage automatique** des messages de service (ex: "X a rejoint le groupe").

### 📢 Groupe Public
- 🧠 **Tri intelligent** des signalements approuvés vers le bon topic :  
  - 🎥 `Vidéos & Dashcams`  
  - 📍 `Radars & Signalements`
  - #️⃣ `Général` (par défaut)
- ⚙️ **Commande admin `/deplacer`** pour ranger un message mal placé dans le bon topic (gère l'anonymat).
- 🔇 **Modération automatique** :
  - **Anti-spam** (supprime les messages trop rapides).
  - **Anti-charabia** (supprime les messages sans signification).
  - **Mute automatique** (restreint les spammeurs du groupe pour 5 min).
- 🧹 **Nettoyage automatique** des messages de service (changement de photo, etc.).
- 🤖 **Menu de commandes** `/` affichant les actions admin (`/deplacer`, `/cancel`).

### ⚙️ Arrière-plan
- 🗃️ **Base de données persistante (SQLite)** : Aucune perte de signalement, d'état de modification ou d'utilisateur "muté", même si le bot redémarre.
- ☁️ Hébergement sur **Render** avec système de **keep-alive** (via Flask).

---

## 📡 Structure du projet

| Fichier | Description |
|----------|-------------|
| `bot.py` | Script principal du bot |
| `requirements.txt` | Dépendances Python (Telegram, aiosqlite, flask, requests) |
| `Procfile` | Configuration Render |
| `README.md` | Documentation du projet (FR) |
| `README_EN.md` | Documentation du projet (EN) |
| `assets/banner.png` | Bannière GitHub |

---

## ⚙️ Variables d'environnement

| Variable | Description |
|-----------|--------------|
| `BOT_TOKEN` | Token du bot Telegram (@BotFather) |
| `ADMIN_GROUP_ID` | ID du groupe privé de modération |
| `PUBLIC_GROUP_ID` | ID du groupe public |
| `KEEP_ALIVE_URL` | URL Render pour le ping automatique |
| `DB_PATH` | **[Requis]** Chemin vers le fichier de BDD (ex: `/var/data/bot_storage.db` sur Render) |

---

## 🚀 Déploiement

1. Crée un **Render Web Service (Free)**.
2. Connecte ton **repo GitHub**.
3. Ajoute les **Variables d'environnement** listées ci-dessus.
4. **Important :** Ajoute un **"Disque Persistant"** sur Render (ex: point de montage `/var/data`) et utilise ce chemin pour la variable `DB_PATH` afin de ne perdre aucune donnée.
5. Le bot s’auto-ping toutes les 10 minutes pour rester actif.

---

## 💬 Liens utiles

- 🛰️ **Canal principal :** [@Accidents_France](https://t.me/Accidents_France)  
- 👥 **Groupe public :** [t.me/AccidentsFR](https://t.me/AccidentsFR)  
- 🤖 **Bot :** [@AccidentsFranceBot](https://t.me/AccidentsFranceBot)

---

## 🧠 À venir

- 📊 Statistiques hebdomadaires sur les signalements.
- 🛰️ Géolocalisation simplifiée des radars et accidents.
- 📂 Gestion du déplacement (`/deplacer`) pour les albums complets.
- 🛡️ Commande `/signaler` pour les membres du groupe public.

---

<p align="center">
  <i>Projet développé pour centraliser les signalements d'accidents, radars et dashcams en France.</i><br>
  <i>🔧 Bot flexible et réutilisable pour d’autres communautés ou projets.</i><br>
  <b>Créé par L.S 🇫🇷</b>
</p>
