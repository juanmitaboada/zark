# Images referenced from README and manpage

This directory holds binary assets the project documentation references.
They are kept out of the source root so the top-level listing stays
focused on code; ``debian/install`` does **not** ship them, since the
deb packaging consumes only the rendered manpage and the README itself.

## Required images

### `apport-popup.png`

Screenshot of Ubuntu's "System program problem detected" Apport dialog
that appears during disk-intensive operations on the live USB.

Used by `docs/TROUBLESHOOTING.md` ("System program problem detected"
popup). The image is a small ~480x140 PNG showing the
question-mark dialog with the `Cancel` and `Report problem...` buttons.

If you regenerate this image, keep the filename the same so the
link to it doesn't need touching.

## Brand assets

### `brand/`

The zark mark, wordmark headers, social preview, GNOME symbolic icon and
favicon, as SVG with PNG renders. `brand/BRAND.md` documents the palette,
typography and which variant to use at each size.
