"""ModFig registry synchronization CLI."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("modfig")
except PackageNotFoundError:
    __version__ = "0+unknown"
