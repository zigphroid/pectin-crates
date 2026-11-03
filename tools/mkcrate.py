#!/usr/bin/env python3
"""Crates: the jar's packages (DESIGN §8 "Crates", jar layout PR 2).

    tools/mkcrate.py build -o OUT.CRT --name NAME [--version V] [--what TEXT]
                           [--api N] [--needs NAME ...] [--wants NAME ...]
                           [--jam FILE.JAM ...] [--tree CARDJAR=HOSTDIR ...]
                           [--file CARDPATH=HOSTFILE ...] [--seed CARDPATH=HOSTFILE ...]
    tools/mkcrate.py install (--jarcard JARCARD --card CARD.IMG | --dir DIR) [--api N] CRATE.CRT ...
    tools/mkcrate.py show CRATE.CRT
    tools/mkcrate.py sources -o OUT NAME URL PUB  GROCER's SOURCES.TXT seed
    tools/mkcrate.py index -o DIR --source URL CRATE.CRT ...  INDEX.TXT for crates beside it
    tools/mkcrate.py publish --store DIR --crtsign BIN --key KEY --pub PUB
                             --repo URL --commit C --url URL CRATE.CRT ...

A crate is a zip (.CRT) whose paths are relative to J:\\: the program's
.JAM in JAMS\\, what it ships in LARDER\\, settings files it seeds in
RECIPES\\, and CRATE.TXT, its manifest. `build` makes one from the
build's files, the same bytes from the same files (fixed dates, sorted
names), so a changed crate is a changed program. `install` puts crates
on a SIMMER card image through jarcard, as GROCER ADD will on the board
(PR 3), and writes each one's record, J:\\KEEP\\GROCER\\NAME.LST: the
manifest, then a line for each file it owns with the file's CRC-32 and
size. `make sim-card` builds its card this way, so a card has its
database from the first boot.

The manifest is a line a field, a key, spaces, the value:

    NAME     JUGBAND               up to 8 of A-Z 0-9 _ (the .LST's name)
    VERSION  1.1                   up to 15 of 0-9 A-Z a-z . + -
    WHAT     synths, drums and a sequencer, from MIDI or the keys
    API      52                    the kernel's API it needs (the .JAMs' min_api)
    NEEDS    SDK                   crates that must be there: ADD refuses without
    WANTS    KITCHEN HOMEMADE PORCH   crates it uses if there: said, not enforced
    SEED     RECIPES\\X\\Y.TXT     copied only where missing; never owned (repeats)

Dependencies are names, checked, not solved: every .JAM carries its own
copy of the SDK, so nothing needs a version range (plan question 5).
"""
import argparse
import io
import os
import re
import struct
import subprocess
import sys
import tempfile
import zipfile
import zlib

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
MANIFEST = "CRATE.TXT"
GROCER_JAR = "KEEP\\GROCER"
NAME_RE = re.compile(r"^[A-Z0-9_]{1,8}$")
VERSION_RE = re.compile(r"^[0-9A-Za-z.+-]{1,15}$")
WHAT_MAX = 72
KEYS = ("NAME", "VERSION", "WHAT", "API", "NEEDS", "WANTS", "SEED")
ZIP_DATE = (1980, 1, 1, 0, 0, 0)        # the earliest a zip can say: no build time in the bytes
# A crate owns files in these jars only: KEEP is what programs write and
# WORK and NOTES the person's (DESIGN §8 "The jar's layout").
OWNED_TOP = ("JAMS", "LARDER")
JAM_MAGIC = b"JAM\x1a"


class CrateError(Exception):
    pass


# ---- Paths ----

def card_path(path):
    """A crate path in the card's spelling, J:\\-relative with backslashes,
    checked: no drive, no leading slash, no empty, . or .. parts."""
    p = path.replace("/", "\\")
    parts = p.split("\\")
    if ":" in p or not p or any(part in ("", ".", "..") for part in parts):
        raise CrateError("%r is not a path inside the jar" % path)
    if any(c in p for c in '*?"<>|') or any(ord(c) < 32 for c in p):
        raise CrateError("%r has a character FAT can't hold" % path)
    return p


def path_key(path):
    """FAT's names are found in any case."""
    return card_path(path).upper()


def check_owned(path):
    p = card_path(path)
    top = p.split("\\")[0].upper()
    if top not in OWNED_TOP:
        raise CrateError("%s: a crate's files go in JAMS or LARDER (or RECIPES as a SEED)" % p)
    if top == "JAMS" and (p.count("\\") != 1 or not p.upper().endswith(".JAM")):
        raise CrateError("%s: JAMS holds programs, one .JAM each" % p)
    return p


# ---- The manifest ----

