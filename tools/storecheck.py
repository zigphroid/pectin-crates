#!/usr/bin/env python3
"""The store's checks (jar layout PR 5, DESIGN §8 "The store").

    tools/storecheck.py TREE                 the store's tree as it stands
    tools/storecheck.py --old BASE --new PR [--report FILE]
                                             a pull request: PR's tree against BASE's

The store (zigphroid/pectin-crates) is crates/NAME.CRT, SOURCES.md
(each crate's source repo and commit) and INDEX.TXT/INDEX.SIG, which
only Jacob's `make publish` writes. A tree is sound when every crate
reads, is named as its file, owns no file another crate owns, seeds
only its own settings (RECIPES\\NAME.*, RECIPES\\NAME\\: mkcrate's
seed_ok, so no two crates seed one file), needs only crates the store
has, and has a SOURCES.md row. A pull request also changes nothing but
crates/ and SOURCES.md, removes no crate, and a crate it changes goes
up in version. --report writes what the PR brings, each crate's
manifest and files, as Markdown for the PR's comment. Nothing in a
crate is run: they are read as zips.

The store's PR check (its .github/workflows/check.yml) runs its own copy
of this file and mkcrate.py, which `make publish` keeps up to date.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mkcrate  # noqa: E402
from mkcrate import CrateError  # noqa: E402

# A PR may change these; anything else is the store's own (publish's).
PR_PATHS = (mkcrate.STORE_CRATES + "/", mkcrate.SOURCES_MD)


def load(tree):
    """{NAME: (bytes, manifest, owned, seeds)} and the problems reading them."""
    crates, problems = {}, []
    jar = os.path.join(tree, mkcrate.STORE_CRATES)
    if os.path.islink(jar):
        return crates, ["%s: a link, not a directory" % mkcrate.STORE_CRATES]
    for n in sorted(os.listdir(jar)) if os.path.isdir(jar) else []:
        path = os.path.join(jar, n)
        if os.path.islink(path):
            # A PR's symlink would be followed to a file outside the tree (#1740).
            problems.append("%s/%s: a link; crates are files" % (mkcrate.STORE_CRATES, n))
            continue
        if not n.endswith(".CRT") or not os.path.isfile(path):
            problems.append("%s/%s: crates/ holds NAME.CRT files only" % (mkcrate.STORE_CRATES, n))
            continue
        with open(path, "rb") as f:
            data = f.read()
        try:
            m, owned, seeds = mkcrate.read_crate_bytes(data, n)
        except CrateError as e:
            problems.append(str(e))
            continue
        if m["NAME"] != n[:-4]:
            problems.append("%s: its NAME is %s; the file is NAME.CRT" % (n, m["NAME"]))
            continue
        crates[m["NAME"]] = (data, m, owned, seeds)
    return crates, problems


def check_tree(tree):
    """The problems with the store's tree, [] when it's sound."""
    crates, problems = load(tree)
    owner = {}
    for name in sorted(crates):
        _, m, owned, _ = crates[name]
        for p in owned:
            k = mkcrate.path_key(p)
            if k in owner:
                problems.append("%s: %s belongs to %s" % (name, p, owner[k]))
            else:
                owner[k] = name
        for dep in m["NEEDS"]:
            if dep not in crates:
                problems.append("%s needs %s, which isn't in the store" % (name, dep))
    if os.path.islink(os.path.join(tree, mkcrate.SOURCES_MD)):
        problems.append("%s: a link; it is a file" % mkcrate.SOURCES_MD)
        return problems
    rows = mkcrate.read_sources_md(os.path.join(tree, mkcrate.SOURCES_MD))
    for name in sorted(crates):
        if name not in rows:
            problems.append("%s: no row in %s (its source repo and commit)" % (name, mkcrate.SOURCES_MD))
    for name in sorted(set(rows) - set(crates)):
        problems.append("%s: a row in %s, but no crates/%s.CRT" % (name, mkcrate.SOURCES_MD, name))
    return problems


def files(tree, running=False):
    """{relative path: bytes} of the tree, .git left out, and with running
    (the store's own tree, whose tools/ this check runs from) the
    __pycache__ its Python writes there too, which isn't a change the PR
    made. A PR's own __pycache__ still counts. A link is its
    target's path, not its bytes (os.walk doesn't follow a link to a
    directory, and a link to a file must not be read: it may point out of
    the tree), so a PR that adds one changes the tree and check_tree names it."""
    out = {}
    for here, dirs, names in os.walk(tree):
        dirs[:] = [d for d in dirs if d != ".git" and not (running and d == "__pycache__")]
        for n in names + [d for d in dirs if os.path.islink(os.path.join(here, d))]:
            p = os.path.join(here, n)
            rel = os.path.relpath(p, tree).replace(os.sep, "/")
            if os.path.islink(p):
                out[rel] = b"link:" + os.fsencode(os.readlink(p))
                continue
            with open(p, "rb") as f:
                out[rel] = f.read()
    return out


def check_pr(old, new):
    """(problems, the changed crates' names) for a PR taking old's tree to new's."""
    problems = check_tree(new)
    a, b = files(old, running=True), files(new)
    for p in sorted(set(a) | set(b)):
        if a.get(p) != b.get(p) and not p.startswith(PR_PATHS[0]) and p != PR_PATHS[1]:
            problems.append("%s: a PR changes only crates/ and %s (the rest is publish's)"
                            % (p, mkcrate.SOURCES_MD))
    was, _ = load(old)
    now, _ = load(new)
    changed = []
    for name in sorted(now):
        if name in was and was[name][0] == now[name][0]:
            continue
        changed.append(name)
        if name in was:
            v0, v1 = was[name][1]["VERSION"], now[name][1]["VERSION"]
            if not mkcrate.newer(v1, v0):
                problems.append("%s: version %s isn't after the store's %s" % (name, v1, v0))
    for name in sorted(set(was) - set(now)):
        changed.append(name)
        problems.append("%s: a PR doesn't remove a crate (the store's owner does, in a publish)"
                        % name)
    return problems, changed


def report(old, new, problems, changed):
    """Markdown for the PR: what each changed crate is and holds."""
    was, _ = load(old)
    now, _ = load(new)
    rows = mkcrate.read_sources_md(os.path.join(new, mkcrate.SOURCES_MD))
    old_rows = mkcrate.read_sources_md(os.path.join(old, mkcrate.SOURCES_MD))
    out = ["## Store check", ""]
    if problems:
        out += ["**%d problem%s:**" % (len(problems), "" if len(problems) == 1 else "s"), ""]
        out += ["- %s" % p for p in problems] + [""]
    else:
        out += ["Sound: every crate reads, and nothing clashes.", ""]
    for name in changed:
        if name not in now:
            out += ["### %s: removed" % name, ""]
            continue
        data, m, owned, seeds = now[name]
        verb = "upgraded from %s" % was[name][1]["VERSION"] if name in was else "new"
        out += ["### %s %s (%s)" % (name, m["VERSION"], verb), ""]
        if name in rows:
            out += ["Source: %s at `%s`" % rows[name], ""]
            if name in was and name in old_rows and old_rows[name][0] != rows[name][0]:
                out += ["**Source moved from %s to %s**: this replaces the crate of another repo." %
                        (old_rows[name][0], rows[name][0]), ""]
        out += ["```", mkcrate.format_manifest(m).replace("\r\n", "\n").rstrip(), "```", ""]
        out += ["| Bytes | File |", "|---:|---|"]
        out += ["| %d | `%s` |" % (len(owned[p]), p) for p in sorted(owned, key=str.upper)]
        out += ["| %d | `%s` (a seed) |" % (len(seeds[p]), p) for p in sorted(seeds, key=str.upper)]
        out += ["", "%d bytes, SHA-256 `%s`" % (len(data), mkcrate.sha256_hex(data)), ""]
    return "\n".join(out) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("tree", nargs="?")
    ap.add_argument("--old")
    ap.add_argument("--new")
    ap.add_argument("--report")
    args = ap.parse_args(argv)
    if args.tree:
        problems, changed = check_tree(args.tree), []
    elif args.old and args.new:
        problems, changed = check_pr(args.old, args.new)
        if args.report:
            with open(args.report, "w") as f:
                f.write(report(args.old, args.new, problems, changed))
    else:
        ap.error("a TREE, or --old and --new")
    for p in problems:
        print("storecheck: %s" % p)
    if not problems:
        print("storecheck: sound%s" % ("; changed: " + " ".join(changed) if changed else ""))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
