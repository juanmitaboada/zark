# zark brand assets

The mark is a disk platter of eight sectors (the drive, the ark) around a shield
carrying a stencil "z". The two orange sectors at the top right are the way back:
the part of the disk that brings the system home.

All artwork is hand-written SVG; text is converted to outlines, so nothing depends
on installed fonts. PNGs are rendered from the SVGs and carry no metadata chunks.

## Palette

| Token        | Hex       | Use                                                       |
|--------------|-----------|-----------------------------------------------------------|
| Navy (ink)   | `#1B2236` | Sectors and z on light grounds, one-ink mark, dark tiles   |
| Orange       | `#E8592A` | Accent sectors, shield, hooks and highlights               |
| White        | `#FFFFFF` | Sectors on dark grounds, one-ink mark on dark              |
| Text light   | `#F2F4F7` | Wordmark on dark grounds                                   |
| Muted        | `#9AA4B2` | Secondary text on dark grounds                             |
| Muted (light)| `#4A5568` | Secondary text on light grounds                            |
| Night        | `#0E1422` | Full-bleed dark backgrounds (social preview)               |
| Terminal bg  | `#0B0F17` | Terminal panels                                            |
| OK green     | `#5FCB7A` | `[OK]` and success frames in terminal mockups              |
| Step cyan    | `#56C8D8` | Step counters in terminal mockups                          |

Orange is the only accent. Keep it to the mark, one headline or one highlight per
view; it is close to Ubuntu orange, so it must never become a background colour or
a large flat area that could read as an Ubuntu property. zark is "for Ubuntu".

## Typography

- Wordmark and headings: Inter Bold, lowercase "zark", tracking -3%.
- Body and taglines: Inter Medium / Regular.
- Code and terminal: DejaVu Sans Mono (Regular and Bold).
- All three are freely licensed (Inter: SIL OFL 1.1; DejaVu: Bitstream Vera
  derived, free). Do not use Ubuntu Sans for zark material.

## Which mark where

| Size / place                         | Asset                               |
|--------------------------------------|-------------------------------------|
| 48 px and up, headers, slides        | `zark-icon` (full: ring + shield)   |
| 32 to 64 px when the ring matters    | `zark-icon-ringz` (ring + z)        |
| 24 px and below, favicon             | `zark-icon-small` (shield + z)      |
| GNOME panel indicator                | `zark-symbolic.svg` (16 px, recoloured by the theme) |
| Avatars, app tiles                   | `zark-icon-tile`, `zark-icon-tile-dark` |
| One colour printing, stamps          | `*-mono` (navy) / `*-mono-white`    |

Every variant has `-dark` (for dark grounds) or one-ink siblings in `svg/`, and
PNG renders in `png/`.

Clear space around the mark: at least one sector thickness (about 12% of the
mark's width) on every side. Do not recolour sectors individually, rotate the
mark, outline it or put it on busy photos.

## README header

```html
<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/images/brand/zark-header-dark.svg">
    <img alt="zark - bare-metal backup and recovery for ZFS on Ubuntu" src="docs/images/brand/zark-header-light.svg" width="880">
  </picture>
</p>
```

The terminal in the header is an illustration of "one command, and it works", not
a transcript of zark's output. The real recording belongs in the quickstart.

## Social preview

`zark-social-preview.png` (1280x640) goes in Settings > General > Social preview.
Its SVG source is next to it.

## Notes for the GNOME extension

- Panel: `zark-symbolic.svg`, installed as `zark-symbolic` in the extension's
  `icons/` and loaded with `Gio.icon_new_for_string`. It is a single path with
  the z knocked out, so the panel theme colours it; state colours are applied by
  the extension's stylesheet on top (ok / overdue / running / attention), not
  baked into the file.
- Menu header and about page: `zark-icon` at 48 or 64 px.
- Accent for progress bars and the running state: orange `#E8592A`; ok state may
  use OK green `#5FCB7A`.

## Notes for the explainer video

- Backgrounds: Night `#0E1422`, optionally with the faint 40 px grid used in the
  social preview (`#1A2335`).
- The mark animates well by its parts: eight sectors appearing in turn (the
  disk), the two orange ones last (the way back), then the shield and the z.
- Text: Inter; on-screen commands in DejaVu Sans Mono with the terminal colours
  above.
