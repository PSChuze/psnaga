"""Nobunaga Server Setup: python setup.py (or the "Nobunaga Server Setup" program)

The first run asks three questions (who plays, where the disc is, the server's name), writes .env,
builds the game data from the disc, starts the server in Docker with the shared DNS it needs, and
checks it. Later runs show a menu: start and stop, settings, update, backups, accounts, logs.
Needs Docker and nothing else (Python 3.8+ when run as a script, standard library only).

python setup.py --update --yes updates the server without a question (an OpenLobby panel's Update
button runs exactly that in the server folder): the newest release is downloaded and its images
built beside the running server, the files are swapped only once that worked, the game data is
built again only when the release changed extract/ or overlay/, and the services that were running
are started again. Its last line says what happened; it exits non-zero when the update failed.
"""
import argparse
import datetime
import hashlib
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import zipfile

from hostsetup import discs as disclib
from hostsetup import openlobby
from hostsetup import progress as proglib
from hostsetup import upnp

REPO = os.environ.get("NOBU_SETUP_REPO", "PSChuze/psnaga")
RELEASES = "https://github.com/%s/releases" % REPO
DOCKER_DOWNLOAD = "https://www.docker.com/products/docker-desktop/"
DOCKER_LINUX = "https://docs.docker.com/engine/install/"
PORT_HELP = "https://portforward.com/router.htm"
TITLE = "Nobunaga's Ambition Online"
KEY = "nobu"
DEFAULT_FOLDER = "Nobunaga Server"
DEFAULT_NAME = "Nobunaga"
FROZEN = getattr(sys, "frozen", False)

CONNSV_PORT = 9070                               # fixed in the client
ADMIN_URL = "http://nobu:19111"
# The names the client looks up: the connect server and login.
DNS_NAMES = ("nobol.koei.co.jp", "testconsv1.ax.koei.co.jp")
# The client's first frame on the connect server: the update check (0x15).
UPDATE_CHECK = bytes.fromhex("0011" "15" "00000001" "000f4309" "00000000" "00004e23")
# Files of ours in the server folder that an update must never replace.
KEEP = (".env", "compose.override.yml", "state", "backups", "discs", "logs", ".update")
# What a panel's deployer runs in the server folder to update it.
UPDATE_COMMAND = ["python", "setup.py", "--update", "--yes"]
# compose.yml's long-running services, in the order they are started.
SERVICES = ("connsv", "auth", "ssl3", "world")
# setup.json's note that an update changed the game data builder and the game data has not been
# built again yet: the next update does it.
DATA_PENDING = "game_data_pending"


def use_root(path):
    """Point every path at the server folder `path`."""
    global ROOT, ENV, ENV_EXAMPLE, BACKUPS, CHOICES, LOG, VERSION_FILE, REVISION_FILE
    ROOT = os.path.abspath(path)
    ENV = os.path.join(ROOT, ".env")
    ENV_EXAMPLE = os.path.join(ROOT, ".env.example")
    BACKUPS = os.path.join(ROOT, "backups")
    CHOICES = os.path.join(ROOT, "state", "setup.json")
    LOG = os.path.join(ROOT, "logs", "setup.log")
    VERSION_FILE = os.path.join(ROOT, ".setup-version")
    REVISION_FILE = os.path.join(ROOT, ".setup-revision")


use_root(os.path.dirname(os.path.abspath(__file__)))


# --- terminal ----------------------------------------------------------------------------

def say(text=""):
    print(text, flush=True)


def ask(question, default=""):
    shown = " [%s]" % default if default else ""
    try:
        answer = input("%s%s: " % (question, shown)).strip()
    except (EOFError, RuntimeError):         # RuntimeError: no input at all (stdin closed)
        if not default:
            raise SystemExit("\nNo answer (the input has ended).")
        answer = ""
    return answer or default


def ask_yes(question, default=True):
    answer = ask(question + (" (Y/n)" if default else " (y/N)")).lower()
    return default if not answer else answer.startswith("y")


def ask_choice(question, options, default=1):
    """Number options from 1 and return the 1-based pick."""
    if question:
        say(question)
    for i, text in enumerate(options, 1):
        say("  %d. %s" % (i, text))
    while True:
        answer = ask("Type a number", str(default))
        if answer.isdigit() and 1 <= int(answer) <= len(options):
            return int(answer)
        say("  Type one of the numbers above.")


def ask_port(question, default):
    while True:
        answer = ask(question, str(default))
        if answer.isdigit() and 1024 <= int(answer) < 65536 and int(answer) != CONNSV_PORT:
            return int(answer)
        say("  A port is a number from 1024 to 65535 (not %d)." % CONNSV_PORT)


def heading(text):
    say()
    say(text)
    say("-" * len(text))


def log_file():
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    f = open(LOG, "a", encoding="utf-8")
    f.write("\n=== %s ===\n" % datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    return f


# --- the server's files -------------------------------------------------------------------

def find_root(args_folder=None):
    """The server folder: the one given, else the folder this runs from when it holds the
    server, else "Nobunaga Server" in the home folder (made when missing)."""
    if args_folder:
        return os.path.abspath(args_folder)
    if not FROZEN:
        return os.path.dirname(os.path.abspath(__file__))
    for d in (os.path.dirname(sys.executable), os.getcwd()):
        if os.path.exists(os.path.join(d, "compose.yml")):
            return d
    return os.path.join(os.path.expanduser("~"), DEFAULT_FOLDER)


def bundled(name):
    """A file packed into the setup program, or None."""
    base = getattr(sys, "_MEIPASS", None)
    path = os.path.join(base, name) if base else None
    return path if path and os.path.exists(path) else None


def unpack_server(archive, root, version=""):
    """Unpack a source zip of this repository into root, replacing the files the last one put
    there and leaving the host's own (.env, state, ...) alone. Returns the paths it wrote."""
    with zipfile.ZipFile(archive) as z:
        names = [n for n in z.namelist() if not n.endswith("/")]
        # GitHub's and git archive's zips put everything in one top folder.
        tops = {n.split("/", 1)[0] for n in z.namelist()}
        strip = len(tops) == 1 and all("/" in n for n in names)
        old = set(read_manifest(root))
        written = []
        for name in names:
            rel = name.split("/", 1)[1] if strip else name
            if not rel or rel.split("/", 1)[0] in KEEP:
                continue
            target = os.path.normpath(os.path.join(root, rel))
            if not target.startswith(os.path.normpath(root) + os.sep):
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with z.open(name) as src, open(target + ".tmp", "wb") as dst:
                shutil.copyfileobj(src, dst)
            os.replace(target + ".tmp", target)
            written.append(rel)
    for rel in old - set(written):
        try:
            os.remove(os.path.join(root, rel))
        except OSError:
            pass
    with open(os.path.join(root, ".setup-files"), "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(sorted(written)) + "\n")
    if version:
        with open(os.path.join(root, ".setup-version"), "w", encoding="utf-8") as f:
            f.write(version + "\n")
    return written


def read_manifest(root):
    try:
        with open(os.path.join(root, ".setup-files"), encoding="utf-8") as f:
            return [line.strip() for line in f if line.strip()]
    except OSError:
        return []


def installed_version():
    try:
        with open(VERSION_FILE, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def ensure_server_files():
    """Put the server's files in ROOT when they are not there yet."""
    if os.path.exists(os.path.join(ROOT, "compose.yml")):
        return True
    archive = bundled("server.zip")
    if not archive:
        say("The server's files are not in %s, and this setup program does not carry them." % ROOT)
        say("Download Nobunaga Server Setup again from %s" % RELEASES)
        return False
    os.makedirs(ROOT, exist_ok=True)
    version = ""
    if bundled("server-version.txt"):
        with open(bundled("server-version.txt"), encoding="utf-8") as f:
            version = f.read().strip()
    unpack_server(archive, ROOT, version)
    say("The server's files are in %s" % ROOT)
    return True


def read_revision(root=None):
    """The commit the server's files are from and when they were packaged ({} when unknown)."""
    try:
        with open(os.path.join(root, ".setup-revision") if root else REVISION_FILE, encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) and isinstance(doc.get("commit"), str) else {}


def write_revision(commit, root=None):
    with open(os.path.join(root, ".setup-revision") if root else REVISION_FILE, "w", encoding="utf-8",
              newline="\n") as f:
        f.write(json.dumps({"commit": commit, "built_at": now_iso()}) + "\n")


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def git_head():
    """The commit a git checkout in ROOT is at, or ""."""
    if not os.path.exists(os.path.join(ROOT, ".git")):
        return ""
    try:
        r = run(["git", "rev-parse", "HEAD"], check=False, capture=True, stdin=subprocess.DEVNULL)
    except OSError:
        return ""
    head = (r.stdout or "").strip()
    return head if r.returncode == 0 and re.fullmatch(r"[0-9a-f]{40}", head) else ""


def build_env(root=None):
    """The environment for `docker compose build`: the commit and build time the panel's update
    notice shows (compose.yml's build args), when they are known."""
    env = dict(os.environ)
    rev = read_revision(root)
    commit = rev.get("commit") or (git_head() if root in (None, ROOT) else "")
    if commit:
        env["NOBU_REVISION"] = commit
        env["NOBU_BUILT_AT"] = rev.get("built_at") or now_iso()
    return env


def github(path):
    """GET https://api.github.com/repos/REPO/<path> -> parsed JSON, or None."""
    url = "https://api.github.com/repos/%s/%s" % (REPO, path)
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"Accept": "application/vnd.github+json"}),
                                    timeout=15) as r:
            return json.load(r)
    except (OSError, ValueError):
        return None


