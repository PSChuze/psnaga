# psnaga

A reference implementation of the servers behind *Nobunaga no Yabou Online - Hiryuu no Shou*
(Nobunaga's Ambition Online) on the PlayStation 2, and a base for hosting your own.

**This server is very early. Most things in the game do not work yet.** You can log in, make a
character, walk the towns and fields, talk to some NPCs, shop, chat and form parties; much of the
rest is missing or rough. Only a real PS2 is supported for now.

It answers what the game connects to: the connect server (update check and world list), the
GAMECITY login over the TLS the game speaks, and the world server, in Python with no dependencies
outside the standard library.

No game data is included. The server builds what it needs from your own *Hiryuu no Shou* disc
(SLPM-65197) on your machine (`extract/`); our English names ship as an overlay keyed by id.

## Quick start

1. Download **Nobunaga Server Setup** for your computer from the [Releases](../../releases) page:
   `Nobunaga-Server-Setup.exe` (Windows), `Nobunaga-Server-Setup-mac.zip` (Mac with Apple
   silicon) or `Nobunaga-Server-Setup-linux-x86_64`.
2. Install [Docker Desktop](https://www.docker.com/products/docker-desktop/) (on Linux, Docker
   Engine) and start it.
3. Double-click Nobunaga Server Setup and answer three questions: who will play, where your disc
   image is, and the server's name.

Setup does the rest: it reads your disc, starts the shared DNS server if there is none, starts
the server, asks your router to open the ports if friends play over the internet, and tells you
what to tell your players. Run it again for a menu: start and stop, settings, update, backups,
accounts, logs.

From a clone of this repository, `python setup.py` does the same (Python 3.8 or newer).

You need an image (`.iso`) of the *Hiryuu no Shou* disc, or a zip of one. Other pressings do not
work.

## Updating

Any of these updates the server to the newest release, keeping your settings, accounts and
characters. The game data is built again from your disc only when the release changed how it is
built, and only the parts of the server that were running are started again.

- In a paired OpenLobby admin panel, press Update once the panel's owner has approved one-click
  updates for this game (Settings > Modules).
  Installs of v1.0.0 must run Nobunaga Server Setup and choose Update once; after that the panel's
  Update button works.
- Run Nobunaga Server Setup and choose Update.
- In the server folder: `python setup.py --update --yes` (it never asks anything; the last line
  says what happened, and it exits non-zero when the update failed).

The panel shows when a newer release is out. `NOBU_UPDATE_REPO` in `.env` points that check at
another repository, or turns it off when empty.

## Players

Players need a game install on a PS2 hard disk whose network settings and DNS point at this
server; setup shows the address to use.

The game finds the servers by name. The shared DNS server answers these with your address:

| name | what |
|---|---|
| `nobol.koei.co.jp` | connect server (TCP 9070) and login (TCP 9443) |
| `testconsv1.ax.koei.co.jp` | connect server, for installs set up to use it |

The world server (TCP 9110) is reached by address. Citizen IDs are 1 to 8 lowercase letters; by
default the first login with a new ID makes the account.

## Ports

Players must reach TCP 9070, 9443 and 9110. Setup opens them on the router with UPnP when it can,
and otherwise lists them. 9070 is fixed in the game; the login and world ports are handed to the
game, so `NOBU_LOGIN_PORT` and `NOBU_WORLD_PORT` can move them.

The shared DNS server (OpenLobby's `dns` service, the container labelled
`org.openlobby.role=dns`) answers home networks only by default. For friends over the internet,
either give them DNS entries of their own for the names above, or allow their networks in the DNS
server's `POL_DNS_CLIENTS` setting and open UDP 53. An open DNS server on the internet gets abused,
so allow only the networks you need.

## Running it by hand

```sh
cp .env.example .env                  # fill in NOBU_PUBLIC_IP and NOBU_ADMIN_TOKEN
mkdir discs                           # put the disc image in it (or set NOBU_DISCS)
docker network inspect openlobby >/dev/null 2>&1 || docker network create openlobby
docker compose run --rm extract       # builds the game data into state/assets
docker compose up -d --build
docker exec <the dns container> python stub.py dns-names add nobu nobol.koei.co.jp testconsv1.ax.koei.co.jp
```

The names above must lead to this computer: register them with the shared DNS server as shown, or
with a DNS server of your own. The login's TLS certificate is made on the first start in
`state/certs`. Accounts, characters and news are kept under `state/data/` (with rotating backups
in `state/data/backups/`), every session is logged under `state/logs/`.

Accounts by hand: `docker compose exec auth python -m nobu.auth add <id> <password>` (also resets
a password) and `... list`. Data checks and restores: `docker compose exec world python -m
nobu.admin validate | backup | list | restore <backup>` (stop the world first to restore
`chars.json`).

Gameplay switches (battles, field enemies, NPC groups) are in `.env.example`; each module's
docstring lists its settings.

## OpenLobby admin panel

The server can be run from an [OpenLobby](https://github.com/PrettyOpenLobby/OpenLobby) admin
panel on the same computer: who is online and where, the clans and the war, the war weeks on the
calendar, announcements, kicking a player, the in-game support reports as a help desk, the news
and the backups. Setup finds a running panel and pairs with it by the code the panel shows.
Without pairing, give the panel `POL_ADMIN_GAME_NOBU_URL=http://nobu:19111` and
`POL_ADMIN_GAME_NOBU_TOKEN` (`NOBU_ADMIN_TOKEN` from `.env`). The admin port is only on the
`openlobby` Docker network and is never published.

## Development

```sh
cd server && python -m unittest discover -s . -p "test_*.py" -t .   # the server
python -m unittest discover -s tests -t .                           # setup and extract
python -m extract --disc <image.iso> --out assets                   # game data for a clone
```

Without game data the server uses the English overlay alone and the tests that need the disc
skip themselves; with `NOBU_ASSETS=assets` they run too.

## Credits and license

See [CREDITS.md](CREDITS.md). GPL-3.0-or-later (see [LICENSE](LICENSE)).

Nobunaga no Yabou Online, Nobunaga's Ambition, GAMECITY and KOEI are trademarks of KOEI TECMO GAMES.
PlayStation is a trademark of Sony Interactive Entertainment. This is a fan project, not affiliated
with or endorsed by either.
