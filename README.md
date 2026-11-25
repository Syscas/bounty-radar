# Bounty Radar

A Telegram bot that watches [Superteam Earn](https://superteam.fun/earn) and pings each user
only about the new bounties and projects that fit **their** profile.

- **Region-aware** – skips listings restricted to another country or region (EU, GCC, LATAM, North America groups supported).
- **Skill filter** – Frontend, Backend, Blockchain, Mobile, Design, Community, Growth, Content, Other.
- **Minimum reward** in USD (stablecoin rewards; other tokens are flagged).
- **Audience check** – flags listings whose judging is mostly reach/views/engagement, and hides them for users without an audience.
- `/missions` lists everything currently open that matches.

Built in plain Python: one dependency (`requests`), SQLite storage, Telegram long polling. No server or webhook needed.

## Setup

1. Create a bot with [@BotFather](https://t.me/BotFather) (`/newbot`) and copy the token.
2. `pip install -r requirements.txt`
3. Copy `.env.example` to `.env` and paste the token.
4. `python bounty_radar.py`

The first scan stores the currently open listings without alerting; afterwards only new listings trigger alerts.
`python bounty_radar.py scan-once` prints open listings with their region and audience score (handy for debugging).

## Commands (French UI)

| Command | Purpose |
|---|---|
| `/pays France` | Country of residence |
| `/competences` | Pick skills (inline buttons) |
| `/min 100` | Minimum reward in USD |
| `/audience oui\|non` | Do you have an audience? |
| `/profil` | Show profile |
| `/missions` | Open listings for you |
| `/stop` / `/start` | Pause / resume |
| `/supprimer` | Delete your data |

## Privacy

Stores only the Telegram chat id, username and the profile settings above. `/supprimer` erases them.
The bot never submits anything on a user's behalf.

## Disclaimer

Independent community tool, not affiliated with Superteam. It reads the public Superteam Earn listings API;
the audience score is a keyword heuristic, always check the listing before working on it.

## License

MIT
