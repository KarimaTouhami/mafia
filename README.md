# mafia - A bot that speaks Darija and hosts "imposter" game with a taste
One Imposter, a secret word, and a lot of lying - JUST FOR FUN .

### 🛠 The Mechanics
* **Word-Association Logic:** Crewmates get a secret word from `words.txt`; the Imposter gets nothing and must blend in.
* **The Punishment System:** Failed votes result in 30-second text/voice mutes. If you eject an innocent, the Imposter is granted "mic-only" dominance in the voice channel.
* **Stateful Lobbies:** Managed per-server with dedicated "Boss" (Host) permissions.
* **Production Ready:** Built with `discord.py` and designed to run as a `systemd` service for 24/7 uptime.

### 🕹 Commands
* `/join` - Enter the lobby.
* `/start` - (Host only) DMs roles/words and picks the first speaker.
* `/vote` - (Host only) Opens the 60s voting UI to eject a suspect.
* `/reset` - Wipes the state for a fresh round.

### 🚀 Setup
1. Define your token in `.env`.
2. Populate `words.txt` with your secret words.
3. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
4. Run it:
   ```bash
   python3 bot.py
   ```

---
> *Strips back the abstraction.*
