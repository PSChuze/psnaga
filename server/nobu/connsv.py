"""Connect server (TCP 9070): answer the client's update check, log everything.

The client dials the host:port in NBCONNSV.BIN and, with "Checking for updates" on screen,
sends a 0x15 version check (nobu.vce.decode_version_check). We answer with a 0x22 "no patch"
reply (nobu.vce.up_to_date_reply). The reply also carries the host/path the client later uses
for its HTTPS login ("https://%s?a=%s"), so that is configurable here.

Every session gets a hex-dump log under $NOBU_LOG_DIR/sessions (both directions, one decoded
line per frame) and open/frame/close lines in events.jsonl.

Env:
  NOBU_CONNSV_PORTS  comma list of TCP ports (default 9070)
  NOBU_BIND          bind address (default 0.0.0.0)
  NOBU_LOG_DIR       log root (default ./logs)
  NOBU_IDLE_SECS     close a silent session after this long (default 600)
  NOBU_LOGIN_HOST    host[:port][/path] for the client's HTTPS login (the client parses :port) (default nobol.koei.co.jp/auth)
  NOBU_UPDATE_REPLY  hex payload to send instead of the built reply (probe knob; no framing)
  NOBU_WORLDS        worlds sent after the login (0x14): "Name@host:port,..." (default
                     "Nobunaga@127.0.0.1:9100")
  NOBU_ADVERTISE_MAP per-client world host, "cidr=addr,..." (first match on the client's
                     source address wins; no match or unset = the host in NOBU_WORLDS).
                     e.g. 192.168.0.0/16=<LAN address>,0.0.0.0/0=<internet address>.
                     Never self-discovered.
  NOBU_WORLD_STATUS  status byte for every world (entry+0x8c). 0 = Offline (not selectable), 1 = Normal (selectable); 2/3 presumably Busy/Crowded. Default 1.
"""
import asyncio
import datetime as dt
import ipaddress
import json
import os
import sys

from . import vce

BIND = os.environ.get("NOBU_BIND", "0.0.0.0")
PORTS = [int(p) for p in os.environ.get("NOBU_CONNSV_PORTS", "9070").split(",") if p.strip()]
LOG_DIR = os.environ.get("NOBU_LOG_DIR", os.path.join(os.getcwd(), "logs"))
IDLE = float(os.environ.get("NOBU_IDLE_SECS", "600"))
LOGIN_HOST = os.environ.get("NOBU_LOGIN_HOST", "nobol.koei.co.jp/auth")
UPDATE_REPLY = bytes.fromhex(os.environ.get("NOBU_UPDATE_REPLY", ""))
WORLDS = vce.parse_worlds(os.environ.get("NOBU_WORLDS", "Nobunaga@127.0.0.1:9100"))
WORLD_STATUS = int(os.environ.get("NOBU_WORLD_STATUS", "1"))
for _w in WORLDS:
    _w["status"] = WORLD_STATUS


def parse_advertise_map(spec):
    """NOBU_ADVERTISE_MAP: "cidr=addr,..." -> [(network, addr)], order kept (first match wins)."""
    out = []
    for item in (x.strip() for x in spec.split(",")):
        if item:
            net, _, addr = item.partition("=")
            out.append((ipaddress.ip_network(net.strip(), strict=False), addr.strip()))
    return out


ADVERTISE = parse_advertise_map(os.environ.get("NOBU_ADVERTISE_MAP", ""))


def worlds_for(peer, worlds=None, amap=None):
    """The world list with each host replaced by the address this client can reach."""
    worlds = WORLDS if worlds is None else worlds
    amap = ADVERTISE if amap is None else amap
    try:
        ip = ipaddress.ip_address(peer)
    except ValueError:
        return worlds
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    for net, addr in amap:
        if ip.version == net.version and ip in net:
            return [dict(w, host=addr) for w in worlds]
    return worlds


def now():
    return dt.datetime.now(dt.timezone.utc)


