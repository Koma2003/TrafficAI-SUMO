"""Compatibility CLI for constrained optimisation of the current SUMO model.

All green/yellow/red intervals are now checked by sumo.signal_plan.
The original network is preserved; outputs are written under runs/.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sumo.signal_plan import main

if __name__ == '__main__':
    main()
