try:
    from pytest_web._version import version as __version__
except ImportError:
    # Package isn't installed from a tagged build (e.g. running from source)
    __version__ = "0.0.0.dev0"