def format_manifest(m):
    lines = ["%-8s %s" % ("NAME", m["NAME"]), "%-8s %s" % ("VERSION", m["VERSION"])]
    if m.get("WHAT"):
        lines.append("%-8s %s" % ("WHAT", m["WHAT"]))
    if m.get("API"):
        lines.append("%-8s %d" % ("API", m["API"]))
    for key in ("NEEDS", "WANTS"):
        if m.get(key):
            lines.append("%-8s %s" % (key, " ".join(m[key])))
    for s in m.get("SEED", []):
        lines.append("%-8s %s" % ("SEED", s))
    return "".join(line + "\r\n" for line in lines)


def parse_manifest(text, extra=()):
    """The manifest's fields, checked. extra names the keys a .LST adds
    (FILE, SEEDED), returned as lists of their raw values."""
    m = {"NEEDS": [], "WANTS": [], "SEED": []}
    for key in extra:
        m[key] = []
    for n, line in enumerate(text.replace("\r\n", "\n").split("\n"), 1):
        if not line.strip():
            continue
        parts = line.split(None, 1)
        key, value = parts[0], (parts[1].strip() if len(parts) > 1 else "")
        if key in extra:
            m[key].append(value)
        elif key in ("NEEDS", "WANTS"):
            m[key] += value.split()
        elif key == "SEED":
            m[key].append(value)
        elif key in KEYS:
            if key in m:
                raise CrateError("line %d: a second %s" % (n, key))
            m[key] = value
        else:
            raise CrateError("line %d: no such field %r" % (n, key))
    check_manifest(m)
    return m


def check_manifest(m):
    if not NAME_RE.match(m.get("NAME", "")):
        raise CrateError("NAME %r: 1 to 8 of A-Z, 0-9 and _" % m.get("NAME"))
    if not VERSION_RE.match(m.get("VERSION", "")):
        raise CrateError("%s: VERSION %r: 1 to 15 of 0-9, A-Z, a-z, '.', '+', '-'"
                         % (m["NAME"], m.get("VERSION")))
    if len(m.get("WHAT", "")) > WHAT_MAX:
        raise CrateError("%s: WHAT is over %d characters" % (m["NAME"], WHAT_MAX))
    api = m.setdefault("API", 0)
    if isinstance(api, str):
        if not api.isdigit():
            raise CrateError("%s: API %r is not a number" % (m["NAME"], api))
        m["API"] = int(api)
    for key in ("NEEDS", "WANTS"):
        for dep in m[key]:
            if not NAME_RE.match(dep):
                raise CrateError("%s: %s %r is not a crate's name" % (m["NAME"], key, dep))
            if dep == m["NAME"]:
                raise CrateError("%s %s itself" % (m["NAME"], key))
    for s in m["SEED"]:
        if not seed_ok(m["NAME"], s):
            raise CrateError("%s: SEED %s isn't its own settings (RECIPES\\%s.* or RECIPES\\%s\\)"
                             % (m["NAME"], s, m["NAME"], m["NAME"]))


def seed_ok(name, path):
    """A crate seeds only its own settings, RECIPES\\NAME.* or a file in
    RECIPES\\NAME\\, so no crate can choose another program's (where
    FUNNEL sends a password, GROCER's sources): grocer's crate.c seed_ok."""
    parts = card_path(path).upper().split("\\")
    if len(parts) < 2 or parts[0] != "RECIPES":
        return False
    if len(parts) > 2:
        return parts[1] == name
    return parts[1].split(".", 1)[0] == name and "." in parts[1]


# ---- .JAM headers (sdk/pectin_abi.h, struct jam_header) ----

def jam_min_api(data, what):
    if data[:4] != JAM_MAGIC or len(data) < 128:
        raise CrateError("%s: not a .JAM" % what)
    return struct.unpack_from("<I", data, 28)[0]


def jam_page_what(data):
    """The WHAT of a program's COOKBOOK page, its first line's
    "# NAME(1) - what", from the .JAM's PAGE resource (DESIGN §9, v3)."""
    if len(data) < 128 or struct.unpack_from("<H", data, 4)[0] < 3:
        return ""
    res_off, count = struct.unpack_from("<II", data, 72)
    for i in range(count):
        rtype, _, off, size, _ = struct.unpack_from("<4sIIII", data, res_off + 20 * i)
        if rtype == b"PAGE":
            first = data[off:off + size].split(b"\n", 1)[0].decode("utf-8", "replace")
            m = re.match(r"#\s*\S+\(\d\)\s+-\s+(.*\S)", first)
            return m.group(1) if m else ""
    return ""


# ---- Building ----

