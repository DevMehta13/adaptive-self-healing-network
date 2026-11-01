"""Laptop-side backend for the Adaptive Self-Healing IoT Network."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("iot-backend")
except PackageNotFoundError:  # running from a source tree that was never installed
    __version__ = "0.0.0"
