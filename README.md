# discord-bracket-bot

[![CI](https://github.com/chaezuha/discord-bracket-bot/actions/workflows/ci.yml/badge.svg)](https://github.com/chaezuha/discord-bracket-bot/actions/workflows/ci.yml)

A self-hostable Discord bot for tournament-style voting brackets. Someone
creates a bracket ("Best movie snack"), the channel fills it with contenders,
and then everyone votes each matchup round by round — complete with a
rendered bracket image — until a champion is crowned.

## Features

- `/bracket create` starts a bracket in the channel; anyone can add items, or
  restrict editing to the owner plus chosen editors (`/bracket editmode`,
  `/bracket editor`) — switchable at any time
- Rounds run on a timer (`/bracket start round_minutes`), by manual advance
  (`/bracket next`, with an are-you-sure confirmation), or both
- Voting via buttons on each matchup message — your confirmation is private
  ("only visible to you"), you can change your vote until the round closes,
  and the public participation total updates live while each contender's tally
  stays hidden until the reveal to avoid bandwagoning
- The bracket is rendered as an image: a proper tournament tree with vote
  counts and highlighted winners at every level, updated as rounds finish
- Proper seeding with byes when the item count isn't a power of two
  (byes never face each other), optional shuffle at start
- Ties — including 0-0 — are settled by a fair coin flip and announced as such
- Round results and the champion announcement are public; setup and edit
  commands respond privately to avoid channel spam
- Everything is persisted in SQLite: running brackets, votes, and timers all
  survive bot restarts, and vote buttons keep working afterwards
- Bracket owners can hand over control (`/bracket transfer`); moderators
  (Manage Channels/Administrator) can always step in on any bracket
- Slash commands, no privileged intents required

## Commands

| Command                          | What it does                                                                                       |
| -------------------------------- | -------------------------------------------------------------------------------------------------- |
| `/bracket create <name> [edit_mode] [seeding]` | Create a bracket in this channel (one active bracket per channel).                   |
| `/bracket add <item>`            | Add a contender (before the bracket starts).                                                       |
| `/bracket rename <item> <new_name>` | Rename a contender (autocompletes).                                                             |
| `/bracket remove <item>`         | Remove a contender (autocompletes).                                                                |
| `/bracket items`                 | Privately list the current contenders.                                                             |
| `/bracket editmode <open\|restricted>` | Toggle whether anyone or only owner+editors may edit items.                                  |
| `/bracket editor add/remove <user>` | Grant or revoke edit access for the restricted mode.                                            |
| `/bracket start [round_minutes]` | Lock the items and start round 1. With `round_minutes`, rounds close automatically; without, only `/bracket next` advances. |
| `/bracket next`                  | Close the current round now (asks for confirmation if it's still open).                            |
| `/bracket show`                  | Re-post the bracket image (30s cooldown per channel).                                              |
| `/bracket transfer <user>`       | Hand bracket ownership to someone else.                                                            |
| `/bracket cancel`                | Cancel the bracket (asks for confirmation).                                                        |
| `/help`                          | How it all works.                                                                                  |

Everyone in the server can vote and use `/bracket show`, `/bracket items`,
and `/help`. Item editing follows the bracket's edit mode. Round control
(`start`/`next`/`cancel`/`transfer`/`editmode`/`editor`) is for the bracket
owner — and for moderators with **Manage Channels** or **Administrator**, so
a bracket can't lock up a channel if its owner disappears. To limit who can
run `/bracket create`, use Discord's built-in per-command permissions
(**Server Settings → Integrations → the bot**).

## Setup

### 1. Create the Discord application

1. Go to the [Discord Developer Portal](https://discord.com/developers/applications) and create a **New Application**.
2. Under **Bot**, click **Reset Token** and copy the token (you'll need it for `.env`). No privileged intents are needed.
3. Invite the bot to your server with this URL (replace `YOUR_CLIENT_ID` with the Application ID from **General Information**):

   ```
   https://discord.com/oauth2/authorize?client_id=YOUR_CLIENT_ID&scope=bot%20applications.commands&permissions=274877959168
   ```

   (That permission set is: View Channels, Send Messages, Send Messages in
   Threads, Embed Links, Attach Files.)

### 2. Run with Docker Compose (recommended)

Clone the repo, paste your bot token into `.env`, and start it:

```sh
git clone https://github.com/chaezuha/discord-bracket-bot.git
cd discord-bracket-bot
cp .env.example .env          # then edit .env and paste your bot token
docker compose up -d          # pulls the prebuilt GHCR image (no local build)
docker compose logs -f        # follow logs
```

Nothing is built locally — the compose file pulls the prebuilt multi-arch
image (amd64 and arm64, so a Raspberry Pi works too). Technically the only
files you need are [`compose.yaml`](compose.yaml) and a `.env` (see
[`.env.example`](.env.example)); the clone is just a convenient way to get
them. If you'd rather skip it, put those two files in any folder and run the
same `docker compose` commands there — the result is identical.

The compose file sets `restart: unless-stopped`, so the bot comes back on its
own after crashes and reboots. Bracket state lives in the `botdata` volume;
running brackets, votes, and round timers all pick up where they left off
after a restart. Run **one** bot container against that volume — SQLite is
single-writer and the bot assumes it's the only process.

To update, run `up` again (the compose file pulls the latest image on every
start):

```sh
docker compose up -d
```

#### Logs

Besides `docker compose logs`, the bot writes rotating log files (about
10 MB of recent history, including whatever led up to a crash) to a
`botlogs` volume:

```sh
docker compose exec bot tail -F logs/bot.log
```

Use `tail -F` (capital F) so following continues across log rotation. The
same directory holds `faulthandler.log`, a normally-empty file that only
receives a traceback if the process dies hard. If you'd rather have the
files directly on the host, replace the `botlogs` volume with a
`./logs:/app/logs` bind mount — but create `./logs` yourself with
permissions the container's `bot` user can write to, or the bot falls back
to console-only logging (the same goes for `botdata`/`./data`).

The compose file also runs the container with a read-only filesystem, no
capabilities, and memory/PID limits.

### Alternative: plain Docker

Same image, without Compose (again with your token in `.env`; the `-v` flag
keeps bracket state across restarts):

```sh
docker run --env-file .env -v bracketdata:/app/data ghcr.io/chaezuha/discord-bracket-bot:latest
```

Or build it yourself from a clone:

```sh
docker build -t discord-bracket-bot .
docker run --env-file .env -v bracketdata:/app/data discord-bracket-bot
```

### Alternative: run directly with Python

You'll need Python 3.10+ and, for nicer bracket images, the DejaVu fonts
(`sudo apt install fonts-dejavu-core` on Debian/Ubuntu; macOS's built-in
Arial is picked up automatically). Then:

```sh
git clone <this repo>
cd discord-bracket-bot
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env        # then edit .env and paste your bot token
python bot.py
```

### Slash-command sync

Slash commands sync automatically on startup. A global sync can take up to an
hour to show up in Discord, so set `DEV_GUILD_ID` in `.env` to your server's
ID for instant sync while testing.

## Configuration (`.env`)

| Variable        | Required | Description                                                                    |
| --------------- | -------- | ------------------------------------------------------------------------------ |
| `DISCORD_TOKEN` | yes      | Bot token from the Developer Portal.                                           |
| `DEV_GUILD_ID`  | no       | Server ID for instant slash-command sync during development.                   |
| `MAX_ITEMS`     | no       | Maximum items per bracket, 2–64 (default `32`).                                |
| `DB_PATH`       | no       | SQLite database location (default `./data/brackets.db`; the compose file mounts a volume there). |
| `LOG_DIR`       | no       | Directory for rotating log files (default `./logs`). Falls back to console-only logging if unwritable. |

## Development

```sh
pip install -r requirements-dev.txt
pytest            # logic, db-constraint, lifecycle/crash-recovery, render tests
ruff check .      # lint
ruff format .     # format
```

CI runs lint, the test suite on Python 3.10/3.12/3.14, a dependency audit
(`pip-audit`), and a Docker build + container smoke test on every push and
PR. Pushes to `main` and `v*` tags publish a multi-arch image to GHCR only
after all of those pass — the amd64 image that was smoke-tested is what gets
pushed.

## Notes

- Commands are server-only. Threads work too, as long as the bot has the
  Send Messages in Threads permission from the invite URL above.
- Round closing is atomic and idempotent: winners, coin flips, and the next
  round's pairings are persisted before anything is posted, so a crash or
  restart mid-round never rerolls a result or advances a bracket twice. If
  the bot was down past a round's deadline, the round closes on startup and
  the next one gets its full configured duration.
- If the bracket's channel is deleted (or the bot loses access to it), the
  bracket is cancelled automatically instead of wedging the scheduler.
- The bracket image uses DejaVu/Arial, which covers Latin, Cyrillic, and
  Greek; emoji or CJK in item names show as boxes in the image (they render
  fine in the messages themselves).
- Coin flips use OS entropy (`random.SystemRandom`), and the result is
  persisted before it's announced — retries can't change an outcome.
