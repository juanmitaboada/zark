# zark — Debian/Ubuntu packaging guide

This document is for the maintainer (Juanmi). It explains how to build
a `.deb` locally, how to sign source packages, and how to upload to the
Launchpad PPA `ppa:juanmitaboada/zark`.

The contents of `debian/` are managed in-tree alongside the upstream
source. The package is **non-native** (`3.0 (quilt)`): the upstream
version is read from `lib/config.py` (`VERSION`) and the Debian revision
is `-1`, `-2`, ... for packaging-only fixes.

---

## 1. Prerequisites

### 1.1 Build tools

```sh
sudo apt install \
    devscripts debhelper dh-make \
    lintian \
    fakeroot \
    dput-ng    # newer dput; classic dput also works
```

`devscripts` brings `debuild`, `dch`, `debsign`. `debhelper` provides the
`dh` sequencer. `lintian` is invoked automatically by `debuild`.

### 1.2 GPG key

You need a GPG key whose user-id matches the `Maintainer:` line in
`debian/control` (`Juanmi Taboada <juanmi@juanmitaboada.com>`). Launchpad
uses this key to verify uploads.

Check whether you already have a usable key:

```sh
gpg --list-secret-keys --keyid-format LONG
```

If there is no `sec` line for `juanmi@juanmitaboada.com`, create one:

```sh
gpg --full-generate-key
# Choose: (1) RSA and RSA, 4096 bits, 0 = no expiration
# Real name: Juanmi Taboada
# Email:     juanmi@juanmitaboada.com
```

Note the long key ID (the part after `rsa4096/`). Export the public
half and upload it to a keyserver (Launchpad reads from
`keyserver.ubuntu.com`):

```sh
gpg --send-keys --keyserver keyserver.ubuntu.com <KEY_ID>
```

Then go to <https://launchpad.net/~juanmitaboada/+editpgpkeys>, paste
the **fingerprint** (not the key ID) of the key, and complete the
encrypted-email confirmation Launchpad sends back.

### 1.3 Launchpad account

You already have one (`juanmitaboada`). The PPA `ppa:juanmitaboada/zark`
will be auto-created the first time you `dput` to it; no manual action
is required.

---

## 2. Build a `.deb` locally (no signing)

Useful for iterating on `debian/` without touching the PPA.

```sh
make deb
```

Equivalent to:

```sh
debuild -us -uc -b
```

Result: `../zark_X.Y.Z-1_all.deb` and the artefacts of the build process
(`.changes`, `.buildinfo`, `.dsc`, `.tar.xz`) sitting next to the source
tree. Install it locally to smoke-test:

```sh
sudo dpkg -i ../zark_X.Y.Z-1_all.deb
zark --version          # should print the version from lib/config.py
sudo zark explore       # should run; failures here probably mean a
                        # Recommends should be a Depends
sudo dpkg -P zark       # purge to clean up
```

`debuild` also runs `lintian` against the resulting `.deb`; address any
errors before declaring the build clean. Warnings should be reviewed
case by case — some are legitimately suppressed in
`debian/zark.lintian-overrides`.

---

## 3. Build a signed source package for the PPA

Launchpad accepts only **source packages**: it builds the binaries on
its own farm. The source package is a `.dsc` plus a tarball, signed
with your GPG key.

```sh
make deb-source
```

Equivalent to:

```sh
debuild -S -sa
```

`-S` means source-only; `-sa` includes the original tarball (required
the first time a given upstream version is uploaded). Subsequent uploads
of the same upstream version with a higher revision (`-2`, `-3`) can use
`-sd` to omit the tarball, since Launchpad already has it.

The resulting `.changes` file is signed automatically with the key
matching the `Maintainer:` field, provided that key is in your local
GPG keyring.

---

## 4. Upload to the PPA

Launchpad needs **one source package per target Ubuntu series**. The
PPA covers: noble (24.04), oracular (24.10), plucky (25.04), questing
(25.10), and the 26.04 release codename when it lands. Each upload is
distinguished by a `~ubuntuXX.YY.N` suffix on the version.

