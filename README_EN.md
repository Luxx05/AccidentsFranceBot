<p align="center">
  🇫🇷 <a href="./README.md">Français</a> │ 🇬🇧 <b>English</b>
</p>

![Banner](https://github.com/Luxx05/AccidentsFranceBot/raw/main/assets/banner.png)

<h1 align="center">🚨 Accidents France Bot</h1>
<p align="center">
  <b>Automated Telegram bot for the Accidents France community.</b><br>
  Anonymous submission, advanced moderation, smart topic sorting, and a persistent database.
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

## 🔧 Key Features

### 👤 Submission (via Private Bot)
- 📸 **Anonymous submission** of videos, photos, albums, and text reports.
- 📬 **Author notification** when their submission is approved and published.
- ⛔ **Author notification** if their submission is rejected and they are muted.
- 🛡️ **Mute check**: The bot refuses submissions from a muted user.
- 🧱 **Simple anti-flood** for private submissions.

### 🛡️ Admin Group
- 🧩 **Manual moderation** by administrators before publication.
- ✏️ **"Edit" button** to rewrite a post's caption before publishing (handles admin anonymity).
- 🔇 **"Reject & Mute 1h" button** to reject a submission and mute the author for 1 hour.
- ❌ **`/cancel` command** to abort an ongoing edit.
- 🚀 **Admin shortcut `/deplacer`**: Post a message directly from the admin group to the correct public topic.
- 🧹 **Automatic cleanup** of service messages (e.g., "X joined the group").

### 📢 Public Group
- 🧠 **Smart sorting** of approved submissions into the correct topic:
  - 🎥 `Vidéos & Dashcams`
  - 📍 `Radars & Signalements`
  - #️⃣ `Général` (default)
- ⚙️ **Admin command `/deplacer`** to move a misplaced message into the correct topic (handles admin anonymity).
- 🔇 **Automatic moderation**:
  - **Anti-spam** (deletes messages sent too quickly).
  - **Anti-gibberish** (deletes meaningless messages).
  - **Auto-mute** (restricts group spammers for 5 minutes).
- 🧹 **Automatic cleanup** of service messages (group photo changes, etc.).
- 🤖 **Command menu** `/` displaying admin actions (`/deplacer`, `/cancel`).

### ⚙️ Backend
- 🗃️ **Persistent Database (SQLite)**: No data loss for pending submissions, edit states, or muted users, even if the bot restarts.
- ☁️ Hosted on **Render** with a **keep-alive** system (via Flask).

---

## 📡 Project Structure

| File | Description |
|----------|-------------|
| `bot.py` | Main bot script |
| `requirements.txt` | Python dependencies (Telegram, aiosqlite, flask, requests) |
| `Procfile` | Render configuration |
| `README.md` | Project documentation (FR) |
| `README_EN.md` | Project documentation (EN) |
| `assets/banner.png` | GitHub banner |

---

## ⚙️ Environment Variables

| Variable | Description |
|-----------|--------------|
| `BOT_TOKEN` | Telegram bot token (from @BotFather) |
| `ADMIN_GROUP_ID` | ID of the private moderation group |
| `PUBLIC_GROUP_ID` | ID of the public group |
| `KEEP_ALIVE_URL` | Render URL for the automatic ping |
| `DB_PATH` | **[Required]** Path to the DB file (e.g., `/var/data/bot_storage.db` on Render) |

---

## 🚀 Deployment

1. Create a **Render Web Service (Free)**.
2. Connect your **GitHub repo**.
3. Add the **Environment Variables** listed above.
4. **Important:** Add a **"Persistent Disk"** on Render (e.g., mount point `/var/data`) and use this path for the `DB_PATH` variable to prevent data loss.
5. The bot pings itself every 10 minutes to stay active.

---

## 💬 Useful Links

- 🛰️ **Main Channel:** [@Accidents_France](https://t.me/Accidents_France)
- 👥 **Public Group:** [t.me/AccidentsFR](https://t.me/AccidentsFR)
- 🤖 **Bot:** [@AccidentsFranceBot](https://t.me/AccidentsFranceBot)

---

## 🧠 Future Features

- 📊 Weekly statistics on submissions.
- 🛰️ Simplified geolocation for radars and accidents.
- 📂 Add support for moving (`/deplacer`) full albums.
- 🛡️ `/report` command for public group members.

---

<p align="center">
  <i>Project developed to centralize accident reports, radar sightings, and dashcam videos in France.</i><br>
  <i>🔧 Flexible bot, reusable for other communities or projects.</i><br>
  <b>Created by L.S 🇫🇷</b>
</p>
