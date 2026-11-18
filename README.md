# pectin-crates: the store

[PectinOS](https://github.com/zigphroid/pectinos) programs and data, as
crates, for `GROCER` to fetch onto a card. A card's
`J:\RECIPES\GROCER\SOURCES.TXT` names this store and its key:

    J:\>GROCER FRESH
    J:\>GROCER FETCH JUGBAND
    J:\>GROCER UPGRADE

`COOKBOOK GROCER` on the board says more.

## What's here

- `crates/NAME.CRT`: every crate, one file each. A crate is a zip whose
  first member is `CRATE.TXT`, its manifest: name, version, what it is,
  the API it needs, the crates it needs (`NEEDS`) and would like (`WANTS`).
  Its other members are what it installs in `JAMS\` and `LARDER\NAME\`,
  and settings it seeds in `RECIPES\` (copied only where there are none).
- `SOURCES.md`: where each crate was built from, a repository and a commit.
- `INDEX.TXT`: a block for each crate, its manifest's lines with its size
  and SHA-256. GROCER plans and checks every download from it.
- `INDEX.SIG`: the Ed25519 signature of `INDEX.TXT`. GROCER refuses an
  index this store's key didn't sign.
- `tools/`: the check every pull request gets (`storecheck.py`, with
  `mkcrate.py`, which reads and writes crates).

Only the store's owner writes `INDEX.TXT` and `INDEX.SIG`, with
`make publish` in the pectinos repository and a key that lives on one
computer. So nothing reaches a card until it has been read and published.

## Sending a crate

1. In your program's own repository (`make new-app` makes one), `make
   preflight` builds `build/NAME.CRT` and checks it as the store will:
   against this store's index, the card paths in your source, the page,
   the settings, the API and size, that the commit is pushed and public,
   and for secrets.
2. `make submit` forks this repository, puts `crates/NAME.CRT` and your
   `SOURCES.md` row (your repository and the full commit) on a branch,
   and opens the pull request with the report as its text. The version
   must go up from the store's.
3. The check below runs on the pull request. It changes only `crates/`
   and `SOURCES.md`.

The check comments with the crate's manifest and files. It refuses a
crate that:
- isn't named as its file;
- owns a file another crate owns;
- seeds settings other than its own (`RECIPES\NAME.*` or `RECIPES\NAME\`);
- needs a crate that isn't in the store;
- doesn't go up in version;
- has no row in `SOURCES.md`;

and a pull request that removes a crate, or changes anything besides
`crates/` and `SOURCES.md`. Once it's reviewed and merged, the next publish puts the
crate in the index.
