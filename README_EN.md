<p align="center">
  🇫🇷 <a href="./README.md">Français</a> │ 🇬🇧 <b>English</b>
</p>

![Banner](https://github.com/Luxx05/AccidentsFranceBot/raw/main/assets/banner.png)

<h1 align="center">🚨 Accidents France Bot</h1>
<p align="center">
  <b>Automated Telegram bot for the Accidents France community.</b><br>
  Anonymous submissions, moderation, smart sorting (radars / accidents) and instant publication.
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

## 🔧 Main Features

- 📸 **Anonymous submission** of accident or radar videos, photos and reports  
- 🧠 **Smart auto-sorting** to the correct topic:  
  - 🎥 `Videos & Dashcams`  
  - 📍 `Radars & Reports`  
- 🧩 **Manual validation** by admins before publication  
- 🚀 **Automatic posting** to the public group  
- 🧱 Built-in **anti-flood** and spam protection  
- ☁️ Hosted on **Render** with an integrated **keep-alive** system  

---

## 📡 Project Structure

| File | Description |
|------|--------------|
| `bot.py` | Main bot script |
| `requirements.txt` | Python dependencies |
| `Procfile` | Render startup configuration |
| `README.md` | Project documentation |
| `assets/banner.png` | GitHub banner |

---

## ⚙️ Environment Variables

| Variable | Description |
|-----------|-------------|
| `BOT_TOKEN` | Telegram bot token (from @BotFather) |
| `ADMIN_GROUP_ID` | Admin group ID for moderation |
| `PUBLIC_GROUP_ID` | Public group ID for publication |
| `KEEP_ALIVE_URL` | Render URL for the auto-ping system |

---

## 🚀 Deployment

1. Create a **Render Web Service (Free)**  
2. Connect your **GitHub repository**  
3. Add the required environment variables  
4. The bot will ping your Render instance every 10 minutes to stay active  

---

## 💬 Useful Links

- 🛰️ **Main channel:** [@Accidents_France](https://t.me/Accidents_France)  
- 👥 **Public group:** [t.me/AccidentsFR](https://t.me/AccidentsFR)  
- 🤖 **Bot:** [@AccidentsFranceBot](https://t.me/AccidentsFranceBot)

---

## 🧠 Coming Soon

- 📩 Auto DM notification when a report is published  
- 📊 Weekly stats on reports and activity  
- 🛰️ Simplified geolocation for radar and accident reports  

---

<p align="center">
  <i>Project built to centralize accident, radar and dashcam reports across France.</i><br>
  <i>🔧 Flexible bot, reusable for other communities or projects.</i><br>
  <b>Created by L.S 🇫🇷</b>
</p>
