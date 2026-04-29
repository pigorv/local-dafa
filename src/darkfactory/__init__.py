from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("darkfactory")
except PackageNotFoundError:
    __version__ = "0.1.0"

__all__ = ["__version__"]
