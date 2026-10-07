"""Native BUSMUST USB backend; register with python-can via package entry points."""

from .bus import BusMustBus, CanStatus

__all__ = ["BusMustBus", "CanStatus"]
__version__ = "0.1.0"
