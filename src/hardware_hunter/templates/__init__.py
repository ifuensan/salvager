"""Bundled example files used by ``hardware-hunter init``.

The three files in this package mirror the repo-root operator-facing
examples. They are duplicated rather than symlinked so the wheel-install
path works on any OS; a unit test asserts the two copies are
byte-identical to prevent drift.

``dot.env.example`` is the rendered name to avoid a ``.env``-prefixed
file inside the source tree (some tools eagerly load anything matching
``.env*``).
"""
