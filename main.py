"""Entrypoint: python3 main.py (the sealed trial runs it as
`python3 -P -s /bot/main.py`, so the script directory is restored onto
sys.path explicitly before importing the forge package).
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from forge.server import main

if __name__ == "__main__":
    main()