def gather(args):
    """[(card path, bytes)] from build's --jam, --tree, --file and --seed."""
    files = []
    for jam in args.jam:
        with open(jam, "rb") as f:
            files.append(("JAMS\\" + os.path.basename(jam).upper(), f.read()))
    for spec in args.tree:
        jar, host = split_spec(spec)
        found = False
        for top, dirs, names in os.walk(host):
            dirs.sort()
            for n in sorted(names):
                if n.startswith("."):
                    continue
                rel = os.path.relpath(os.path.join(top, n), host).replace(os.sep, "\\")
                with open(os.path.join(top, n), "rb") as f:
                    files.append((card_path(jar) + "\\" + rel, f.read()))
                found = True
        if not found:
            raise CrateError("%s: no files" % host)
    for spec in args.file:
        path, host = split_spec(spec)
        with open(host, "rb") as f:
            files.append((card_path(path), f.read()))
    seeds = []
    for spec in args.seed:
        path, host = split_spec(spec)
        with open(host, "rb") as f:
            seeds.append((card_path(path), f.read()))
    return files, seeds


def split_spec(spec):
    if "=" not in spec:
        raise CrateError("%r: CARDPATH=HOSTPATH" % spec)
    return spec.split("=", 1)


def make_crate(manifest, files, seeds=()):
    """The crate's bytes: CRATE.TXT first, then the files sorted by path,
    deflated, with no dates or host bits in them."""
    m = dict(manifest)
    m.setdefault("NEEDS", [])
    m.setdefault("WANTS", [])
    m["SEED"] = sorted(p for p, _ in seeds)
    api = m.get("API", 0) or 0
    seen = {}
    for path, data in files:
        check_owned(path)
        if path.upper().startswith("JAMS\\"):
            api = max(api, jam_min_api(data, path))
        if path_key(path) in seen:
            raise CrateError("%s is in the crate twice" % path)
        seen[path_key(path)] = path
    for path, _ in seeds:
        if path_key(path) in seen:
            raise CrateError("%s is in the crate twice" % path)
        seen[path_key(path)] = path
    m["API"] = api
    check_manifest(m)
    if not files and not m["NEEDS"]:
        raise CrateError("%s: a crate with no files needs NEEDS (BASE's kind)" % m["NAME"])
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        entries = [(MANIFEST, format_manifest(m).encode("ascii"))]
        entries += sorted(((p.replace("\\", "/"), d) for p, d in list(files) + list(seeds)),
                          key=lambda e: e[0].upper())
        for name, data in entries:
            info = zipfile.ZipInfo(name, ZIP_DATE)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 0              # MS-DOS: no Unix mode bits
            info.external_attr = 0
            z.writestr(info, data, compresslevel=9)
    return out.getvalue()


def read_crate(path, what=None):
    """(manifest, {card path: bytes} of owned files, {card path: bytes} of
    seeds), checked as install would. path may be a file object, what
    its name in messages."""
    what = what or path
    try:
        z = zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile) as e:
        raise CrateError("%s: not a crate (%s)" % (what, e))
    with z:
        names = z.namelist()
        if MANIFEST not in names:
            raise CrateError("%s: no %s" % (what, MANIFEST))
        m = parse_manifest(z.read(MANIFEST).decode("ascii"))
        seeds = {path_key(s) for s in m["SEED"]}
        owned, seeded = {}, {}
        for name in names:
            if name == MANIFEST or name.endswith("/"):
                continue
            p = card_path(name)
            if path_key(p) in seeds:
                seeded[p] = z.read(name)
            else:
                check_owned(p)
                owned[p] = z.read(name)
        missing = seeds - {path_key(p) for p in seeded}
        if missing:
            raise CrateError("%s: SEED %s is not in the crate" % (what, sorted(missing)[0]))
    return m, owned, seeded


# ---- The record: KEEP\GROCER\NAME.LST ----

def crc32(data):
    return zlib.crc32(data) & 0xffffffff


def format_record(m, owned, seeded, source=None):
    """The manifest, FROM (where the crate came from: a crate's path on the
    card for GROCER ADD, PC for make sim-card), then FILE CRC BYTES PATH
    for each owned file and SEEDED for each seed this install put there
    (DROP may take one only while its CRC still matches: an edited seed is
    the person's). GROCER's crate.c writes the same bytes."""
    lines = [format_manifest(m)]
    if source:
        lines.append("%-8s %s\r\n" % ("FROM", source))
    for p in sorted(owned, key=str.upper):
        lines.append("%-8s %08x %d %s\r\n" % ("FILE", crc32(owned[p]), len(owned[p]), p))
    for p in sorted(seeded, key=str.upper):
        lines.append("%-8s %08x %d %s\r\n" % ("SEEDED", crc32(seeded[p]), len(seeded[p]), p))
    return "".join(lines)