def latest_release():
    """(tag, commit, source zip URL) of the newest release, or None. When GitHub names the tag's
    commit the zip is that commit, so the update notice compares exactly what was installed;
    otherwise commit is ""."""
    info = github("releases/latest")
    tag = info.get("tag_name") if isinstance(info, dict) else None
    if not isinstance(tag, str) or not tag:
        return None
    got = github("commits/%s" % urllib.parse.quote(tag, safe=""))
    commit = got.get("sha") if isinstance(got, dict) else None
    if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
        return tag, "", "https://github.com/%s/archive/refs/tags/%s.zip" % (REPO, urllib.parse.quote(tag))
    return tag, commit, "https://github.com/%s/archive/%s.zip" % (REPO, commit)


def download(url, path):
    with urllib.request.urlopen(url, timeout=60) as r, open(path, "wb") as f:
        shutil.copyfileobj(r, f)


def tree_digest(*folders, root=None):
    """One hash over every file under these folders of root (default ROOT), to tell whether an
    update changed the game data builder."""
    root = root or ROOT
    h = hashlib.sha256()
    for folder in folders:
        for base, dirs, files in os.walk(os.path.join(root, folder)):
            dirs[:] = sorted(d for d in dirs if d != "__pycache__")
            for name in sorted(files):
                path = os.path.join(base, name)
                h.update(os.path.relpath(path, root).replace("\\", "/").encode())
                with open(path, "rb") as f:
                    h.update(hashlib.sha256(f.read()).digest())
    return h.hexdigest()


# --- docker ------------------------------------------------------------------------------

def run(cmd, check=True, capture=False, **kw):
    """Run a command in the server folder; its output goes to the terminal unless captured."""
    if capture:
        kw.update(stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
    result = subprocess.run(cmd, cwd=ROOT if os.path.isdir(ROOT) else None, **kw)
    if check and result.returncode != 0:
        raise SystemExit("Failed (exit %d): %s" % (result.returncode, " ".join(cmd)))
    return result


def compose(*args, **kw):
    return run(["docker", "compose"] + list(args), **kw)


def add_docker_to_path():
    """Docker installed while this window was open is not on its PATH yet."""
    places = [os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "Docker", "Docker", "resources",
                           "bin"),
              "/usr/local/bin", "/Applications/Docker.app/Contents/Resources/bin",
              os.path.join(os.path.expanduser("~"), ".docker", "bin")]
    path = os.environ.get("PATH", "").split(os.pathsep)
    for p in places:
        if os.path.isdir(p) and p not in path:
            path.append(p)
    os.environ["PATH"] = os.pathsep.join(path)


def docker_state():
    """"ok", "missing", "stopped", "denied" (Linux user not allowed) or "old"."""
    add_docker_to_path()
    if not shutil.which("docker"):
        return "missing"
    try:
        info = run(["docker", "info"], check=False, capture=True, timeout=60)
    except subprocess.TimeoutExpired:
        return "stopped"
    if info.returncode != 0:
        return "denied" if "permission denied" in (info.stdout or "").lower() else "stopped"
    if run(["docker", "compose", "version"], check=False, capture=True).returncode != 0:
        return "old"
    return "ok"


def start_docker_desktop():
    """Ask Docker Desktop to start; True when there was one to start."""
    if os.name == "nt":
        exe = os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "Docker", "Docker",
                           "Docker Desktop.exe")
        if os.path.exists(exe):
            subprocess.Popen([exe], close_fds=True)
            return True
    elif sys.platform == "darwin" and os.path.isdir("/Applications/Docker.app"):
        return subprocess.run(["open", "-a", "Docker"]).returncode == 0
    return False


def wait_for_docker(wait=True):
    """Return once Docker works. Explains what is missing and waits for the host to fix it
    (or returns False at once with wait=False)."""
    started = False
    while True:
        state = docker_state()
        if state == "ok":
            return True
        say()
        if state == "missing":
            if sys.platform.startswith("linux"):
                say("Docker is not installed. Install Docker Engine: %s" % DOCKER_LINUX)
            else:
                say("Docker Desktop is not installed. This server runs inside it.")
                say("Download it here, install it and start it: %s" % DOCKER_DOWNLOAD)
                say("If it asks you to sign in, you can skip that.")
        elif state == "stopped" and not started and wait and start_docker_desktop():
            started = True
            say("Starting Docker Desktop. This can take a minute or two.")
            for _ in range(60):
                time.sleep(3)
                if docker_state() == "ok":
                    say("Docker Desktop is running.")
                    return True
            say("Docker Desktop has not started yet. If it shows a message (terms to accept,")
            say("an update to install), answer it.")
        elif state == "stopped":
            if sys.platform.startswith("linux"):
                say("Docker is installed but not running. Start it with: sudo systemctl start docker")
            else:
                say("Docker Desktop is installed but not running. Open it and wait until it says")
                say("it is running.")
        elif state == "denied":
            say("Your user may not use Docker yet. Run this, then log out and back in:")
            say("  sudo usermod -aG docker $USER")
        else:
            say("This Docker is too old (it has no 'docker compose'). Update it: %s" % DOCKER_DOWNLOAD)
        if not wait:
            return False
        ask("Press Enter to check again (or close this window to stop)")


