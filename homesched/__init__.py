"""Calendar- and presence-aware chore scheduling for a household robot."""

from .model import Household, Plan
from .scheduler import schedule

__all__ = ["Household", "Plan", "schedule"]
__version__ = "0.1.0"