def parse_record(text):
    """(manifest, {path: (crc, size)} owned, {path: (crc, size)} seeded).
    The manifest's FROM is its source, or None."""
    m = parse_manifest(text, extra=("FILE", "SEEDED", "FROM"))
    froms = m.pop("FROM")
    if len(froms) > 1:
        raise CrateError("%s: a second FROM" % m["NAME"])
    m["FROM"] = froms[0] if froms else None
    tables = []
    for key in ("FILE", "SEEDED"):
        t = {}
        for value in m.pop(key):
            parts = value.split(None, 2)
            if len(parts) != 3 or not re.match(r"^[0-9a-f]{8}$", parts[0]) or not parts[1].isdigit():
                raise CrateError("%s: bad %s line %r" % (m["NAME"], key, value))
            t[card_path(parts[2])] = (int(parts[0], 16), int(parts[1]))
        tables.append(t)
    return m, tables[0], tables[1]


# ---- Installing on a SIMMER card (jarcard) ----

class Card:
    def __init__(self, jarcard, img):
        self.jarcard, self.img = jarcard, img
        self.dirs = {}

    def run(self, *args):
        p = subprocess.run([self.jarcard, self.img] + list(args), capture_output=True, text=True,
                           env=dict(os.environ, ASAN_OPTIONS="detect_leaks=0"))
        if p.returncode:
            raise CrateError("jarcard %s: %s" % (" ".join(args), p.stderr.strip()))
        return p.stdout

    def names(self, jar):
        """The upper-case names (short and long) in a jar, or None if
        there's no such jar."""
        key = jar.upper()
        if key not in self.dirs:
            p = subprocess.run([self.jarcard, self.img, "ls", "\\" + jar], capture_output=True,
                               text=True, env=dict(os.environ, ASAN_OPTIONS="detect_leaks=0"))
            if p.returncode:
                self.dirs[key] = None
            else:
                got = set()
                for line in p.stdout.splitlines():
                    short = line[:12].strip()
                    rest = line[14:].split(None, 1)
                    got.add(short.upper())
                    if len(rest) == 2:
                        got.add(rest[1].strip().upper())
                self.dirs[key] = got
        return self.dirs[key]

    def has(self, path):
        jar, _, name = card_path(path).rpartition("\\")
        names = self.names(jar) if jar else self.names("")
        return names is not None and name.upper() in names

    def get(self, path):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "f")
            self.run("get", "\\" + path, out)
            with open(out, "rb") as f:
                return f.read()

    def records(self):
        """{NAME: (manifest, owned, seeded)} from KEEP\\GROCER\\*.LST."""
        names = self.names(GROCER_JAR)
        recs = {}
        for n in sorted(names or ()):
            if n.endswith(".LST"):
                m, owned, seeded = parse_record(self.get(GROCER_JAR + "\\" + n).decode("ascii"))
                recs[m["NAME"]] = (m, owned, seeded)
        return recs

    def write(self, puts, removes):
        """puts [(card path, bytes)] and removes [card path] in one jarcard run."""
        with tempfile.TemporaryDirectory() as tmp:
            lines = ["rm \\%s" % p for p in removes]
            for i, (path, data) in enumerate(puts):
                host = os.path.join(tmp, "%d" % i)
                with open(host, "wb") as f:
                    f.write(data)
                lines.append("put %s\t\\%s" % (host, path))
            batch = os.path.join(tmp, "batch")
            with open(batch, "w") as f:
                f.write("".join(line + "\n" for line in lines))
            self.run("batch", batch)
        self.dirs.clear()


class DirCard:
    """A card's tree in a host directory (the release's card zip, before
    it's zipped): the same calls as Card. Names match whatever their case,
    as FAT's do; a new jar or file takes the case it's written with."""

    def __init__(self, root):
        self.root = root
        os.makedirs(root, exist_ok=True)

    def _find(self, path, make=False):
        """The host path for a card path, or None; make: create its jars."""
        here = self.root
        parts = [q for q in card_path(path).split("\\") if q]
        for i, part in enumerate(parts):
            last = i == len(parts) - 1
            found = None
            if os.path.isdir(here):
                found = next((n for n in os.listdir(here) if n.upper() == part.upper()), None)
            if found is None:
                if not make:
                    return None
                found = part
                if not last:
                    os.makedirs(os.path.join(here, found))
            here = os.path.join(here, found)
        return here

    def names(self, jar):
        host = self._find(jar) if jar else self.root
        if host is None or not os.path.isdir(host):
            return None
        return {n.upper() for n in os.listdir(host)}

    def has(self, path):
        host = self._find(path)
        return host is not None and os.path.isfile(host)

    def get(self, path):
        with open(self._find(path), "rb") as f:
            return f.read()

    def records(self):
        names = self.names(GROCER_JAR)
        recs = {}
        for n in sorted(names or ()):
            if n.endswith(".LST"):
                m, owned, seeded = parse_record(self.get(GROCER_JAR + "\\" + n).decode("ascii"))
                recs[m["NAME"]] = (m, owned, seeded)
        return recs

    def write(self, puts, removes):
        for p in removes:
            host = self._find(p)
            if host is not None:
                os.remove(host)
        for path, data in puts:
            with open(self._find(path, make=True), "wb") as f:
                f.write(data)


