# Security Policy

## Supported versions

zark is a single-maintainer project. Security fixes are produced for
the **latest released version only**. Older versions are not patched.

| Version | Supported          |
| ------- | ------------------ |
| 1.0.x   | :white_check_mark: |
| < 1.0   | :x:                |

## Reporting a vulnerability

If you have found a security issue in zark — anything that could allow data loss, unauthorised disk access, encryption-key exposure, or
boot-chain compromise on a system using zark — please **do not open a public GitHub issue**.

Instead, email **juanmi@juanmitaboada.com** with:

- A description of the issue and the affected zark version.
- Steps to reproduce, or a minimal proof of concept.
- Your assessment of impact (loss of confidentiality / integrity / availability, requires local access, requires root, etc.).
- Optionally, a suggested fix.

You should receive an acknowledgement within **72 hours**. If you do not, please follow up — the email may have been filtered or missed.

## Handling

For accepted reports:

1. We confirm the issue and assess severity.
2. We prepare a fix in a private branch.
3. We coordinate a release date with you (default: as soon as the fix is validated).
4. The fix ships in the next zark release.
5. The vulnerability is disclosed in the release notes ([`CHANGELOG.md`](CHANGELOG.md), GitHub Releases page) and credits
   the reporter unless they prefer to remain anonymous.

For reports that turn out not to be vulnerabilities (e.g. expected behaviour, configuration issue, upstream ZFS/GRUB bug), we reply with
an explanation and, where useful, recommend filing an issue with the appropriate project.

## Threat model — what zark assumes

zark always runs as **root** and orchestrates `zfs`, `cryptsetup`, `grub-install`, `dracut` and similar tools. It is therefore **not**
designed to defend against an attacker who already has root on the system being backed up — such an attacker has already won.

zark **is** designed to:

- Preserve the confidentiality of ZFS-encrypted data during transfer to the backup drive (raw `zfs send`, no decryption in flight).
- Detect and refuse to recover from a backup with a missing or tampered keystore zvol.
- Avoid producing a recovered system that diverges from a fresh Ubuntu install (custom binaries, unsigned boot chain, etc.).
- Fail loudly rather than silently when integrity assumptions break.

Reports about behaviour outside this model (for example, "I gave zark root and it did something powerful") are not security issues but may
still be valid bug reports — please file them as regular GitHub issues.
