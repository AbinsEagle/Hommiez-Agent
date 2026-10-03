#!/home/rona/jarvis/.venv/bin/python
"""Hommie voice assistant. All features are plugins in ./plugins (see hommie/core.py).

  jarvis.py            hands-free: say "Hey Hommie ..."
  jarvis.py --type     keyboard mode
  jarvis.py --plugins  list which plugins loaded
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hommie.core import main

if __name__ == "__main__":
    main()