def plan_order(crates):
    """The crates in an order where each comes after the batch's crates it
    NEEDS (BASE last); a cycle is refused."""
    by_name = {m["NAME"]: (m, o, s) for m, o, s in crates}
    if len(by_name) != len(crates):
        raise CrateError("a crate is named twice in one install")
    done, order = set(), []

    def visit(name, path):
        if name in done or name not in by_name:
            return
        if name in path:
            raise CrateError("NEEDS go round in a circle: %s" % " -> ".join(path + [name]))
        for dep in by_name[name][0]["NEEDS"]:
            visit(dep, path + [name])
        done.add(name)
        order.append(by_name[name])

    for name in sorted(by_name):
        visit(name, [])
    return order


def install(card, paths, api, say=print, source="PC"):
    """Put the crates at paths on card, upgrading ones already there. Refuses
    (before writing anything for that crate) a NEEDS that's missing, a
    newer API than api, or a file another crate owns."""
    recs = card.records()
    crates = plan_order([read_crate(p) for p in paths])
    for m, owned, seeds in crates:
        name = m["NAME"]
        if m["API"] > api:
            raise CrateError("%s needs API %d; this kernel has %d" % (name, m["API"], api))
        for dep in m["NEEDS"]:
            if dep not in recs:
                raise CrateError("%s needs %s, which isn't installed or in this install" % (name, dep))
        mine = {path_key(p) for p in owned}
        for other, (_, theirs, _) in recs.items():
            if other == name:
                continue
            clash = mine & {path_key(p) for p in theirs}
            if clash:
                raise CrateError("%s: %s belongs to %s" % (name, sorted(clash)[0], other))
        old = recs.get(name)
        gone = []
        seeded = {}
        if old:
            gone = [p for p in old[1] if path_key(p) not in mine and card.has(p)]
            seeded = {p: old[2][p] for p in old[2]}
        puts = sorted(owned.items(), key=lambda e: e[0].upper())
        new_seeded = {}
        for p, data in sorted(seeds.items()):
            prev = [q for q in seeded if path_key(q) == path_key(p)]
            if prev:
                new_seeded[prev[0]] = seeded[prev[0]]   # an earlier install seeded it: leave it
            elif not card.has(p):
                puts.append((p, data))
                new_seeded[p] = (crc32(data), len(data))
            elif card.get(p) == data:
                # The crate's own bytes: an install cut after the copy and
                # before the record (GROCER's crate.c does the same).
                new_seeded[p] = (crc32(data), len(data))
        record = format_record(m, owned, {}, source(name) if callable(source) else source)
        record += "".join("%-8s %08x %d %s\r\n" % ("SEEDED", c, n, p)
                          for p, (c, n) in sorted(new_seeded.items(), key=lambda e: e[0].upper()))
        card.write(puts, gone)
        # The record last: a cut before it leaves the old one, which
        # GROCER CHECK reports and a second ADD finishes (DESIGN §8).
        card.write([(GROCER_JAR + "\\" + name + ".LST", record.encode("ascii"))], [])
        recs[name] = (m, {p: (crc32(d), len(d)) for p, d in owned.items()}, new_seeded)
        say("%s %s %s: %d files%s" % ("upgraded" if old else "installed", name, m["VERSION"],
                                      len(owned), ", %d seeded" % len(new_seeded) if new_seeded else ""))
    # A want is said, not enforced: the program runs without it. Said once
    # the whole install is in, so a want in the same install counts.
    for m, _, _ in crates:
        missing = [w for w in m["WANTS"] if w not in recs]
        if missing:
            say("%s wants %s, not installed (it runs without)" % (m["NAME"], " ".join(missing)))
    return recs


# ---- The store (jar layout PR 5, DESIGN §8 "The store") ----
#
# A source is a URL with INDEX.TXT, INDEX.SIG and the crates below it.
# The store is zigphroid/pectin-crates, served by raw.githubusercontent:
# crates/NAME.CRT, SOURCES.md (each crate's source repo and commit), and
# the index, which only `publish` writes, signed with the key on Jacob's
# Mac. GROCER's store.c reads the same index (apps/grocer/store.h).

STORE_CRATES = "crates"
INDEX = "INDEX.TXT"
INDEX_SIG = "INDEX.SIG"
SOURCES_MD = "SOURCES.md"
NEEDS_A_LINE = 8                # NEEDS names a line in the index, so a line stays short