def docker_run(args, timeout=600, cwd=None):
    """openlobby's way to run docker, from the server folder unless told otherwise."""
    return openlobby.run_docker(args, timeout=timeout, cwd=cwd or (ROOT if os.path.isdir(ROOT) else None))


def server_running():
    out = compose("ps", "--status", "running", "--services", check=False, capture=True).stdout or ""
    return "world" in out.split()


# --- checks ------------------------------------------------------------------------------

def connsv_answers(port=CONNSV_PORT, timeout=5.0):
    """Send the client's update check; True when the connect server gives its 0x22 answer."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout) as s:
            s.sendall(UPDATE_CHECK)
            head = b""
            while len(head) < 3:
                chunk = s.recv(3 - len(head))
                if not chunk:
                    return False
                head += chunk
    except OSError:
        return False
    return head[2] == 0x22


def port_open(port, timeout=3.0):
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def wait_ready(env, timeout=120):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if connsv_answers(connsv_port(env)) and port_open(world_port(env)) and port_open(login_port(env)):
            return True
        time.sleep(2)
    return False


def smoke(quiet=False):
    env = read_env()
    if not quiet:
        say("Checking the server...")
    checks = [("The connect server (port %d)" % connsv_port(env), connsv_answers(connsv_port(env))),
              ("The login (port %d)" % login_port(env), port_open(login_port(env))),
              ("The world server (port %d)" % world_port(env), port_open(world_port(env)))]
    bad = [name for name, ok in checks if not ok]
    if not bad:
        say("The server answers as it should.")
        return True
    for name in bad:
        say("%s does not answer. See the server's log (menu: logs)." % name)
    return False


# --- settings ----------------------------------------------------------------------------

def read_env(path=None):
    path = path or ENV
    values = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                m = re.match(r"\s*([A-Z_][A-Z0-9_]*)=(.*)$", line.rstrip("\n"))
                if m:
                    values[m.group(1)] = m.group(2).strip()
    return values


def write_env(values, path=None, template=None):
    """Write values into the template's lines (a commented-out `#KEY=` line is switched on,
    a value of None comments a line out); keys the template does not have go at the end.
    Comments stay as they are."""
    path, template = path or ENV, template or ENV_EXAMPLE
    source = path if os.path.exists(path) else template
    with open(source, encoding="utf-8") as f:
        lines = f.read().splitlines()
    left = dict(values)
    out = []
    for line in lines:
        m = re.match(r"(#?)([A-Z_][A-Z0-9_]*)=", line)
        if m and m.group(2) in left:
            value = left.pop(m.group(2))
            out.append(("#" + line.lstrip("#")) if value is None else "%s=%s" % (m.group(2), value))
            continue
        out.append(line)
    for key, value in left.items():
        if value is not None:
            out.append("%s=%s" % (key, value))
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(out) + "\n")
    os.replace(tmp, path)


def read_choices():
    try:
        with open(CHOICES, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def write_choices(choices):
    os.makedirs(os.path.dirname(CHOICES), exist_ok=True)
    with open(CHOICES, "w", encoding="utf-8", newline="\n") as f:
        json.dump(choices, f, indent=1)
        f.write("\n")


def lan_address():
    """This machine's address on the local network (no packet is sent)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))
            return s.getsockname()[0]
    except OSError:
        return ""


def public_address():
    try:
        with urllib.request.urlopen("https://api.ipify.org", timeout=4) as r:
            ip = r.read().decode().strip()
        socket.inet_aton(ip)
        return ip
    except (OSError, ValueError):
        return ""


def to_ip(address):
    """The IPv4 address for what the host typed (an address, or a name that leads to one), or ""."""
    try:
        socket.inet_aton(address)
        return address if address.count(".") == 3 else ""
    except OSError:
        pass
    try:
        return socket.gethostbyname(address)
    except (OSError, UnicodeError):
        return ""


def who_plays(env, choices):
    """"home" or "internet": as chosen, or guessed from the address."""
    if choices.get("who") in ("home", "internet"):
        return choices["who"]
    return "home" if upnp.is_private(env.get("NOBU_PUBLIC_IP", "")) else "internet"


def world_name(name):
    """The name as the world list can carry it: no commas or @ (they split the list), at most 32
    characters."""
    name = re.sub(r"[,@\r\n]", " ", name or "").strip()
    return " ".join(name.split())[:32] or DEFAULT_NAME


def first_settings(address, name, env):
    """The .env values for the answers. Everything not asked keeps its value in env (the
    template on a first run) or gets the usual default."""
    changes = {"NOBU_PUBLIC_IP": address, "NOBU_SERVER_NAME": world_name(name)}
    if not env.get("NOBU_ADMIN_TOKEN"):
        changes["NOBU_ADMIN_TOKEN"] = secrets.token_hex(24)
    return changes


def connsv_port(env):
    return int(env.get("NOBU_CONNSV_PORT") or CONNSV_PORT)


def login_port(env):
    return int(env.get("NOBU_LOGIN_PORT") or 9443)


def world_port(env):
    return int(env.get("NOBU_WORLD_PORT") or 9110)


def ports_to_open(env):
    """The TCP ports players must reach, in order."""
    return [connsv_port(env), login_port(env), world_port(env)]


def state_dir(env=None):
    env = read_env() if env is None else env
    return os.path.join(ROOT, env.get("NOBU_STATE") or "state")


def assets_dir(env=None):
    return os.path.join(state_dir(env), "assets")


def has_assets(env=None):
    return os.path.exists(os.path.join(assets_dir(env), "manifest.json"))


