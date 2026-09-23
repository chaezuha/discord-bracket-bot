# discord-bracket-bot

[![CI](https://github.com/chaezuha/discord-bracket-bot/actions/workflows/ci.yml/badge.svg)](https://github.com/chaezuha/discord-bracket-bot/actions/workflows/ci.yml)

A self-hostable Discord bot for tournament-style voting brackets, built on
discord.py and SQLite. Someone creates a bracket ("Best movie snack"), the
channel fills it with contenders, and then everyone votes on each matchup,
round by round, until a champion is crowned.

<!-- Screenshot suggestion: a rendered bracket image next to a voting board. -->

## Features

- **Build brackets together:** Anyone in the channel can add contenders, or you can restrict editing to the owner and chosen editors. You can switch modes at any time.
- **Flexible rounds:** Rounds close on a timer, when the owner advances them manually, or both.
- **Compact voting boards:** Each message holds up to 10 matchups with numbered buttons. You can change your vote until the round closes, and your vote receipt is private.
- **No bandwagoning:** Everyone sees how many people have voted, but each contender's tally stays hidden until the round's results are revealed.
- **Bracket image:** A rendered tournament tree with vote counts and highlighted winners at every level, updated after each round.
- **Fair seeding and ties:** Brackets of any size get byes where needed (byes never face each other), with optional shuffling at start. Ties, including 0-0, are settled by an announced coin flip.
- **Survives restarts:** Running brackets, votes, and timers are saved in SQLite, and vote buttons keep working after the bot restarts.
- **Works anywhere:** Servers, threads, DMs with the bot, friend DMs, and group DMs. Slash commands only, no privileged intents.

## Quick start

You need [Docker](https://docs.docker.com/get-docker/) with Compose and a Discord account.

1. **Create the Discord application.**
   1. In the [Discord Developer Portal](https://discord.com/developers/applications), create a **New Application**.
   2. Under **Bot**, click **Reset Token** and copy the token. No privileged intents are needed.
   3. Under **Installation**, enable both **Guild Install** and **User Install**, and select **Discord Provided Link**. Set these defaults, then save:
      - **User Install:** `applications.commands`
      - **Guild Install:** `applications.commands` and `bot`, with View Channels, Send Messages, Send Messages in Threads, Embed Links, and Attach Files
   4. Open the install link from that page. Choose **Add to server** for server use, and **Add to my apps** for DMs and group DMs (see [Where it works](#where-it-works)).
2. **Start the bot.**

   ```sh
   git clone https://github.com/chaezuha/discord-bracket-bot.git   # get compose.yaml and .env.example
   cd discord-bracket-bot                                           # enter the project folder
   cp .env.example .env                                             # create your config, then paste your token after DISCORD_TOKEN=
   docker compose up -d                                             # pull the prebuilt image and start in the background
   docker compose logs -f                                           # watch the logs (Ctrl+C to stop watching)
   ```

That's it. In any channel the bot can see, run `/bracket create Best movie snack`
and add a few items with `/bracket add`.

> [!TIP]
> **Commands don't show up?** New slash commands can take up to an hour to
> appear in Discord. For instant commands while testing, set `DEV_GUILD_ID` to
> your server's ID in `.env` and run `docker compose up -d` again.

The prebuilt image supports amd64 and arm64, so a Raspberry Pi works too. The
clone is only a convenient way to get [`compose.yaml`](compose.yaml) and
[`.env.example`](.env.example). You can put those two files in any folder
instead and run the same commands there.

## Usage

### Commands

| Command | What it does |
| --- | --- |
| `/bracket create <name> [edit_mode] [seeding]` | Create a bracket in this channel or conversation (one active bracket per channel). `edit_mode` is `open` (default) or `restricted`. `seeding` is `order` (default) or `shuffle`. |
| `/bracket add <item>` | Add a contender (before the bracket starts). |
| `/bracket rename <item> <new_name>` | Rename a contender (autocompletes). |
| `/bracket remove <item>` | Remove a contender (autocompletes). |
| `/bracket items` | Privately list the current contenders. |
| `/bracket editmode <open\|restricted>` | Choose whether anyone, or only the owner and editors, may edit items. |
| `/bracket editor add <user>` / `/bracket editor remove <user>` | Grant or revoke edit access for restricted mode. |
| `/bracket start [round_minutes]` | Lock the items and start round 1. With `round_minutes` (1 to 10080), rounds close automatically. Without it, only `/bracket next` advances. |
| `/bracket next` | Close the current round now (asks for confirmation if it's still open). |
| `/bracket show` | Re-post the bracket image (30 second cooldown per channel). |
| `/bracket transfer <user>` | Hand bracket ownership to someone else. |
| `/bracket cancel` | Cancel the bracket (asks for confirmation). |
| `/help` | How it all works. |

Round results and the champion announcement are public. Setup and edit
commands reply privately so the channel doesn't fill with noise.

### What you see while voting

- Each round posts a voting board with up to 10 matchups per message. A
  16-entry bracket opens with two messages (image plus one board). A 64-entry
  bracket opens with five (image plus four boards).
- Pick a numbered button to vote. Your private receipt ("only visible to you")
  disappears after 8 seconds.
- Vote totals on each board refresh at most once per second.
- When a round closes, server and bot-DM boards show their results in place.
  Friend and group DMs get separate result summaries.

### Permissions

Everyone who can see a matchup can vote. Item editing follows the bracket's
edit mode. Anyone with the app installed can use `/bracket show`,
`/bracket items`, and `/help`.

The exceptions are round control commands (`start`, `next`, `cancel`,
`transfer`, `editmode`, and `editor`). Only the bracket owner can use them,
plus server moderators with **Manage Channels** or **Administrator**, so a
bracket can't lock up a channel if its owner disappears.

To limit who can run `/bracket create`, use Discord's per-command permissions
(**Server Settings → Integrations → the bot**).

### Where it works

| Place | Install needed | Notes |
| --- | --- | --- |
| Servers and threads | **Add to server** | Timers and manual rounds. Threads need the Send Messages in Threads permission. |
| DMs with the bot | **Add to my apps** | Timers and manual rounds. |
| Friend DMs and group DMs | **Add to my apps** | Manual rounds only (`/bracket next`). There are no moderators, so only the owner has round control. Anyone in the conversation with the app installed can edit an open bracket. |

If you use your user-installed commands in a server that hasn't added the bot,
the bot tells you the server needs to install it first.

The bracket image supports Latin, Cyrillic, and Greek text. Emoji and CJK
characters in item names show as boxes in the image, but display normally in
messages.

## Configuration

Set these in `.env` (see [`.env.example`](.env.example)).

| Variable | Required | What it does |
| --- | --- | --- |
| `DISCORD_TOKEN` | yes | Bot token from the Developer Portal. |
| `DEV_GUILD_ID` | no | Server ID that gets an instant copy of the slash commands while testing. Global commands still sync as usual. Default: unset. |
| `MAX_ITEMS` | no | Maximum items per bracket, from 2 to 64. Default: `32`. |
| `DB_PATH` | no | SQLite database location. Default: `./data/brackets.db` (the compose file mounts a volume there). |
| `LOG_DIR` | no | Directory for rotating log files. If it isn't writable, the bot logs to the console only. Default: `./logs`. |

> [!WARNING]
> Keep `.env` private. Anyone with `DISCORD_TOKEN` can control your bot. If it
> leaks, click **Reset Token** in the Developer Portal and update `.env`.
>
> Run only **one** bot process against a database. SQLite is single-writer and
> the bot assumes it is the only process.

## Other ways to run

### Plain Docker

Use the same prebuilt image without Compose. Put your token in `.env` first.
The `-v` flag keeps bracket state across restarts.

```sh
docker run --env-file .env -v bracketdata:/app/data ghcr.io/chaezuha/discord-bracket-bot:latest
```

### Build the image yourself

From a clone of the repo:

```sh
docker build -t discord-bracket-bot .                                  # build the image locally
docker run --env-file .env -v bracketdata:/app/data discord-bracket-bot  # run it with persistent data
```

### Run directly with Python

You need Python 3.10 or newer. For nicer bracket images, install the DejaVu
fonts (`sudo apt install fonts-dejavu-core` on Debian/Ubuntu). On macOS, the
built-in Arial is used automatically.

```sh
git clone https://github.com/chaezuha/discord-bracket-bot.git   # get the code
cd discord-bracket-bot                                           # enter the project folder
python3 -m venv .venv                                            # create a virtual environment
source .venv/bin/activate                                        # activate it (Windows: .venv\Scripts\activate)
pip install -r requirements.txt                                  # install dependencies
cp .env.example .env                                             # create your config, then paste your token
python bot.py                                                    # start the bot
```

## Running it long-term

These steps assume the [Quick start](#quick-start) Docker Compose setup.

### Updating

The compose file pulls the latest image every time it starts, so updating is:

```sh
docker compose up -d
```

### Restarts

The compose file sets `restart: unless-stopped`, so the bot comes back on its
own after crashes and reboots. Running brackets, votes, and round timers pick
up where they left off. If the bot was down past a round's deadline, that
round closes on startup and the next one gets its full duration.

### Logs

Follow the console output with `docker compose logs -f`. The bot also writes
rotating log files (about 12 MB of recent history, including whatever led up
to a crash) to the `botlogs` volume:

```sh
docker compose exec bot tail -F logs/bot.log
```

Use `tail -F` (capital F) so following continues across log rotation. The same
directory holds `faulthandler.log`, which stays empty unless the process dies
hard.

To keep the files on the host instead, replace the `botlogs` volume in
`compose.yaml` with a `./logs:/app/logs` bind mount. Create `./logs` yourself
with permissions the container's `bot` user can write to, or the bot falls back
to console-only logging. The same applies if you swap `botdata` for `./data`.

### Backups

All bracket state lives in the `botdata` volume. Stop the bot first so the
database is idle, then copy it out:

```sh
docker compose stop                                                  # stop the bot so the database isn't being written
docker cp "$(docker compose ps -aq bot)":/app/data ./bracket-backup  # copy the data folder to ./bracket-backup
docker compose start                                                 # start the bot again
```

### Security hardening

The compose file already runs the container with a read-only filesystem, all
capabilities dropped, `no-new-privileges`, and memory and PID limits. The bot
runs as a non-root `bot` user. Only the data volume, the log volume, and `/tmp`
are writable. See the [Configuration warning](#configuration) for keeping your
token safe.

## Development

```sh
pip install -r requirements-dev.txt   # install test and lint tools
pytest                                # run the test suite
ruff check .                          # lint
ruff format .                         # format
```

CI runs on every push and pull request: lint and format checks, tests on
Python 3.10, 3.12, and 3.14, a dependency audit (`pip-audit`), and a Docker
build with a container smoke test. Pushes to `main` and `v*` tags publish a
multi-arch image to GHCR only after all of those pass.
