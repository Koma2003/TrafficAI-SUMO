import tempfile
import unittest
from pathlib import Path
from ui.project_paths import model_file


class ProjectPathsTests(unittest.TestCase):
    def test_first_launch_uses_bundled_model(self):
        with tempfile.TemporaryDirectory() as name:
            root=Path(name)
            example=root/'examples/moscow/model/scenario.sumocfg'
            example.parent.mkdir(parents=True);example.write_text('<configuration/>')
            self.assertEqual(model_file(root,'config'),example)

    def test_existing_user_model_takes_precedence(self):
        with tempfile.TemporaryDirectory() as name:
            root=Path(name)
            working=root/'osm/geometry.json';working.parent.mkdir();working.write_text('[]')
            example=root/'examples/moscow/geometry.json';example.parent.mkdir(parents=True);example.write_text('[]')
            self.assertEqual(model_file(root,'geometry'),working)

    def test_missing_model_remains_a_missing_path(self):
        with tempfile.TemporaryDirectory() as name:
            self.assertEqual(model_file(name,'config'),Path(name)/'sumo/intersection.sumocfg')


if __name__=='__main__':unittest.main()
