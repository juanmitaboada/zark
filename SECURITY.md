# Security Policy

## Supported versions

zark follows [semantic versioning](https://semver.org/). Only the latest
release on the `main` branch receives security fixes.

| Version | Supported          |
| ------- | ------------------ |
| Latest  | :white_check_mark: |
| Older   | :x:                |

If you are running a release older than the current one, please upgrade
before reporting — the issue may already be fixed.

## Reporting a vulnerability

zark runs as **root** by design, operates on **whole disks**, and
manages **encryption keys**. A vulnerability in zark could result in
data loss, key disclosure, or unauthorized system access. Please
report responsibly.

There are two ways to report a security issue, in order of preference:

### 1. GitHub Security Advisories (preferred)

Open a private advisory at
<https://github.com/juanmitaboada/zark/security/advisories/new>.

This keeps the report private until a fix is published, lets us
coordinate a CVE if warranted, and creates a clear audit trail. You
will need a GitHub account.

### 2. Email

Send an email to **juanmi@juanmitaboada.com** with subject line
beginning `[zark security]`. If the issue is sensitive, encrypt the
message with the maintainer's PGP key
(fingerprint: `347D C6D5 0D77 9E6C 93E2 11C6 2D87 70E8 99CF 5696`,
available from `keyserver.ubuntu.com`).

## What to include

To help triage and reproduce, please provide:

- A description of the vulnerability and its impact
- Steps to reproduce, or a proof-of-concept
- The zark version (`zark --version`)
- The Ubuntu version and ZFS version
- Whether the issue requires user interaction or specific configuration
- Any suggested fix or mitigation, if you have one

## What to expect

- **Acknowledgement** within 72 hours
- **Initial assessment** within 7 days, including whether we accept the
  report and an estimated timeline for a fix
- **Coordinated disclosure**: we will work with you on a public
  disclosure date, typically once a fix has been released. Please do
  not publicly disclose the vulnerability before then
- **Credit** in the release notes of the fixing version, unless you
  prefer to remain anonymous

## Out of scope

The following are **not** considered security vulnerabilities in zark:

- Issues in upstream dependencies (ZFS, sanoid, GRUB, dracut,
  cryptsetup) — please report those to the respective projects
- Configuration mistakes by the operator (e.g. weak LUKS passphrase,
  unencrypted backup destination)
- Issues that require physical access to a machine that is already
  compromised or unlocked
- Denial of service that requires root on the same host where zark
  runs (root can already do anything)

If you are unsure whether something qualifies, report it anyway and
we will discuss.