def write_news(env, server_name, text):
    path = os.path.join(state_dir(env), "data", "news.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    news = [{"date": datetime.date.today().isoformat(), "title": "Welcome to %s" % server_name, "body": text}]
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(news, f, ensure_ascii=False, indent=2)
        f.write("\n")


# --- the questions -----------------------------------------------------------------------

def ask_who(current="home", total=3):
    heading("Question 1 of %d: Who will play on this server?" % total)
    pick = ask_choice("", ["Just people on my home network", "Friends over the internet"],
                      2 if current == "internet" else 1)
    return "internet" if pick == 2 else "home"


def ask_address(who, current=""):
    """The address players' consoles reach: this machine's for home, the internet one otherwise."""
    if who == "home":
        lan = lan_address()
        if lan:
            say("Players at home reach this computer at %s." % lan)
            return lan
        say("Setup could not find this computer's address on your network.")
        default = current
    else:
        public = public_address()
        default = current if current and not upnp.is_private(current) else public
        if public:
            say("Your internet address is %s." % public)
        if default:
            say("If that is the address friends reach, press Enter. Otherwise type it.")
        else:
            say("Setup could not find your internet address. Type it, or a name that leads to it.")
    while True:
        typed = ask("Address", default)
        address = to_ip(typed) if typed else ""
        if address:
            if address != typed:
                say("That name leads to %s; setup uses that address." % address)
            return address
        if typed:
            say("  That is not an address, and no address could be found for that name.")


def find_disc(total=3, extra=()):
    """Look in the usual places and offer what is there; ask until the disc is known. -> Disc."""
    heading("Question 2 of %d: Where is your Nobunaga's Ambition Online disc?" % total)
    say("Setup needs an image (.iso) of Nobunaga no Yabou Online - Hiryuu no Shou (SLPM-65197),")
    say("or a zip of one. The server reads its name tables and maps from it.")
    places = [ROOT] + ([os.path.dirname(sys.executable)] if FROZEN else []) + list(extra)
    say("Looking in Downloads, Documents, Desktop and PCSX2's game folders...")
    chosen = disclib.pick(disclib.search(disclib.likely_folders(places)))
    if chosen:
        say("Found: %s" % chosen.shown())
        if ask_yes("Use it?"):
            return chosen
    while True:
        say("Type the folder (or file) where the disc image is. You can also drag the file into this")
        say("window and press Enter.")
        place = ask("Folder or file").strip().strip('"').strip("'")
        if not place:
            continue
        place = os.path.abspath(os.path.expanduser(place))
        if os.path.isfile(place) or os.path.exists(os.path.join(place, "SYSTEM.CNF")):
            found = disclib.identify(place)
        elif os.path.isdir(place):
            found = disclib.search([place], depth=4)
        else:
            say("  There is no folder or file at %s" % place)
            continue
        chosen = disclib.pick(found)
        if chosen:
            say("Found: %s" % chosen.shown())
            return chosen
        say("  No Hiryuu no Shou disc image there. Other pressings of the game do not work.")


def ask_name(current=DEFAULT_NAME, number=3, total=3):
    heading("Question %d of %d: Give your server a name" % (number, total))
    say("Players see it in the game's world list.")
    return world_name(ask("Server name", current) or DEFAULT_NAME)


def ask_pairing(panel, number=4, total=4):
    heading("Question %d of %d: Use your OpenLobby admin panel?" % (number, total))
    say("An OpenLobby admin panel runs on this computer (%s)." % panel)
    return ask_yes("Manage %s from it?" % TITLE)


# --- the disc to game data -----------------------------------------------------------------

def disc_record(d):
    return {"path": d.path, "member": d.member}


def unzip_disc(rec, progress, dest_dir):
    """Unpack the disc image from its zip into dest_dir; returns the new file's path."""
    os.makedirs(dest_dir, exist_ok=True)
    target = os.path.join(dest_dir, os.path.basename(rec["member"]))
    with zipfile.ZipFile(rec["path"]) as z:
        info = z.getinfo(rec["member"])
        if os.path.exists(target) and os.path.getsize(target) == info.file_size:
            return target
        if shutil.disk_usage(dest_dir).free < info.file_size + (1 << 30):
            raise SystemExit("Not enough free disk space to unpack %s (it needs %d GB)."
                             % (os.path.basename(rec["path"]), info.file_size // (1 << 30) + 1))
        progress.begin("Unpacking %s" % os.path.basename(rec["path"]), "a few minutes")
        done = 0
        with z.open(info) as src, open(target + ".tmp", "wb") as dst:
            while True:
                chunk = src.read(8 << 20)
                if not chunk:
                    break
                dst.write(chunk)
                done += len(chunk)
                progress.set("%d%%" % (done * 100 // max(1, info.file_size)))
    os.replace(target + ".tmp", target)
    progress.end()
    return target


def disc_mount(path):
    """(docker -v options, the disc's path inside the container): its folder is shared read-only."""
    path = os.path.abspath(path)
    if os.path.isdir(path):
        return ["-v", "%s:/picked/1:ro" % path.replace("\\", "/")], "/picked/1"
    folder = os.path.dirname(path)
    return ["-v", "%s:/picked/1:ro" % folder.replace("\\", "/")], "/picked/1/" + os.path.basename(path)


def ensure_disc(choices, ask_again=False):
    if ask_again or not choices.get("disc"):
        choices["disc"] = disc_record(find_disc(total=1))
        write_choices(choices)


def build_game_data(env, choices, progress, log, unseen_ok=False):
    """Run extract on the disc. True when it worked. With unseen_ok a disc image this process
    cannot see is still handed to Docker: a panel's deployer runs setup in a container that
    has the server folder but not the rest of the host's files, while Docker sees them all."""
    rec = choices.get("disc") or {}
    unpacked = os.path.join(ROOT, "discs", os.path.basename(rec.get("member") or ""))
    try:
        if rec.get("member") and unseen_ok and not os.path.exists(rec.get("path", "")) and os.path.isfile(unpacked):
            path = unpacked
        else:
            path = unzip_disc(rec, progress, os.path.join(ROOT, "discs")) if rec.get("member") else rec.get("path", "")
    except (SystemExit, OSError, KeyError, zipfile.BadZipFile) as e:
        progress.end(False)
        say(str(e))
        return False
    if not path or not (os.path.exists(path) or (unseen_ok and os.path.isabs(path))):
        say("The disc image is not at %s any more. Choose Rebuild the game data to pick it again." % path)
        return False
    volumes, inside = disc_mount(path)
    # The path goes through the environment: `-e NAME` alone copies it from ours, so no path has to
    # survive command-line quoting.
    cmd = ["docker", "compose", "run", "--rm"] + volumes + ["-e", "NOBU_DISC_FILES", "extract"]
    progress.begin("Reading your disc", "under a minute")
    code, tail = proglib.run_logged(cmd, log, cwd=ROOT, env=dict(os.environ, NOBU_DISC_FILES=inside),
                                    progress=progress)
    progress.end(code == 0)
    if code != 0:
        failed("Building the game data failed.", tail)
    return code == 0


# --- the shared DNS and the panel -----------------------------------------------------------

def port_in_use(port):
    """True when something on this machine already listens on the TCP port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1)
        return s.connect_ex(("127.0.0.1", port)) == 0


def udp_port_taken(port):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.bind(("0.0.0.0", port))
        except OSError as e:
            return getattr(e, "errno", 0) not in (13, 10013)     # no permission is not "taken"
    return False


def ensure_dns(env, progress, log):
    """Register the game's names with the shared DNS, starting it when there is none."""
    progress.begin("Setting up the DNS server", "a few seconds")
    dns = openlobby.find_dns(docker_run)
    if not dns:
        if port_in_use(53) or udp_port_taken(53):
            progress.end(False)
            say("Port 53 is taken by another DNS server on this computer, so the shared DNS cannot")
            say("start. Point these names at %s on that server: %s" % (env.get("NOBU_PUBLIC_IP"),
                                                                       ", ".join(DNS_NAMES)))
            return False
        dns = openlobby.start_dns(os.path.join(state_dir(env), "dns"), env.get("NOBU_PUBLIC_IP", ""), docker_run)
    ok, out = False, "The shared DNS server did not start."
    if dns:
        ok, out = openlobby.dns_names(dns, KEY, DNS_NAMES, docker_run)
        log.write(out + "\n")
    progress.end(ok)
    if not ok:
        say(out)
        say("Players' consoles cannot find the server until these names lead here: %s" % ", ".join(DNS_NAMES))
    return ok


def updater_block():
    """How the panel's deployer updates this install: this setup, run in this folder."""
    return openlobby.updater(ROOT, UPDATE_COMMAND)


def note_updater(answer):
    if answer == "requested":
        say("The panel's Update button works once the panel's owner approves it on Settings > Modules.")


def pair_with_panel(env, panel):
    """Pair with the panel by its one-time code; keeps the secret in .env."""
    say("In the panel, open Settings, Modules, \"Pair with a setup program\". It shows a code.")
    for _ in range(3):
        code = ask("The code (Enter to skip)")
        if not code:
            say("Skipped. Choose Change settings, then OpenLobby admin panel, to pair later.")
            return False
        ok, got, updater = openlobby.pair(panel, code, KEY, TITLE, ADMIN_URL, env["NOBU_ADMIN_TOKEN"],
                                          updater=updater_block())
        if ok:
            write_env({"OPENLOBBY_PAIRING": got})
            choices = read_choices()
            choices["updater_folder"] = ROOT
            write_choices(choices)
            say("Paired: the panel now shows %s." % TITLE)
            note_updater(updater)
            return True
        say(got)
    return False


def tell_panel_the_folder(env, choices):
    """A paired panel runs updates in this folder: tell it again when the folder has moved."""
    if not env.get("OPENLOBBY_PAIRING") or choices.get("updater_folder") == ROOT:
        return
    panel = openlobby.find_panel(docker_run)
    if not panel:
        return
    ok, _why, updater = openlobby.pair_update(panel, KEY, env["OPENLOBBY_PAIRING"], env.get("NOBU_ADMIN_TOKEN", ""),
                                              updater=updater_block())
    if ok:
        choices["updater_folder"] = ROOT
        write_choices(choices)
        say("Told the OpenLobby panel that the server is now in %s." % ROOT)
        note_updater(updater)


# --- building and starting -------------------------------------------------------------------

def failed(what, tail):
    say()
    say(what)
    reason = proglib.explain(tail)
    if reason:
        say(reason)
    say("The last lines it wrote:")
    for line in tail[-12:]:
        say("  " + line)
    say("Everything it wrote is in %s" % LOG)


def build_images(progress, log, root=None):
    """docker compose build in root (default ROOT). True when it worked."""
    progress.begin("Getting the server program ready", "about 5 minutes the first time")
    code, tail = proglib.run_logged(["docker", "compose", "build"], log, cwd=root or ROOT, env=build_env(root),
                                    progress=progress)
    progress.end(code == 0)
    if code != 0:
        failed("Getting the server program ready failed.", tail)
    return code == 0


def start_server(progress, log):
    env = read_env()
    if not openlobby.ensure_network(docker_run):
        say("The Docker network %s could not be made." % openlobby.NETWORK)
        return False
    progress.begin("Starting the server", "under a minute")
    code, tail = proglib.run_logged(["docker", "compose", "up", "-d"], log, cwd=ROOT, progress=progress)
    ok = code == 0 and wait_ready(env)
    progress.end(ok)
    if code != 0:
        failed("Starting the server failed.", tail)
        return False
    if not ok:
        say("The server did not answer. Its last lines:")
        say(compose("logs", "--tail", "30", check=False, capture=True).stdout)
    return ok


# --- router ------------------------------------------------------------------------------

def open_router_ports(env, quiet=False):
    """Ask the router to forward the ports. Returns (all open?, the router's internet address or "")."""
    lan = lan_address()
    ports = ports_to_open(env)
    if not quiet:
        say("Asking your router to open the ports...")
    gateway = upnp.find_gateway(lan) if lan else None
    if not gateway:
        if not quiet:
            say("Your router did not answer (its UPnP setting may be off). The ports need opening by hand.")
        return False, ""
    results = upnp.open_ports(gateway, ports, lan)
    try:
        external = gateway.external_ip()
    except upnp.UpnpError:
        external = ""
    good = [p for p, r in results.items() if r in ("opened", "already")]
    bad = {p: r for p, r in results.items() if p not in good}
    if not quiet or bad:
        if not bad:
            say("Your router opened the ports for this computer (%s)." % lan)
        else:
            say("Your router opened %d of %d ports. Not opened:" % (len(good), len(ports)))
            for p, r in bad.items():
                say("  %d: %s" % (p, "another device uses it" if r == "taken" else r))
    if external and upnp.is_private(external):
        say()
        say("Note: your router has no internet address of its own (%s). Your internet provider" % external)
        say("probably shares one address between customers, or there is a second router in front")
        say("of this one. Friends outside your home will likely not reach the server. Ask your")
        say("provider for a public IP address.")
    return not bad, external


def show_port_help(env, router=""):
    lan = lan_address() or "this computer"
    heading("Open these ports on your router")
    say("Your router needs to send these ports to this computer (%s)." % lan)
    if router:
        say("Open the router's settings page (usually http://%s, or the address printed on the" % router)
        say('router) and look for "Port forwarding" (or "Virtual server", or "NAT").')
    else:
        say("Open the router's settings page (its address is printed on the router) and look for")
        say('"Port forwarding" (or "Virtual server", or "NAT").')
    say()
    say("  Port             Type   To")
    for p in ports_to_open(env):
        say("  %-16s TCP    %s" % (p, lan))
    say()
    say("Steps for many router models: %s" % PORT_HELP)


# --- finishing ---------------------------------------------------------------------------

def finish(env, choices):
    host = env["NOBU_PUBLIC_IP"]
    say()
    say("=" * 72)
    say("Your server is running.")
    say()
    say("Tell your players: set the DNS server in the PS2's network settings to %s" % host)
    if who_plays(env, choices) == "internet":
        say()
        if choices.get("router") == "open":
            say("Your router has the ports open.")
        else:
            say("Friends can join once the ports above are open on your router.")
        say("The shared DNS server answers only home networks unless you allow more (see README.md),")
        say("so friends over the internet may need their own DNS entries for %s." % ", ".join(DNS_NAMES))
    say()
    say("The server keeps running when you close this window, as long as Docker runs.")
    say("Run Nobunaga Server Setup again to stop it, change settings or make backups.")
    say("=" * 72)


def first_run(dry_run=False):
    say("Nobunaga Server Setup")
    say("=====================")
    say("This sets up a %s server on this computer. Answer three questions," % TITLE)
    say("then setup does the rest in a few minutes.")
    env = read_env(ENV_EXAMPLE)
    choices = read_choices()
    panel = None if dry_run else openlobby.find_panel(docker_run)
    total = 4 if panel else 3
    who = ask_who(total=total)
    address = ask_address(who)
    disc = find_disc(total=total)
    name = ask_name(total=total)
    use_panel = ask_pairing(panel, total=total) if panel else False
    changes = first_settings(address, name, env)
    choices.update(who=who, name=name, disc=disc_record(disc))

    heading("Ready")
    say("  Players:  %s, at %s" % ("people on your home network" if who == "home" else "friends over the internet",
                                   address))
    say("  Disc:     %s" % disc.shown())
    say("  Name:     %s" % name)
    if panel:
        say("  Panel:    %s" % ("manage from the OpenLobby panel" if use_panel else "not used"))
    say("Everything else uses the usual settings. You can change them later: run Nobunaga Server Setup")
    say("again and choose Change settings.")
    if dry_run:
        say()
        say("Dry run: nothing was written. Setup would write these settings to %s:" % ENV)
        for key, value in changes.items():
            if key != "NOBU_ADMIN_TOKEN":
                say("  %s=%s" % (key, value))
        say("  NOBU_ADMIN_TOKEN: made at random")
        return
    if not ask_yes("Start?"):
        return
    write_env(changes)
    env = read_env()
    write_choices(choices)
    write_news(env, name, "Welcome! Have fun, and tell the people who run this server about any bugs.")
    say()
    say("Details of every step go to %s" % LOG)
    if who == "internet":
        heading("Router")
        ok, router = open_router_ports(env)
        choices["router"] = "open" if ok else "closed"
        write_choices(choices)
        if not ok:
            show_port_help(env, router)
    if complete_setup(env, choices) and use_panel:
        heading("OpenLobby admin panel")
        pair_with_panel(read_env(), panel)


def complete_setup(env, choices):
    """Build what is missing, then start the server."""
    heading("Building")
    say("Each step says roughly how long it takes.")
    progress = proglib.Progress()
    with log_file() as log:
        if not openlobby.ensure_network(docker_run):
            say("The Docker network %s could not be made." % openlobby.NETWORK)
            return False
        if not build_images(progress, log):
            return False
        if not has_assets(env):
            ensure_disc(choices)
            if not build_game_data(env, choices, progress, log):
                say("Fix the cause, then run Nobunaga Server Setup again and choose Rebuild the game data.")
                return False
        ensure_dns(env, progress, log)
        if not start_server(progress, log):
            return False
    finish(read_env(), choices)
    return True


# --- menu --------------------------------------------------------------------------------

def restart_if_running(was_running):
    if was_running:
        say("Restarting the server with the new settings...")
        compose("up", "-d")
        if wait_ready(read_env()):
            smoke()


def change_settings():
    """The settings menu. Returns once the host goes back."""
    while True:
        env, choices = read_env(), read_choices()
        who = who_plays(env, choices)
        items = [
            ("Who will play (now: %s, at %s)" % ("home network" if who == "home" else "friends over the internet",
                                                 env.get("NOBU_PUBLIC_IP")), change_who),
            ("Server name and news (now: %s)" % (env.get("NOBU_SERVER_NAME") or DEFAULT_NAME), change_name),
            ("Who can make an account (now: %s)" % ("anyone, by logging in" if env.get("NOBU_AUTH_OPEN") == "claim"
                                                    else "only accounts you add"), change_signup),
            ("Login and world ports (now: %d and %d)" % (login_port(env), world_port(env)), change_ports),
            ("OpenLobby admin panel (now: %s)" % ("paired" if env.get("OPENLOBBY_PAIRING") else "not paired"),
             change_panel),
            ("Make a new admin token", new_token),
        ]
        heading("Change settings")
        for i, (title, _) in enumerate(items, 1):
            say("  %d. %s" % (i, title))
        say("  0. Back")
        pick = ask("Type a number", "0")
        if pick == "0":
            return
        if pick.isdigit() and 1 <= int(pick) <= len(items):
            running = server_running()
            if items[int(pick) - 1][1](env, read_choices()):
                restart_if_running(running)


def change_who(env, choices):
    who = ask_who(who_plays(env, choices), total=1)
    address = ask_address(who, env.get("NOBU_PUBLIC_IP", ""))
    choices["who"] = who
    write_env({"NOBU_PUBLIC_IP": address})
    if who == "internet":
        ok, router = open_router_ports(read_env())
        choices["router"] = "open" if ok else "closed"
        if not ok:
            show_port_help(read_env(), router)
    write_choices(choices)
    if os.path.exists(os.path.join(state_dir(), "dns", "compose.yml")):
        # this setup started the shared DNS server: it answers with the new address from now on
        openlobby.start_dns(os.path.join(state_dir(), "dns"), address, docker_run)
    else:
        say("The shared DNS server answers with the address it was started with. If that was this")
        say("computer's old address, start it again with POL_ADVERTISE=%s." % address)
    say("Players now set their DNS server to %s." % address)
    return True


def change_name(env, choices):
    name = ask_name(env.get("NOBU_SERVER_NAME") or DEFAULT_NAME, number=1, total=1)
    text = ask("News text", "Welcome! Have fun, and tell the people who run this server about any bugs.")
    write_news(env, name, text)
    write_env({"NOBU_SERVER_NAME": name})
    choices["name"] = name
    write_choices(choices)
    say("Saved. Players see the new name in the world list and the news in the Function menu.")
    return True


def change_signup(env, choices):
    pick = ask_choice("Who can make an account?", [
        "Anyone: logging in with a new citizen ID and password makes the account",
        "Only accounts you add yourself (Add a player account in the menu)"],
        1 if env.get("NOBU_AUTH_OPEN") == "claim" else 2)
    write_env({"NOBU_AUTH_OPEN": "claim" if pick == 1 else ""})
    return True


def change_ports(env, choices):
    say("Only change these if something else on this computer uses them. The connect server's")
    say("port %d is fixed in the game and cannot move. The router needs the new ones open." % CONNSV_PORT)
    write_env({"NOBU_LOGIN_PORT": ask_port("Login port", login_port(env)),
               "NOBU_WORLD_PORT": ask_port("World port", world_port(env))})
    return True


def change_panel(env, choices):
    panel = openlobby.find_panel(docker_run)
    if not panel:
        say("No OpenLobby admin panel runs on this computer.")
        say("Without pairing, a panel can add this game with POL_ADMIN_GAME_NOBU_URL=%s" % ADMIN_URL)
        say("and POL_ADMIN_GAME_NOBU_TOKEN set to NOBU_ADMIN_TOKEN from %s." % ENV)
        return False
    pair_with_panel(env, panel)
    return False


def new_token(env, choices):
    if not ask_yes("Make a new admin token? A panel that is not paired needs the new one by hand.", False):
        return False
    token = secrets.token_hex(24)
    write_env({"NOBU_ADMIN_TOKEN": token})
    panel = openlobby.find_panel(docker_run)
    if env.get("OPENLOBBY_PAIRING") and panel:
        ok, why, _updater = openlobby.pair_update(panel, KEY, env["OPENLOBBY_PAIRING"], token,
                                                  updater=updater_block())
        say("The panel has the new token." if ok else "The panel was not told (%s). Pair again." % why)
    return True


def start_server_menu():
    if server_running():
        say("The server is already running.")
        return
    env, choices = read_env(), read_choices()
    if not has_assets(env):
        say("The game data is not built yet.")
        if ask_yes("Build it now and start?"):
            complete_setup(env, choices)
        return
    if who_plays(env, choices) == "internet":
        open_router_ports(env, quiet=True)
    with log_file() as log:
        progress = proglib.Progress()
        ensure_dns(env, progress, log)
        if start_server(progress, log):
            say("Started.")


def stop_server():
    if not server_running():
        say("The server is not running.")
        return
    if ask_yes("Stop the server? Players in the game are disconnected.", False):
        compose("stop")
        say("Stopped. It starts again when you choose Start the server.")


class UpdateFailed(Exception):
    """An update that stopped; the message is the one sentence the host sees last."""


def running_services():
    """compose.yml's services that run now, in start order."""
    r = run(["docker", "compose", "ps", "--status", "running", "--services"], check=False, capture=True,
            stdin=subprocess.DEVNULL)
    if r.returncode != 0:
        raise UpdateFailed("Docker could not list the server's containers, so nothing was changed.")
    names = (r.stdout or "").split()
    return [n for n in SERVICES if n in names]


# Run inside one of the server's containers: does a service answer on the compose network? The
# update's check works this way because setup may itself run in a container (a panel's
# deployer), where 127.0.0.1 is that container and not this host. With a third argument the
# connect server must also answer the client's update check.
PORT_CHECK = """import socket, sys
s = socket.create_connection((sys.argv[1], int(sys.argv[2])), 5)
if len(sys.argv) > 3:
    s.sendall(bytes.fromhex(sys.argv[3]))
    head = b""
    while len(head) < 3:
        chunk = s.recv(3 - len(head))
        if not chunk:
            sys.exit(1)
        head += chunk
    sys.exit(0 if head[2] == 0x22 else 1)
"""
# The services whose image has Python to run the check, in the order they are tried.
CHECKERS = ("world", "connsv", "auth")


def check_target(service, env):
    """(host, port and extra arguments) of the service on the compose network."""
    return {"connsv": ["connsv", "9070", UPDATE_CHECK.hex()],
            "auth": ["auth", "18080"],
            "ssl3": ["ssl3", str(login_port(env))],
            "world": ["world", str(world_port(env))]}[service]


def service_answers(service, env, checker):
    """True when the service answers, checked from inside the `checker` service's container."""
    r = run(["docker", "compose", "exec", "-T", checker, "python", "-c", PORT_CHECK] + check_target(service, env),
            check=False, capture=True, stdin=subprocess.DEVNULL)
    return r.returncode == 0


def services_answer(services, env, timeout=120):
    """Wait until every one of these services answers -> the ones that still do not."""
    checkers = [c for c in CHECKERS if c in services]
    if not checkers:          # only ssl3 runs: no Python to check from, running is all there is
        return [s for s in services if s not in running_services()]
    deadline = time.monotonic() + timeout
    while True:
        bad = [s for s in services if not service_answers(s, env, checkers[0])]
        if not bad or time.monotonic() >= deadline:
            return bad
        time.sleep(3)


def is_current(tag, commit):
    have = read_revision().get("commit")
    if commit and have:
        return have == commit
    return tag == installed_version()


def update_server(yes=False):
    """Update to the newest release (see the module docstring) -> the closing line. Raises
    UpdateFailed with the sentence to show when it cannot."""
    if not wait_for_docker(wait=False):
        raise UpdateFailed("Docker is not running here, so nothing was updated.")
    services = running_services()
    if os.path.exists(os.path.join(ROOT, ".git")):
        return update_checkout(services)
    say("Looking for the newest release of %s..." % REPO)
    release = latest_release()
    if not release:
        raise UpdateFailed("Could not reach GitHub to find the newest release, so nothing was changed.")
    tag, commit, url = release
    if is_current(tag, commit):
        if read_choices().get(DATA_PENDING):
            return after_update(services, True, tag)
        return "Already up to date (%s)." % tag
    if not yes and not ask_yes("Update from %s to %s?" % (installed_version() or "this version", tag)):
        return "Nothing was changed."
    stage = os.path.join(ROOT, ".update")
    shutil.rmtree(stage, ignore_errors=True)
    os.makedirs(stage)
    try:
        say("Downloading %s..." % tag)
        archive = os.path.join(stage, "release.zip")
        new = os.path.join(stage, "server")
        try:
            download(url, archive)
            unpack_server(archive, new)
        except (OSError, zipfile.BadZipFile) as e:
            raise UpdateFailed("The download of %s failed (%s), so nothing was changed." % (tag, e))
        if not os.path.exists(os.path.join(new, "compose.yml")):
            raise UpdateFailed("The download of %s is not this server, so nothing was changed." % tag)
        if commit:
            write_revision(commit, new)
        for name in (".env", "compose.override.yml"):
            if os.path.exists(os.path.join(ROOT, name)):
                shutil.copyfile(os.path.join(ROOT, name), os.path.join(new, name))
        # The new images are built beside the running server, whose containers keep the old ones
        # until they are recreated below.
        with log_file() as log:
            if not build_images(proglib.Progress(), log, root=new):
                raise UpdateFailed("Building %s failed, so the server's files were not changed and the old "
                                   "version keeps running." % tag)
        data_changed = tree_digest("extract", "overlay") != tree_digest("extract", "overlay", root=new)
        say("Replacing the server's files with %s..." % tag)
        unpack_server(archive, ROOT, tag)
        if commit:
            write_revision(commit)
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    if FROZEN:
        say("A new Nobunaga Server Setup is also on %s" % RELEASES)
    return after_update(services, data_changed, tag)


def update_checkout(services):
    """A git checkout is updated with git pull, then built and restarted as a release is."""
    head = git_head()
    before = tree_digest("extract", "overlay")
    say("Updating this git checkout (git pull --ff-only)...")
    r = run(["git", "pull", "--ff-only"], check=False, capture=True, stdin=subprocess.DEVNULL)
    if r.returncode != 0:
        raise UpdateFailed("git pull failed (local changes?), so nothing was changed.")
    now = git_head()
    if now == head:
        if read_choices().get(DATA_PENDING):
            return after_update(services, True, now[:7])
        return "Already up to date (%s)." % (now[:7] or "git")
    with log_file() as log:
        if not build_images(proglib.Progress(), log):
            raise UpdateFailed("Building %s failed; the old version keeps running." % now[:7])
    return after_update(services, tree_digest("extract", "overlay") != before, now[:7])


def after_update(services, data_changed, label):
    """Build the game data again when its builder changed, then start the services that ran
    before (and only those) and check them -> the closing line."""
    env, choices = read_env(), read_choices()
    progress = proglib.Progress()
    if (data_changed or choices.get(DATA_PENDING)) and has_assets(env):
        choices[DATA_PENDING] = True
        write_choices(choices)
        if not choices.get("disc"):
            raise UpdateFailed("%s changes the game data, but setup does not know where the disc image is: run "
                               "Nobunaga Server Setup and choose Rebuild the game data." % label)
        say("This update changes the game data: building it again from your disc.")
        with log_file() as log:
            if not build_game_data(env, choices, progress, log, unseen_ok=True):
                raise UpdateFailed("%s is in place but building the game data failed, so the old version keeps "
                                   "running; run the update again once the cause is fixed." % label)
        choices = read_choices()
        choices.pop(DATA_PENDING, None)
        write_choices(choices)
    if not services:
        return "Updated to %s. The server was stopped and stays stopped." % label
    if not openlobby.ensure_network(docker_run):
        raise UpdateFailed("Updated to %s, but the Docker network %s could not be made, so the server was not "
                           "restarted." % (label, openlobby.NETWORK))
    progress.begin("Restarting %s" % ", ".join(services), "under a minute")
    with log_file() as log:
        code, tail = proglib.run_logged(["docker", "compose", "up", "-d", "--no-build", "--no-deps"] + services,
                                        log, cwd=ROOT, progress=progress)
    progress.end(code == 0)
    if code != 0:
        failed("Restarting the server failed.", tail)
        raise UpdateFailed("Updated to %s, but the server did not start again: see %s." % (label, LOG))
    say("Checking the server...")
    bad = services_answer(services, env)
    if bad:
        raise UpdateFailed("Updated to %s and restarted, but %s does not answer: see the server's logs."
                           % (label, " and ".join(bad)))
    return "Updated to %s; restarted %s, and the server answers." % (label, ", ".join(services))


def update():
    """The menu's Update: the same update as --update, asking before it starts."""
    try:
        say(update_server(yes=False))
    except UpdateFailed as e:
        say(str(e))


def update_now(yes):
    """--update: run the update -> the exit code. The last line printed is the summary."""
    if not os.path.exists(os.path.join(ROOT, "compose.yml")):
        say("There is no server in %s to update." % ROOT)
        return 1
    os.chdir(ROOT)
    try:
        say(update_server(yes=yes))
        return 0
    except UpdateFailed as e:
        say(str(e))
    except KeyboardInterrupt:
        say("The update was stopped before it finished; run it again.")
    except SystemExit as e:
        say("The update stopped (%s); run it again." % e)
    except Exception as e:      # whatever it was, the last line must still say so
        say("The update stopped (%s: %s); run it again." % (type(e).__name__, e))
    return 1


def rebuild_assets():
    env, choices = read_env(), read_choices()
    if choices.get("disc") and not ask_yes("Use the same disc image as last time (%s)?" % (
            choices["disc"].get("member") or choices["disc"].get("path"))):
        ensure_disc(choices, ask_again=True)
    ensure_disc(choices)
    progress = proglib.Progress()
    with log_file() as log:
        if not build_game_data(env, choices, progress, log):
            return
    choices = read_choices()
    if choices.pop(DATA_PENDING, None):
        write_choices(choices)
    if server_running():
        say("Restarting the server to use it...")
        compose("restart")


def data_dir():
    return os.path.join(state_dir(), "data")


def backup():
    data = data_dir()
    if not os.path.isdir(data):
        say("There is nothing to back up yet (%s is missing)." % data)
        return
    if server_running():
        compose("exec", "-T", "world", "python", "-m", "nobu.admin", "backup", "all", check=False, capture=True)
    os.makedirs(BACKUPS, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    archive = os.path.join(BACKUPS, "nobunaga-%s.zip" % stamp)
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
        for base, dirs, files in os.walk(data):
            dirs[:] = [d for d in dirs if d != "backups"]
            for name in files:
                path = os.path.join(base, name)
                z.write(path, os.path.relpath(path, data))
    say("Saved %s" % archive)
    say("Also keep a copy of %s somewhere safe." % ENV)


def restore():
    zips = sorted(f for f in os.listdir(BACKUPS) if f.endswith(".zip")) if os.path.isdir(BACKUPS) else []
    if not zips:
        say("No backups in %s." % BACKUPS)
        return
    for i, name in enumerate(zips, 1):
        say("  %d. %s" % (i, name))
    pick = ask("Restore which one (number, Enter to cancel)")
    if not pick.isdigit() or not 1 <= int(pick) <= len(zips):
        return
    say("This replaces every account and character with the ones in %s." % zips[int(pick) - 1])
    say("Play since then is lost.")
    if not ask_yes("Go on?", False):
        return
    running = server_running()
    compose("stop", check=False)
    data = data_dir()
    with zipfile.ZipFile(os.path.join(BACKUPS, zips[int(pick) - 1])) as z:
        for name in z.namelist():
            target = os.path.normpath(os.path.join(data, name))
            if target.startswith(os.path.normpath(data) + os.sep) and not name.endswith("/"):
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with z.open(name) as src, open(target + ".tmp", "wb") as dst:
                    shutil.copyfileobj(src, dst)
                os.replace(target + ".tmp", target)
    say("Restored.")
    if running:
        compose("up", "-d")


def accounts():
    if not server_running():
        say("Start the server first.")
        return
    pick = ask_choice("Accounts", ["Add a player account / reset a password", "List the accounts"])
    if pick == 2:
        compose("exec", "auth", "python", "-m", "nobu.auth", "list", check=False)
        return
    cid = ask("The citizen ID (1 to 8 lowercase letters, what the player types in the game)").lower()
    if not re.fullmatch(r"[a-z]{1,8}", cid):
        say("A citizen ID is 1 to 8 lowercase letters.")
        return
    password = ask("The password")
    if password:
        compose("exec", "auth", "python", "-m", "nobu.auth", "add", cid, password, check=False)


def logs():
    say("Showing what the server is doing. Press Ctrl+C to go back to the menu.")
    try:
        compose("logs", "-f", "--tail", "200", "connsv", "auth", "world", check=False)
    except KeyboardInterrupt:
        pass


def router_again():
    env, choices = read_env(), read_choices()
    if who_plays(env, choices) != "internet":
        say("Your server is set up for your home network only, so the router needs no open ports.")
        say("Choose Change settings, then Who will play, to change that.")
        return
    ok, router = open_router_ports(env)
    choices["router"] = "open" if ok else "closed"
    write_choices(choices)
    if not ok:
        show_port_help(env, router)


def tell_players():
    finish(read_env(), read_choices())


def uninstall():
    """Stop and remove the server's containers, and take its names off the shared DNS. The server
    folder (accounts and characters in state/data) stays; the shared DNS keeps running."""
    say("This removes the server's containers and takes its names off the shared DNS server.")
    say("Accounts, characters and backups stay in %s; delete that folder yourself if you want." % ROOT)
    if not ask_yes("Remove the server?", False):
        return
    compose("down", check=False)
    dns = openlobby.find_dns(docker_run)
    if dns:
        ok, out = openlobby.dns_remove(dns, KEY, docker_run)
        if not ok:
            say(out)
    say("Removed. Run Nobunaga Server Setup again to set it up anew.")


MENU = [("Start the server", start_server_menu),
        ("Stop the server", stop_server),
        ("Change settings", change_settings),
        ("Update", update),
        ("Make a backup", backup),
        ("Restore a backup", restore),
        ("Add a player account / list the accounts", accounts),
        ("Show what the server is doing (logs)", logs),
        ("Rebuild the game data", rebuild_assets),
        ("Check that the server works", smoke),
        ("Open the router ports again", router_again),
        ("Show what to tell players", tell_players),
        ("Remove the server", uninstall)]


def menu_status(env):
    if not has_assets(env):
        return "game data not built yet"
    return "running" if server_running() else "stopped"


def menu():
    while True:
        env = read_env()
        heading("Nobunaga server: %s (players' DNS: %s)" % (menu_status(env), env.get("NOBU_PUBLIC_IP", "?")))
        for i, (title, _) in enumerate(MENU, 1):
            say("  %2d. %s" % (i, title))
        say("   0. Quit")
        pick = ask("Type a number", "0")
        if pick == "0":
            return
        if pick.isdigit() and 1 <= int(pick) <= len(MENU):
            try:
                MENU[int(pick) - 1][1]()
            except KeyboardInterrupt:
                say()
            except SystemExit as e:
                say(str(e))


def parse_args(argv=None):
    ap = argparse.ArgumentParser(prog="Nobunaga Server Setup", description=(
        "Sets up and runs a Nobunaga's Ambition Online server in Docker. The first run asks three "
        "questions; later runs show a menu."))
    ap.add_argument("--folder", help="the server folder (default: the one this runs from, or "
                                     "'Nobunaga Server' in your home folder for the setup program)")
    ap.add_argument("--dry-run", action="store_true",
                    help="ask the questions and look for the disc, then show what would be done; "
                         "writes nothing and needs no Docker")
    ap.add_argument("--update", action="store_true",
                    help="update the server to the newest release and stop (what an OpenLobby panel's "
                         "Update button runs)")
    ap.add_argument("--yes", action="store_true", help="with --update: never ask, never read the input")
    return ap.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    use_root(find_root(args.folder))
    if args.update:
        return update_now(args.yes)
    try:
        if args.dry_run:
            say("Server folder: %s%s" % (ROOT, "" if os.path.exists(os.path.join(ROOT, "compose.yml")) else
                                         " (the server's files would be unpacked here)"))
            say("Docker: %s" % docker_state())
            first_run(dry_run=True)
            return
        if not ensure_server_files():
            return
        os.chdir(ROOT)
        wait_for_docker()
        if not os.path.exists(ENV):
            first_run()
            return
        env = read_env()
        tell_panel_the_folder(env, read_choices())
        if not has_assets(env) and read_choices().get("disc"):
            say("Setup did not finish last time.")
            if ask_yes("Carry on where it stopped?"):
                complete_setup(env, read_choices())
                return
        menu()
    except KeyboardInterrupt:
        say()
        sys.exit(1)


if __name__ == "__main__":
    code = 0
    try:
        code = main()
    finally:
        if FROZEN and "--yes" not in sys.argv:
            # A double-clicked program's window closes when it ends: keep it open to be read.
            try:
                input("Press Enter to close this window.")
            except (EOFError, KeyboardInterrupt, RuntimeError):
                pass
    sys.exit(code or 0)