def hexdump(data, width=16):
    out = []
    for i in range(0, len(data), width):
        chunk = data[i:i + width]
        hx = " ".join(f"{b:02x}" for b in chunk)
        asc = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        out.append(f"  {i:06x}  {hx:<{width * 3}} {asc}")
    return "\n".join(out)


def event(**kw):
    kw["t"] = now().isoformat()
    with open(os.path.join(LOG_DIR, "events.jsonl"), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(kw) + "\n")
    print(json.dumps(kw), flush=True)


def answer(payload, peer=None):
    """Reply payloads for one client payload (unframed); peer = the client's source address."""
    if payload and payload[0] == vce.CMD_VERSION_CHECK:
        if UPDATE_REPLY:
            return [UPDATE_REPLY]
        return [vce.up_to_date_reply(vce.decode_version_check(payload), LOGIN_HOST)]
    if payload and payload[0] == vce.CMD_REQ_14:
        vce.decode_req_14(payload)
        return vce.world_list(worlds_for(peer))
    if payload and payload[0] == vce.CMD_REQ_16:
        # HYPOTHESIS: 0x16 asks for one world's state; 0x23 updates entry+0x8c and its UI row.
        key, wid, sub = vce.decode_req_16(payload)[:3]
        status = next((w["status"] for w in WORLDS if w["wid"] == wid), vce.WORLD_OFFLINE)
        return [vce.encode_world_status(key, wid, sub, status)]
    return []


async def session(reader, writer, port):
    peer = writer.get_extra_info("peername") or ("?", 0)
    sid = f"{now():%Y%m%dT%H%M%S%f}_{peer[0]}_{peer[1]}_p{port}"
    path = os.path.join(LOG_DIR, "sessions", sid + ".log")
    event(ev="open", sid=sid, peer=peer[0], port=port, svc="connsv")
    total = 0
    frames = vce.FrameReader()
    with open(path, "a", encoding="utf-8") as log:
        def record(direction, data, note=""):
            log.write(f"{now().isoformat()} {direction} {len(data)} bytes{note}\n{hexdump(data)}\n")
            log.flush()

        try:
            while True:
                data = await asyncio.wait_for(reader.read(65536), IDLE)
                if not data:
                    break
                total += len(data)
                record("C>S", data)
                for payload in frames.feed(data):
                    what = vce.describe(payload)
                    log.write(f"  = {what}\n")
                    event(ev="frame", sid=sid, dir="C>S", cmd=payload[0] if payload else None,
                          hex=payload.hex(), what=what)
                    try:
                        replies = answer(payload, peer[0])
                    except vce.WireError as e:
                        log.write(f"  ! cannot answer: {e}\n")
                        replies = []
                    for rep in replies:
                        wire = vce.frame(rep)
                        writer.write(wire)
                        await writer.drain()
                        record("S>C", wire, f"  = {vce.describe(rep)}")
                        event(ev="frame", sid=sid, dir="S>C", cmd=rep[0], hex=rep.hex(),
                              what=vce.describe(rep))
                if frames.buf:
                    log.write(f"  (partial frame buffered: {len(frames.buf)} bytes)\n")
        except asyncio.TimeoutError:
            log.write(f"{now().isoformat()} idle timeout\n")
        except ConnectionError as e:
            log.write(f"{now().isoformat()} {e!r}\n")
        log.write(f"{now().isoformat()} closed\n")
    writer.close()
    event(ev="close", sid=sid, bytes_in=total)


async def main():
    os.makedirs(os.path.join(LOG_DIR, "sessions"), exist_ok=True)
    servers = []
    for port in PORTS:
        servers.append(await asyncio.start_server(
            lambda r, w, p=port: session(r, w, p), BIND, port))
        print(f"connsv: listening on {BIND}:{port}, login host {LOGIN_HOST!r}, logs in {LOG_DIR}",
              file=sys.stderr, flush=True)
    await asyncio.gather(*(s.serve_forever() for s in servers))


if __name__ == "__main__":
    asyncio.run(main())
