import itertools
import os

from src.logger import setup_logger

logger = setup_logger(__name__)


class ProxyManager:
    """Loads proxies from a file and rotates through them via round-robin."""

    def __init__(self, filepath: str):
        self.proxies: list[str] = []
        self._cycle = None
        self._load_proxies(filepath)

    def _load_proxies(self, filepath: str) -> None:
        """Load proxies from a text file (one per line)."""
        if not os.path.isfile(filepath):
            logger.warning("Proxy file not found: %s. Falling back to no proxy.", filepath)
            return

        with open(filepath, "r", encoding="utf-8") as f:
            self.proxies = [line.strip() for line in f if line.strip()]

        if not self.proxies:
            logger.warning("Proxy file is empty: %s. Falling back to no proxy.", filepath)
            return

        self._cycle = itertools.cycle(self.proxies)
        logger.info("Loaded %d proxies from %s.", len(self.proxies), filepath)

    def get_next_proxy(self) -> str | None:
        """Return the next proxy in round-robin order, or None if unavailable."""
        if self._cycle is None:
            return None
        proxy = next(self._cycle)
        logger.info("Using proxy: %s", proxy)
        return proxy
