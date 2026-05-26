"""Install Playwright Chromium browser binaries."""

import os
import sys


def main():
    print("Installing Playwright Chromium browser...")
    exit_code = os.system("playwright install chromium")
    if exit_code != 0:
        print("Failed to install Playwright Chromium.", file=sys.stderr)
        sys.exit(1)
    print("Playwright Chromium installed successfully.")


if __name__ == "__main__":
    main()
