import os
import sys

# Let tests import the cartridge module despite the hyphenated folder name.
sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "apps", "ricochet-robots")
)