The recommended workflow is:

```sh
make deb-ppa
```

This iterates over the configured Ubuntu series, regenerates
`debian/changelog` for each one with the appropriate `~ubuntuXX.YY.1`
suffix, builds a signed source package, and uploads it via `dput`.
The original `debian/changelog` is restored at the end.

If you prefer a manual single-series upload:

```sh
# Edit debian/changelog: change "UNRELEASED" to "noble" (or whichever)
# and append "~ubuntu24.04.1" to the version. Save.
debuild -S -sa
dput -c dput.cf zark-ppa ../zark_X.Y.Z-1~ubuntu24.04.1_source.changes
```

After upload, Launchpad emails you with the build status (usually within
~15 min for accepted builds).

### Why a custom dput profile?

The repo ships a `dput.cf` at the project root with a `[zark-ppa]`
profile that uploads via **HTTPS** to `ppa.launchpad.net`. The
canonical shorthand `ppa:juanmitaboada/zark` resolved by `dput-ng`
defaults to **FTP**, which we observed timing out — many ISPs and
corporate networks block outbound FTP (port 21) by default. HTTPS
goes through the same port (443) as the rest of the Launchpad web,
so it works wherever `git push` does.

`make deb-ppa` invokes `dput -c dput.cf zark-ppa ...` so the local
profile is always picked up regardless of the user's `~/.dput.cf`.

---

## 5. Versioning rules

* **Upstream version** lives in `lib/config.py` as the `VERSION` constant.
  Bump it for every upstream release.
* **Debian revision** (`-1`, `-2`, ...) is bumped only when the packaging
  itself changes between upstream releases. The same upstream `X.Y.Z`
  may produce `X.Y.Z-1` and later `X.Y.Z-2` if a packaging fix is needed.
* **PPA suffix** (`~ubuntuXX.YY.N`) distinguishes per-series builds of the
  same source package. `~` sorts before any letter or digit, so
  `X.Y.Z-1~ubuntu24.04.1` is correctly considered older than
  `X.Y.Z-1` (which would be the upload to Debian sid, if we ever did
  that) and older than `X.Y.Z-2~ubuntu24.04.1`.

The full version string for a typical PPA upload is therefore:

```
X.Y.Z-1~ubuntu24.04.1
^^^^^ ^ ^^^^^^^^^^^^
|     | |
|     | per-series build, increment if you re-upload the same -N
|     packaging revision
upstream version (single source of truth: lib/config.py)
```

---

## 6. Removing zark from a system

Standard apt:

```sh
sudo apt remove zark        # keeps /etc/zark/known_drives.json
sudo apt purge zark         # also removes /etc/zark/
```

`/var/log/zark.log` is **not** managed by the package and is left in
place on uninstall — it may contain forensic information the user
wants to keep.

---

## 7. Troubleshooting

* **`debuild` fails with `gpg: no default secret key`** — your default
  GPG identity does not match the `Maintainer:` line. Either add
  `-k<KEY_ID>` to debuild, or set `DEBSIGN_KEYID` in your shell profile,
  or set the key as default with `gpg --default-key`.
* **`dput` rejects the upload with "Bad signature"** — Launchpad has not
  yet picked up your GPG key. Wait a few minutes after uploading the key
  and retry; allow up to 30 min on first registration.
* **Lintian flags `bad-distribution-in-changes-file`** — the
  `Distribution:` line in `debian/changelog` says `UNRELEASED` (the
  default for development). Change it to the target Ubuntu series
  (e.g. `noble`) before building for upload.
* **`No such file or directory: zark_X.Y.Z.orig.tar.gz`** — non-native
  source format requires an `.orig.tar.gz`. `make deb` regenerates it
  from `make dist` automatically; if running `debuild` by hand, run
  `make dist && cp zark_X.Y.Z.tar.gz ../zark_X.Y.Z.orig.tar.gz` first.
