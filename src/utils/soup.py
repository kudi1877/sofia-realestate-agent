"""Shared BeautifulSoup construction with the fastest available parser.

lxml parses 3-10x faster than the stdlib html.parser — meaningful when the
nightly walks hundreds of 40-listing portal pages (TIN-518). Falls back to
html.parser transparently if lxml is ever missing from the venv.
"""

from __future__ import annotations

from bs4 import BeautifulSoup

try:
    import lxml  # noqa: F401 — availability probe only

    PARSER = "lxml"
except ImportError:  # pragma: no cover — depends on environment
    PARSER = "html.parser"


def make_soup(markup: str) -> BeautifulSoup:
    return BeautifulSoup(markup, PARSER)
