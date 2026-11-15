"""Prefer the working model; use the bundled Moscow model on first launch."""
from pathlib import Path


def model_file(project_root, kind):
    root=Path(project_root)
    paths={
        'geometry': ('osm/geometry.json','examples/moscow/geometry.json'),
        'config': ('sumo/intersection.sumocfg','examples/moscow/model/scenario.sumocfg'),
    }
    working,example=(root/name for name in paths[kind])
    # Once a local build has started, keep every path in that same model. A
    # missing companion stays missing so callers can show the incomplete build
    # instead of silently loading a bundled file for a different intersection.
    has_working_model = any((root / pair[0]).exists() for pair in paths.values())
    return working if has_working_model or not example.exists() else example
