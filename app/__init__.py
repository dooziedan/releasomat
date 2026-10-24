"""Releasomat: turns playlists from streaming services into playlists in Navidrome."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("releasomat")
except PackageNotFoundError:  # running from a source checkout without installing
    __version__ = "0.0.0-dev"

REPOSITORY = "https://github.com/dooziedan/releasomat"
