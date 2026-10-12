"""JARVIS - persistent personal AI operating system and autonomous digital assistant.

The package is organised around the operating model described in CONCEPT.md:

* :mod:`jarvis.core`          - executive runtime, memory, permissions, verification, recovery
* :mod:`jarvis.intelligence`  - interchangeable local/cloud model routing and planning
* :mod:`jarvis.tools`         - computer, code and research execution tools
* :mod:`jarvis.odysseus`      - long-running goal (voyage) management
* :mod:`jarvis.evolver`       - self-evaluation and governed improvement
* :mod:`jarvis.interfaces`    - desktop, voice and CLI interfaces
"""

from jarvis.version import __version__, PRODUCT_NAME, banner

__all__ = ["__version__", "PRODUCT_NAME", "banner"]
