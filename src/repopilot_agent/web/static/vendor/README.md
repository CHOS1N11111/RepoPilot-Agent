# Frontend vendor assets

These files are served locally; the UI does not request a CDN.

- `diff.min.js`: jsdiff (`diff`) 9.0.0, BSD-3-Clause. Unmodified npm
  `dist/diff.min.js`. Source: https://github.com/kpdecker/jsdiff
  Package SHA-512: `svtcdpS8CgJyqAjEQIXdb3OjhFVVYjzGAPO8WGCmRbrml64SPw/jJD4GoE98aR7r25A0XcgrK3F02yw9R/vhQw==`
- `icons.svg`: selected Lucide (`lucide-static`) 1.41.0 icon nodes, ISC.
  Source: https://github.com/lucide-icons/lucide
  Package SHA-512: `39fX7SH+Rwis0oUmLLOipOoFSiJll9yi2DyEGDaE7Sp0qQAEhEfMQ2scQNdWKeGVENGv1uXc5ZeZqBWsuhQSFg==`
  Symbol `history` uses `rotate-ccw-clock`; `trash-2` uses `trash`.

Licenses are included alongside the assets. To update, verify the npm tarball
integrity, copy the distribution and license, and regenerate the sprite from
the listed icon nodes without changing their geometry.
