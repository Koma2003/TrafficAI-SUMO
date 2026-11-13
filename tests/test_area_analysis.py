"""Checks for area facts, real cache provenance and coordinate selection."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from osm.area_analysis import build_query, classify, fetch_area, report_html, summarize
from ui.area_dialog import draw_bbox, initial_bbox

BBOX = [54.789, 55.874, 54.794, 55.882]


def element(identifier, tags, **position):
    return {'type': 'node', 'id': identifier, 'lat': 54.791, 'lon': 55.878, 'tags': tags, **position}


class InfrastructureFactsTests(unittest.TestCase):
    def test_industrial_works_are_requested_and_classified(self):
        self.assertIn('nwr[man_made=works]', build_query(BBOX))
        self.assertEqual(classify({'man_made': 'works'}), 'business')
        self.assertEqual(classify({'shop': 'no'}), 'other')

    def test_rules_can_be_extended_without_changing_classifier(self):
        custom = {'social': {'label': 'Social', 'match': {'amenity': ['new_category']}}}
        self.assertEqual(classify({'amenity': 'new_category'}, custom), 'social')

    def test_unknown_buildings_and_named_poi_are_separate(self):
        data = {'elements': [element(1, {'building': 'yes'}),
                             element(2, {'shop': 'supermarket', 'name': 'Магазин <script>x</script>'}),
                             element(3, {'amenity': 'school', 'name': 'Школа № 1'}),
                             element(4, {'building': 'apartments'})]}
        result = summarize(data, BBOX)
        self.assertEqual(result['unknown_buildings'], 1)
        self.assertEqual(result['role_counts']['poi'], 2)
        self.assertEqual(result['counts']['residential'], 1)
        self.assertIn({'tag': 'amenity=school', 'label': 'Школы', 'count': 1}, result['subtypes'])
        output = report_html(result)
        self.assertIn('Школа № 1', output)
        self.assertIn('Супермаркеты', output)
        self.assertNotIn('<script>', output)
        self.assertNotIn('больше всего в категории', output)
        self.assertIn('У 1 зданий назначение не определено', output)
        self.assertIn('<svg', output)

    def test_offline_data_do_not_acquire_an_invented_fetch_date(self):
        result = summarize({'elements': []}, BBOX)
        self.assertIsNone(result['retrieved_at'])
        self.assertIsNotNone(result['generated_at'])
        self.assertIn('не сохранена в исходном файле', report_html(result))

    def test_boundary_centers_outside_are_preserved_and_explained(self):
        result = summarize({'elements': [element(1, {'leisure': 'park'}, lat=54.8)]}, BBOX)
        self.assertEqual(result['representative_points_outside'], 1)
        self.assertEqual(len(result['objects']), 1)
        self.assertIn('геометрия не обрезалась', report_html(result))
        self.assertNotIn('<circle', report_html(result))

    def test_bad_responses_and_duplicate_ids(self):
        obj = element(1, {'shop': 'supermarket'})
        result = summarize({'elements': [obj, obj, None, {'type': 'node', 'id': True}]}, BBOX)
        self.assertEqual(len(result['objects']), 1)
        with self.assertRaises(ValueError):
            summarize({'elements': None}, BBOX)


class CacheTests(unittest.TestCase):
    def test_cache_retains_actual_fetch_time_and_avoids_second_request(self):
        response = Mock()
        response.json.return_value = {'elements': [], 'osm3s': {'timestamp_osm_base': '2026-11-01T10:00:00Z'}}
        with tempfile.TemporaryDirectory() as cache, patch('osm.area_analysis.requests.post', return_value=response) as request:
            first = fetch_area(BBOX, cache)
            fetched_at = first['_trafficai']['fetched_at']
            self.assertFalse(first['_trafficai']['cache_hit'])
            self.assertEqual(request.call_count, 1)
            second = fetch_area(BBOX, cache)
            self.assertEqual(request.call_count, 1)
            self.assertTrue(second['_trafficai']['cache_hit'])
            self.assertEqual(second['_trafficai']['fetched_at'], fetched_at)
            result = summarize(second, BBOX)
            self.assertEqual(result['retrieved_at'], fetched_at)
            self.assertEqual(result['osm_snapshot_at'], '2026-11-01T10:00:00Z')
            self.assertTrue(result['cache_hit'])
            self.assertEqual(len(list(Path(cache).glob('*.json'))), 1)
            self.assertEqual(list(Path(cache).glob('*.tmp')), [])


class AreaSelectionTests(unittest.TestCase):
    def test_initial_area_uses_visible_map_instead_of_saved_intersection(self):
        with tempfile.TemporaryDirectory() as project:
            (Path(project)/'clicked_coords.json').write_text(json.dumps({'lat': 54.79, 'lon': 55.88}))
            map_widget = Mock()
            map_widget.get_position.return_value = (55.75, 37.62)
            bounds = initial_bbox(map_widget, project)
            self.assertAlmostEqual((bounds[0]+bounds[2])/2, 55.75)
            self.assertAlmostEqual((bounds[1]+bounds[3])/2, 37.62)

    def test_bad_saved_json_uses_same_default_as_the_main_map(self):
        with tempfile.TemporaryDirectory() as project:
            (Path(project)/'clicked_coords.json').write_text('invalid')
            bounds = initial_bbox(None, project)
            self.assertAlmostEqual((bounds[0]+bounds[2])/2, 55.751244)
            self.assertAlmostEqual((bounds[1]+bounds[3])/2, 37.618423)

    def test_rectangle_is_shown_and_fitted_in_correct_lat_lon_order(self):
        map_widget, previous = Mock(), Mock()
        path = draw_bbox(map_widget, BBOX, previous)
        previous.delete.assert_called_once()
        map_widget.fit_bounding_box.assert_called_once_with((BBOX[2], BBOX[1]), (BBOX[0], BBOX[3]))
        self.assertEqual(map_widget.set_path.call_args.args[0][0], (BBOX[0], BBOX[1]))
        self.assertIs(path, map_widget.set_path.return_value)


if __name__ == '__main__':
    unittest.main()
