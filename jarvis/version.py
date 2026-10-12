"""Version and identity of the JARVIS system."""

__version__ = "0.1.0"
PRODUCT_NAME = "JARVIS"
PRODUCT_TAGLINE = "Persistent personal AI operating system and autonomous digital assistant"


def banner() -> str:
    return f"{PRODUCT_NAME} v{__version__} - {PRODUCT_TAGLINE}"
