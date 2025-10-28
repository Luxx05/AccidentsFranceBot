# 🚨 Accidents France Bot

Bot Telegram automatisé pour la communauté **Accidents France 🇫🇷**  
📸 Signalements d’accidents, dashcams et radars en temps réel.  
Le bot gère la **modération, anonymisation et publication automatique** dans le groupe public.

---

## ⚙️ Fonctionnalités principales

- ✅ **Envoi anonyme** : les utilisateurs peuvent signaler un accident ou radar sans révéler leur identité.  
- 🧠 **Tri intelligent** :  
  - Les messages contenant “radar”, “contrôle”, etc. sont envoyés dans le topic **📍 Radars & Signalements**.  
  - Les vidéos ou textes contenant “accident”, “dashcam”, etc. vont dans **🎥 Vidéos & Dashcams**.  
- 🛡️ **Vérification avant publication** : tout passe par le groupe admin pour modération.  
- 📤 **Publication automatique** dans le groupe public une fois approuvé.  
- 🧹 **Anti-spam & nettoyage mémoire** intégré.  
- 🔄 **Compatible Render / Keep-Alive** (Flask + thread pour éviter l’endormissement).  

---

## 🧩 Structure du projet

/app
├── bot.py # Code principal du bot
├── requirements.txt # Dépendances Python
├── Procfile # Lancement Render
└── assets/ # (optionnel) bannières, visuels, logo

yaml
Copier le code

---

## 🚀 Déploiement

1. Crée une app **Render** (Free Web Service).
2. Ajoute les variables d’environnement :
BOT_TOKEN=<ton_token_bot>
ADMIN_GROUP_ID=<ID_du_groupe_admin>
PUBLIC_GROUP_ID=<ID_du_groupe_public>
KEEP_ALIVE_URL=<URL_de_ton_service_Render>

yaml
Copier le code
3. Connecte ton repo GitHub et déploie automatiquement.  
4. Le bot ping périodiquement ton URL pour rester actif.

---

## 🔗 Liens officiels

- Groupe public → [@AccidentsFR](https://t.me/AccidentsFR)  
- Bot de signalement → [@AccidentsFranceBot](https://t.me/AccidentsFranceBot)  
- Canal Telegram → [Accidents France 🚨](https://t.me/accidents_france)

---

## 💡 À venir

- 📩 Notification automatique à l’utilisateur quand son signalement est approuvé.  
- 🧠 Analyse plus fine des textes pour un tri encore plus précis.  
- 📊 Statistiques automatiques hebdomadaires des signalements.

---

**Créé et maintenu par : [Laurentiu Stoian](https://github.com/)**  
🔥 Open Source — projet communautaire, améliorable par tous.