def read_crate_bytes(data, what="the crate"):
    return read_crate(io.BytesIO(data), what)


def version_key(v):
    """For "newer": the parts between . + -, numbers as numbers (1.10 is
    after 1.9), words after numbers."""
    return [(0, int(p), "") if p.isdigit() else (1, 0, p) for p in re.split(r"[.+-]", v)]


def newer(a, b):
    """a is a later version than b."""
    return version_key(a) > version_key(b)


def same_content(a, b):
    """Two crates' bytes hold the same crate but for its VERSION."""
    ma, oa, sa = read_crate_bytes(a)
    mb, ob, sb = read_crate_bytes(b)
    strip = lambda m: {k: v for k, v in m.items() if k != "VERSION"}
    return strip(ma) == strip(mb) and oa == ob and sa == sb


def next_version(store_version, built):
    """The version a changed crate goes into the store with: the build's
    own (the kernel's, 1.1) when it's later than the store's, else the
    store's with one more on its last part past the build's: 1.1.1, 1.1.2."""
    if newer(built, store_version):
        return built
    rest = store_version[len(built) + 1:] if store_version.startswith(built + ".") else ""
    v = "%s.%d" % (built, int(rest) + 1) if rest.isdigit() else built + ".1"
    if not newer(v, store_version) or not VERSION_RE.match(v):
        raise CrateError("can't make a version after %s from %s" % (store_version, built))
    return v


def with_version(data, version):
    """The crate's bytes again with another VERSION."""
    m, owned, seeds = read_crate_bytes(data)
    m = dict(m, VERSION=version)
    return make_crate(m, sorted(owned.items()), sorted(seeds.items()))


def sha256_hex(data):
    import hashlib
    return hashlib.sha256(data).hexdigest()


def format_index(entries, serial, source):
    """INDEX.TXT for [(path below the source, crate bytes)]: a head, then a
    block a crate, by name, CR LF lines. The head's SERIAL only goes up
    from one publish to the next, so GROCER FRESH can refuse an older
    index replayed; SOURCE is the URL it's published at, so an index
    signed for one source is refused at another (store.c's fresh_one)."""
    if not (0 < serial <= 0xffffffff):
        raise CrateError("SERIAL %r: 1 to 4294967295" % serial)
    blocks = []
    for path, data in sorted(entries, key=lambda e: read_crate_bytes(e[1], e[0])[0]["NAME"]):
        m = read_crate_bytes(data, path)[0]
        lines = ["%-8s %s" % ("NAME", m["NAME"]), "%-8s %s" % ("VERSION", m["VERSION"])]
        if m.get("WHAT"):
            lines.append("%-8s %s" % ("WHAT", m["WHAT"]))
        if m.get("API"):
            lines.append("%-8s %d" % ("API", m["API"]))
        for key in ("NEEDS", "WANTS"):
            names = m.get(key) or []
            for i in range(0, len(names), NEEDS_A_LINE):
                lines.append("%-8s %s" % (key, " ".join(names[i:i + NEEDS_A_LINE])))
        lines += ["%-8s %s" % ("CRATE", path), "%-8s %d" % ("BYTES", len(data)),
                  "%-8s %s" % ("SHA256", sha256_hex(data))]
        blocks.append("".join(line + "\r\n" for line in lines))
    head = ("; PectinOS's crates: GROCER FRESH reads this, and checks INDEX.SIG signs it.\r\n"
            "%-8s %d\r\n%-8s %s\r\n" % ("SERIAL", serial, "SOURCE", source))
    return head + "".join("\r\n" + b for b in blocks)


def index_head(text):
    """{key: value} of the lines before the first NAME: SERIAL and SOURCE."""
    head = {}
    for line in text.replace("\r\n", "\n").split("\n"):
        line = line.strip()
        if not line or line[0] in ";#":
            continue
        parts = line.split(None, 1)
        if parts[0] == "NAME":
            break
        head[parts[0]] = parts[1] if len(parts) > 1 else ""
    return head


def next_serial(old_text, now=None):
    """The SERIAL a new index takes: the time, or one past the old one's."""
    import time
    old = index_head(old_text).get("SERIAL", "0") if old_text else "0"
    return max(int(now if now is not None else time.time()), int(old) + 1 if old.isdigit() else 1)


def parse_index(text):
    """{NAME: {fields}} from INDEX.TXT, as store.c reads it."""
    out, cur = {}, None
    for n, line in enumerate(text.replace("\r\n", "\n").split("\n"), 1):
        line = line.strip()
        if not line or line[0] in ";#":
            continue
        parts = line.split(None, 1)
        key, value = parts[0], (parts[1] if len(parts) > 1 else "")
        if key == "NAME":
            if value in out:
                raise CrateError("index line %d: a second %s" % (n, value))
            cur = out[value] = {"NAME": value, "NEEDS": [], "WANTS": []}
        elif cur is None:
            continue                    # the head: index_head reads it
        elif key in ("NEEDS", "WANTS"):
            cur[key] += value.split()
        else:
            cur[key] = value
    return out


