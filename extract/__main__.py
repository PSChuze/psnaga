"""python -m extract [--disc PATH] [--out DIR] [--overlay DIR]

Reads the Hiryuu no Shou disc (an .iso, or a folder with the disc's files) and writes:
  <out>/text/<table>.tsv          the name tables the server reads (the disc's text and our English)
  <out>/maps/province_gates.json  the checkpoints between the province fields
  <out>/manifest.json             what was built, from which disc

Without --disc it takes the first disc image in $NOBU_DISC_FILES (one path per line, as setup
passes them) or in /discs. Each step prints "<step>: <seconds>s" when it is done.
"""
import argparse
import json
import os
import sys
import time

from . import disc as disclib
from . import maps, text

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VERSION = 1


def find_disc(args):
    if args.disc:
        return args.disc
    listed = [p.strip() for p in os.environ.get("NOBU_DISC_FILES", "").splitlines() if p.strip()]
    if listed:
        return listed[0]
    folder = "/discs"
    if os.path.isdir(folder):
        for base, _dirs, files in os.walk(folder):
            for name in sorted(files):
                if name.lower().endswith(".iso"):
                    return os.path.join(base, name)
        if os.path.exists(os.path.join(folder, "SYSTEM.CNF")):
            return folder
    sys.exit("No disc given: pass --disc <image.iso or folder>, or put the image in ./discs")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m extract")
    ap.add_argument("--disc", help="the disc image (.iso) or a folder with the disc's files")
    ap.add_argument("--out", default=os.environ.get("NOBU_ASSETS") or os.path.join(HERE, "assets"))
    ap.add_argument("--overlay", default=os.environ.get("NOBU_OVERLAY") or os.path.join(HERE, "overlay"))
    args = ap.parse_args(argv)
    path = find_disc(args)
    t0 = time.monotonic()
    try:
        d = disclib.Disc(path)
        d.check()
        print("disc: %s" % path, flush=True)
        wanted = ["data/STRDAT.BIN", "data/ui/WDMMSG.BIN"] + [maps.cmap_name(m) for m in maps.PROVINCES]
        files = disclib.game_files(d, wanted)
        d.close()
    except (disclib.DiscError, OSError) as e:
        sys.exit("extract: %s" % e)
    print("read_disc: %.1fs" % (time.monotonic() - t0), flush=True)

    t = time.monotonic()
    done = text.build(files, os.path.join(args.overlay, "text"), os.path.join(args.out, "text"))
    print("text: %.1fs" % (time.monotonic() - t), flush=True)

    t = time.monotonic()
    os.makedirs(os.path.join(args.out, "maps"), exist_ok=True)
    doc = maps.province_gates(files)
    with open(os.path.join(args.out, "maps", "province_gates.json"), "w", encoding="utf-8", newline="\n") as fh:
        json.dump(doc, fh, indent=1)
        fh.write("\n")
    print("maps: %.1fs" % (time.monotonic() - t), flush=True)

    manifest = {"version": VERSION, "disc": os.path.basename(path.rstrip("/\\")), "boot": disclib.BOOT2,
                "tables": done, "province_exits": len(doc["exits"]),
                "built": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    with open(os.path.join(args.out, "manifest.json"), "w", encoding="utf-8", newline="\n") as fh:
        json.dump(manifest, fh, indent=1)
        fh.write("\n")
    print("extract: done in %.0fs" % (time.monotonic() - t0), flush=True)


if __name__ == "__main__":
    main()
