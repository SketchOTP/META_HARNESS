"""
meta_harness
~~~~~~~~~~~~
A self-improving outer loop for any project.
"""
__version__ = "0.1.0"
__all__ = ["run_cycle", "load_config"]

from .config import load_config
from .cycle import run_cycle