def format_sources(lines):
    """RECIPES\\GROCER\\SOURCES.TXT: [(name, url, key hex)]."""
    head = ["; GROCER's sources: NAME URL KEY, a line each (COOKBOOK GROCER).",
            "; GROCER SOURCE adds and drops them; so does any editor."]
    return "".join(s + "\r\n" for s in head + ["%s %s %s" % s for s in lines])


def read_key_hex(path, n=32):
    with open(path) as f:
        text = f.read().strip()
    if not re.match(r"^[0-9a-f]{%d}$" % (2 * n), text):
        raise CrateError("%s: not %d hex digits" % (path, 2 * n))
    return text


def read_sources_md(path):
    """{NAME: (repo, commit)} from SOURCES.md's table rows."""
    rows = {}
    if not os.path.exists(path):
        return rows
    with open(path) as f:
        for line in f:
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) == 3 and NAME_RE.match(cells[0]):
                rows[cells[0]] = (cells[1], cells[2])
    return rows


def write_sources_md(path, rows):
    out = ["# Where each crate comes from", "",
           "Each crate in `crates/` is built from this repository at this commit.", "",
           "| Crate | Source | Commit |", "|---|---|---|"]
    out += ["| %s | %s | %s |" % (n, rows[n][0], rows[n][1]) for n in sorted(rows)]
    with open(path, "w") as f:
        f.write("\n".join(out) + "\n")


def store_crates(store):
    """{NAME: crate bytes} from the store's crates/."""
    out = {}
    jar = os.path.join(store, STORE_CRATES)
    for n in sorted(os.listdir(jar)) if os.path.isdir(jar) else []:
        if n.endswith(".CRT"):
            with open(os.path.join(jar, n), "rb") as f:
                out[n[:-4]] = f.read()
    return out


def publish(store, crates, source, crtsign, key, pub, url, say=print):
    """Our built crates into the store, each changed one with a version
    after the store's, then the index made from every crate there and
    signed. Returns the names that changed. The store's tree is checked
    first (tools/storecheck.py), and the signature against pub."""
    import storecheck
    problems = storecheck.check_tree(store)     # before ours go in: nothing left behind
    if problems:
        raise CrateError("the store: " + "; ".join(problems))
    have = store_crates(store)
    rows = read_sources_md(os.path.join(store, SOURCES_MD))
    changed = []
    os.makedirs(os.path.join(store, STORE_CRATES), exist_ok=True)
    for path in crates:
        with open(path, "rb") as f:
            data = f.read()
        m = read_crate_bytes(data, path)[0]
        name = m["NAME"]
        old = have.get(name)
        if old is not None and same_content(old, data):
            continue
        if old is not None:
            version = next_version(read_crate_bytes(old)[0]["VERSION"], m["VERSION"])
            if version != m["VERSION"]:
                data = with_version(data, version)
        with open(os.path.join(store, STORE_CRATES, name + ".CRT"), "wb") as f:
            f.write(data)
        rows[name] = source
        changed.append(name)
        say("publish: %s %s" % (name, read_crate_bytes(data)[0]["VERSION"]))
    write_sources_md(os.path.join(store, SOURCES_MD), rows)
    problems = storecheck.check_tree(store)
    if problems:
        raise CrateError("the store: " + "; ".join(problems))
    entries = [("%s/%s.CRT" % (STORE_CRATES, n), d) for n, d in store_crates(store).items()]
    index = os.path.join(store, INDEX)
    old = open(index).read() if os.path.exists(index) else ""
    with open(index, "wb") as f:
        f.write(format_index(entries, next_serial(old), url).encode("ascii"))
    sig = os.path.join(store, INDEX_SIG)
    new = sig + ".new"
    for args in (["sign", key, index, new], ["verify", pub, index, new]):
        r = subprocess.run([crtsign] + args)
        if r.returncode != 0:
            raise CrateError("crtsign %s failed%s" % (args[0], ": the key isn't %s's" % pub
                                                     if args[0] == "verify" else ""))
    os.replace(new, sig)
    tools = os.path.join(store, "tools")
    os.makedirs(tools, exist_ok=True)
    for n in ("mkcrate.py", "storecheck.py"):          # the PR check's own copies
        with open(os.path.join(ROOT, "tools", n), "rb") as f:
            data = f.read()
        with open(os.path.join(tools, n), "wb") as f:
            f.write(data)
    say("publish: %d changed; %d crates in the index, signed" % (len(changed), len(entries)))
    return changed


