"""Finding the other installs on this machine: the shared DNS and an OpenLobby panel.

They find each other through Docker labels and one shared Docker network, `openlobby`:

  org.openlobby.role=dns         the one DNS server every install shares (OpenLobby's image)
  org.openlobby.role=panel       an OpenLobby admin panel, with org.openlobby.panel.url

Every function takes `run`, which runs a docker command and returns (exit code, output); the
tests pass a fake one.
"""
import json
import os
import subprocess
import time
import urllib.error
import urllib.request

NETWORK = "openlobby"
DNS_IMAGE = "ghcr.io/prettyopenlobby/openlobby:latest"
TOO_OLD = ("The shared DNS server's OpenLobby image is too old for this (it has no 'dns-names'). "
           "Update it: docker pull %s, then restart that container." % DNS_IMAGE)

# OpenLobby's dns service on its own, for a machine with no OpenLobby (docs/installs.md there).
DNS_COMPOSE = """\
# The DNS server every game install on this machine shares (OpenLobby's dns service).
# Started by the first setup program that found none; the others only add their names.
name: openlobby-dns
services:
  dns:
    image: %(image)s
    command: ["dns"]
    restart: unless-stopped
    labels:
      org.openlobby.role: dns
    environment:
      POL_ADVERTISE: "%(address)s"
    ports:
      - "0.0.0.0:53:53/udp"
    volumes:
      - dns-data:/data
volumes:
  dns-data:
"""


def run_docker(args, timeout=600, cwd=None):
    """Run `docker <args>` -> (exit code, its output)."""
    try:
        r = subprocess.run(["docker"] + list(args), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           text=True, encoding="utf-8", errors="replace", timeout=timeout, cwd=cwd)
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)
    return r.returncode, r.stdout or ""


def running(label, run=run_docker):
    """Names of the running containers with this label (key=value)."""
    code, out = run(["ps", "--filter", "label=" + label, "--format", "{{.Names}}"])
    return [n.strip() for n in out.splitlines() if n.strip()] if code == 0 else []


def label_of(container, key, run=run_docker):
    code, out = run(["inspect", "--format", '{{index .Config.Labels "%s"}}' % key, container])
    value = out.strip() if code == 0 else ""
    return "" if value == "<no value>" else value


def ensure_network(run=run_docker):
    """Create the shared network when it is missing (it is never removed); True when it is there."""
    if run(["network", "inspect", NETWORK])[0] == 0:
        return True
    return run(["network", "create", NETWORK])[0] == 0


# --- the shared DNS ----------------------------------------------------------------------------

def find_dns(run=run_docker):
    names = running("org.openlobby.role=dns", run)
    return names[0] if names else None


def start_dns(folder, address, run=run_docker):
    """Start OpenLobby's dns service from `folder` (made if missing); -> its container or None."""
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, "compose.yml")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(DNS_COMPOSE % {"image": DNS_IMAGE, "address": address})
    code, out = run(["compose", "-f", path, "up", "-d"], cwd=folder)
    if code != 0:
        return None
    for _ in range(20):
        name = find_dns(run)
        if name:
            return name
        time.sleep(1)
    return None


def too_old(code, out):
    """True when stub.py answered with its usage line: an image from before dns-names."""
    return code != 0 and "usage" in out.lower() and "dns-names" not in out.lower()


def dns_names(container, key, names, run=run_docker):
    """Answer `names` (and their subdomains) with this machine's address -> (ok, message)."""
    code, out = run(["exec", container, "python", "stub.py", "dns-names", "add", key] + list(names),
                    timeout=60)
    if code == 0:
        return True, out.strip()
    return False, TOO_OLD if too_old(code, out) else (out.strip() or "dns-names failed (exit %d)" % code)


def dns_remove(container, key, run=run_docker):
    code, out = run(["exec", container, "python", "stub.py", "dns-names", "remove", key], timeout=60)
    if code == 0:
        return True, out.strip()
    return False, TOO_OLD if too_old(code, out) else out.strip()


# --- an OpenLobby admin panel --------------------------------------------------------------------

def find_panel(run=run_docker):
    """The address of a running OpenLobby panel, or None."""
    for name in running("org.openlobby.role=panel", run):
        url = label_of(name, "org.openlobby.panel.url", run)
        if url:
            return url.rstrip("/")
    return None


def _post(url, doc, opener):
    req = urllib.request.Request(url, data=json.dumps(doc).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with opener(req, timeout=15) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {}
    except (OSError, ValueError) as e:
        return 0, {"error": e.__class__.__name__}


def pair(panel_url, code, key, title, url, token, opener=urllib.request.urlopen):
    """Pair with the panel by its one-time code (sent as typed) -> (True, pairing secret) or (False, why)."""
    status, doc = _post(panel_url + "/api/pair", {"code": code.strip(), "key": key, "title": title,
                                                  "url": url, "token": token}, opener)
    if status == 200 and doc.get("ok") and doc.get("pairing"):
        return True, doc["pairing"]
    why = {403: "The code is wrong, used or older than 10 minutes.", 429: "Too many tries. Wait a minute.",
           0: "The panel did not answer."}.get(status)
    if status == 400:
        why = ("The panel said no: %s. If the panel already has POL_ADMIN_GAME_%s_URL set, it manages "
               "this game from that setting; remove it there to pair instead." % (
                   doc.get("error") or "bad request", key.upper()))
    return False, why or doc.get("error") or "The panel said no (%d)." % status


def pair_update(panel_url, key, pairing, token, url=None, opener=urllib.request.urlopen):
    """Tell the panel the new admin token -> (ok, why)."""
    doc = {"key": key, "pairing": pairing, "token": token}
    if url:
        doc["url"] = url
    status, out = _post(panel_url + "/api/pair/update", doc, opener)
    if status == 200 and out.get("ok"):
        return True, ""
    return False, {403: "The panel no longer knows this pairing.", 404: "The panel has no such game.",
                   0: "The panel did not answer."}.get(status, out.get("error") or "HTTP %d" % status)
