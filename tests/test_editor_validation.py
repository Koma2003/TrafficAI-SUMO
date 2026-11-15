import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from ui.main import ApproachEditor
from ui.control_panel import ControlPanel


class EditorValidationTests(unittest.TestCase):
    def editor(self, value):
        editor = ApproachEditor.__new__(ApproachEditor)
        key = ('Test street', 0)
        original = {'street_name': key[0], 'angle_deg': 0, 'direction': 'ВЪЕЗД',
                    'lanes': 1, 'turn_lanes': [['through']], 'lane_flow_rates': [450],
                    'traffic_lights': [], 'oneway': False}
        editor.current_group_key = key
        editor.groups = {key: dict(original, has_in=True, has_out=False, lanes_in=1,
                                   lanes_out=0, in_app=original, out_app=None)}
        editor.lane_vars = [[Mock(get=lambda: 0), Mock(get=lambda: 1), Mock(get=lambda: 0)]]
        editor.flow_vars = [Mock(get=lambda: value)]
        editor.oneway_var = Mock(get=lambda: False)
        editor.item_to_key = {}
        return editor

    def test_invalid_flow_keeps_file_and_skips_rebuild(self):
        with tempfile.TemporaryDirectory() as directory:
            geometry = Path(directory) / 'geometry.json'
            original = '[{"unchanged": true}]'
            geometry.write_text(original, encoding='utf-8')
            with patch('ui.main.GEOMETRY_FILE', str(geometry)), \
                 patch('ui.main.build_network') as network, \
                 patch('ui.main.generate_routes_from_network') as routes, \
                 patch('ui.main.messagebox.showerror') as error, \
                 patch('ui.main.messagebox.showinfo') as info:
                self.editor('invalid').save_and_rebuild()
                error.assert_called_once()
                info.assert_not_called()
                network.assert_not_called()
                routes.assert_not_called()
                self.assertEqual(geometry.read_text(encoding='utf-8'), original)

    def test_valid_flow_is_saved_and_rebuilt(self):
        with tempfile.TemporaryDirectory() as directory:
            geometry = Path(directory) / 'geometry.json'
            with patch('ui.main.GEOMETRY_FILE', str(geometry)), \
                 patch('ui.main.build_network') as network, \
                 patch('ui.main.generate_routes_from_network') as routes, \
                 patch('ui.main.messagebox.showerror') as error, \
                 patch('ui.main.messagebox.showinfo'):
                self.editor('600').save_and_rebuild()
                self.assertEqual(json.loads(geometry.read_text(encoding='utf-8'))[0]['lane_flow_rates'], [600])
                network.assert_called_once()
                routes.assert_called_once()
                error.assert_not_called()

    def test_save_without_selected_row_keeps_existing_model(self):
        editor = self.editor('invalid')
        editor.current_group_key = None
        self.assertTrue(editor.apply_changes())

    def test_partial_local_model_does_not_launch_bundled_sumo(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            geometry = root / 'osm/geometry.json'
            geometry.parent.mkdir()
            geometry.write_text('[]', encoding='utf-8')
            example = root / 'examples/moscow/model/scenario.sumocfg'
            example.parent.mkdir(parents=True)
            example.write_text('<configuration/>', encoding='utf-8')
            panel = ControlPanel.__new__(ControlPanel)
            panel._open_sumo_impl = Mock()
            with patch('ui.control_panel.PROJECT_ROOT', root), \
                 patch('ui.control_panel.messagebox.showwarning') as warning:
                panel.on_open_sumo()
                panel._open_sumo_impl.assert_not_called()
                warning.assert_called_once()
                self.assertIn(str(root / 'sumo/intersection.sumocfg'), warning.call_args.args[1])


if __name__ == '__main__':
    unittest.main()