def sdk_api():
    with open(os.path.join(ROOT, "sdk", "pectin_abi.h")) as f:
        return int(re.search(r"^#define\s+PECTIN_API_VERSION\s+(\d+)u?\b", f.read(), re.M).group(1))


def kernel_version():
    with open(os.path.join(ROOT, "kernel", "version.h")) as f:
        text = f.read()
    major = re.search(r"PECTIN_VERSION_MAJOR\s+(\d+)", text).group(1)
    minor = re.search(r"PECTIN_VERSION_MINOR\s+(\d+)", text).group(1)
    return "%s.%s" % (major, minor)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("-o", "--output", required=True)
    b.add_argument("--name", required=True)
    b.add_argument("--version", default=None, help="default: the kernel's, kernel/version.h")
    b.add_argument("--what", default=None, help="default: the first .JAM's COOKBOOK page")
    b.add_argument("--api", default="0", help="a number, or 'sdk' for sdk/pectin_abi.h's")
    b.add_argument("--needs", nargs="*", default=[])
    b.add_argument("--wants", nargs="*", default=[])
    b.add_argument("--jam", nargs="*", default=[])
    b.add_argument("--tree", nargs="*", default=[])
    b.add_argument("--file", nargs="*", default=[])
    b.add_argument("--seed", nargs="*", default=[])
    i = sub.add_parser("install")
    i.add_argument("--jarcard")
    i.add_argument("--card", help="a SIMMER card image (with --jarcard)")
    i.add_argument("--dir", help="a card's tree in a directory instead")
    i.add_argument("--api", type=int, default=None, help="default: sdk/pectin_abi.h's")
    i.add_argument("crates", nargs="+")
    s = sub.add_parser("show")
    s.add_argument("crate")
    so = sub.add_parser("sources")
    so.add_argument("-o", "--output", required=True)
    so.add_argument("name")
    so.add_argument("url")
    so.add_argument("pub")
    ix = sub.add_parser("index")
    ix.add_argument("-o", "--output", required=True, help="the directory the crates are served from")
    ix.add_argument("--source", required=True, help="the URL the directory is served at")
    ix.add_argument("crates", nargs="+")
    pu = sub.add_parser("publish")
    pu.add_argument("--store", required=True)
    pu.add_argument("--crtsign", required=True)
    pu.add_argument("--key", required=True)
    pu.add_argument("--pub", required=True)
    pu.add_argument("--repo", required=True)
    pu.add_argument("--commit", required=True)
    pu.add_argument("--url", required=True, help="the URL the store is read at")
    pu.add_argument("crates", nargs="+")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "build":
            files, seeds = gather(args)
            what = args.what
            if what is None:
                what = next((jam_page_what(d) for p, d in files if p.upper().startswith("JAMS\\")), "")
            m = {"NAME": args.name, "VERSION": args.version or kernel_version(), "WHAT": what,
                 "API": sdk_api() if args.api == "sdk" else int(args.api),
                 "NEEDS": sorted(set(args.needs)), "WANTS": sorted(set(args.wants))}
            data = make_crate(m, files, seeds)
            tmp = args.output + ".tmp"
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, args.output)
        elif args.cmd == "sources":
            text = format_sources([(args.name, args.url, read_key_hex(args.pub))])
            with open(args.output, "wb") as f:
                f.write(text.encode("ascii"))
        elif args.cmd == "index":
            entries = []
            for path in args.crates:
                with open(path, "rb") as f:
                    entries.append((os.path.relpath(path, args.output).replace(os.sep, "/"), f.read()))
            index = os.path.join(args.output, INDEX)
            old = open(index).read() if os.path.exists(index) else ""
            with open(index, "wb") as f:
                f.write(format_index(entries, next_serial(old), args.source).encode("ascii"))
        elif args.cmd == "publish":
            publish(args.store, args.crates, (args.repo, args.commit), args.crtsign, args.key, args.pub,
                    args.url)
        elif args.cmd == "install":
            if bool(args.dir) == bool(args.card and args.jarcard):
                raise CrateError("install takes --dir, or --jarcard and --card")
            card = DirCard(args.dir) if args.dir else Card(args.jarcard, args.card)
            install(card, args.crates, sdk_api() if args.api is None else args.api)
        else:
            m, owned, seeded = read_crate(args.crate)
            sys.stdout.write(format_manifest(m).replace("\r\n", "\n"))
            for p in sorted(owned, key=str.upper):
                print("  %9d  %s" % (len(owned[p]), p))
            for p in sorted(seeded, key=str.upper):
                print("  %9d  %s (seed)" % (len(seeded[p]), p))
    except CrateError as e:
        print("mkcrate: %s" % e, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
