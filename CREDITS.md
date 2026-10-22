# Credits

- Enemy, equipment, vendor and recipe numbers (levels, drops, prices, stats) in `server/nobu/*.json`
  and the seed tables in the code come from the fan wiki "Yoriaisho (Honjin)" at
  wiki.ohmynobu.net/nol, kept by the game's players since 2003. Only the ids and numbers are
  kept.
- `server/nobu/town_spots.json`, `server/nobu/field_spots.json`, `server/nobu/castle_gates.json`
  and the spawn points in `server/nobu/enemies_seed.json` are positions worked out from the
  game's map geometry together with the fan wiki above. They hold coordinates only, no game data.
- **OpenSSL 1.1.1w** (OpenSSL and SSLeay licenses) and **stunnel 5.80** (GPL-2.0-or-later with an
  OpenSSL exception) are downloaded from their own sites and built when the image is built; they
  are not part of this repository.
- `server/nobu/olport.py` and `server/nobu/olpage.py` come from OpenLobby
  (https://github.com/PrettyOpenLobby/OpenLobby), copied unchanged so an OpenLobby admin panel
  can show this server. `olport.py` is dual licensed AGPL-3.0-or-later OR GPL-3.0-or-later and is
  used here under GPL-3.0-or-later; its header line "See LICENSE for the AGPL" refers to
  OpenLobby's LICENSE, as the file is copied unchanged. `olpage.py` has the same dual licence
  (its header is being added upstream).
- `hostsetup/` (UPnP, progress, setup layout) is adapted from dqx-server
  (https://github.com/PSChuze/dqx-server), GPL-3.0-or-later, same authors.
