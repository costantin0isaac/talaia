# Talaia brand assets

Mark: corbelled parapet. 32-unit grid, three solid shapes, no strokes.

| file | size | use |
| --- | --- | --- |
| `favicon.svg` | vector | primary favicon, both colour schemes in one file |
| `favicon-16.png` | 16 x 16 | |
| `favicon-32.png` | 32 x 32 | |
| `favicon-48.png` | 48 x 48 | |
| `apple-touch-icon.png` | 180 x 180 | opaque ground, square corners |
| `mark.svg` | vector | mark alone, inherits `currentColor` |
| `mark-dark.png` / `mark-light.png` | 512 x 512 | raster mark |
| `lockup-light.svg` / `.png` | vector / 916 x 384 | light on dark |
| `lockup-dark.svg` / `.png` | vector / 916 x 384 | dark on light |
| `readme-banner.png` | 1280 x 320 | README header |
| `readme-banner@2x.png` | 2560 x 640 | retina |
| `social-preview.png` | 1280 x 640 | OpenGraph card, safe area 1120 x 480 |
| `architecture-horizontal.png` | 2560 x 740 | config flow, README width |
| `architecture-vertical.png` | 1040 x 1032 | config flow, docs column |

## Palette

    ground     #12100e
    raised     #1b1815
    sunken     #0b0908
    border     #3a342c
    text       #e6e0d6
    muted      #b5ada1
    accent     #e8743c   identity only, never a status
    up         #7fa855
    down       #d9503d
    paused     #d79a2b
    unknown    #6f675c
    no data    #3a342c

## Type

Azeret Mono for the wordmark, targets, values and anything technical.
IBM Plex Sans for prose. Identity files ship as outlined vectors, so the
running app keeps its system stacks and still works with no network.

## Lockup rules

- Clear space: 0.5 x mark height on all four sides.
- Gap between mark and wordmark: 0.36 x mark height.
- Parapet top aligns to the cap height of the wordmark.
- Below 104 px wide, drop the wordmark and use the mark alone.
