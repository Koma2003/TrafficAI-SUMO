"""Prefer the working model; use the bundled Moscow model on first launch."""
from pathlib import Path


def model_file(project_root, kind):
    root=Path(project_root)
    paths={
        'geometry': ('osm/geometry.json','examples/moscow/geometry.json'),
        'config': ('sumo/intersection.sumocfg','examples/moscow/model/scenario.sumocfg'),
    }
    working,example=(root/name for name in paths[kind])
    return working if working.exists() or not example.exists() else example
