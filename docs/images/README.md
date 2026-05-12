# Images referenced from README and manpage

This directory holds binary assets the project documentation references.
They are kept out of the source root so the top-level listing stays
focused on code; ``debian/install`` does **not** ship them, since the
deb packaging consumes only the rendered manpage and the README itself.

## Required images

### `apport-popup.png`

Screenshot of Ubuntu's "System program problem detected" Apport dialog
that appears during disk-intensive operations on the live USB.

Used by the README troubleshooting section ("System program problem
detected" popup). The image is a small ~480x140 PNG showing the
question-mark dialog with the `Cancel` and `Report problem...` buttons.

If you regenerate this image, keep the filename the same so the
README link doesn't need touching.
