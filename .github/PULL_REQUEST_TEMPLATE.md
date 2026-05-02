## What this changes

<!-- One paragraph: what is the user-visible / behavioural change?
     Skip this if the diff is purely mechanical (formatting, typos). -->

## Why

<!-- The reason for the change. The diff already shows what changed —
     this section explains why it had to change. Link issues with
     "Fixes #123" or "Refs #123" if applicable. -->

## Checklist

- [ ] `make tox` is green (tests + lint, all Python versions)
- [ ] mypy reports no errors
- [ ] pylint reports `10.00/10`
- [ ] Ruff `RUF027` is clean
- [ ] `CHANGELOG.md` updated under the appropriate section if this is
      a user-visible change
- [ ] Commit messages follow the project style (imperative subject,
      paragraph explaining the why)
- [ ] Did NOT bump the version in `lib/config.py` (the maintainer does
      that as part of the release commit)

## Notes for the reviewer

<!-- Anything specific you want the reviewer to focus on, areas you're
     unsure about, alternative approaches you considered, etc.
     Delete this section if you have nothing to add. -->
