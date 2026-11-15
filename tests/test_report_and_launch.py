import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests
from osm.area_analysis import fetch_area, summarize
from ui.signal_dialog import training_command
from ui.report_viewer import ReportText, show_report


class LaunchTests(unittest.TestCase):
    def test_training_child_ignores_system_pythonpath_and_uses_utf8(self):
        command=training_command(Path(__file__).resolve().parents[1],{'episodes':2},Path('runs/test'),'libsumo')
        self.assertIn('-E',command)
        self.assertEqual(command[command.index('-X')+1],'utf8')
        self.assertEqual(command[command.index('--episodes')+1],'2')
        self.assertEqual(command[command.index('--backend')+1],'libsumo')

    def test_preview_shows_report_not_css_or_script(self):
        parser=ReportText();parser.feed('<style>hidden css</style><h1>Район</h1><p>Магазины: 4</p><svg><text>graphic</text></svg><table><tr><td>Школы</td><td>2</td></tr></table>')
        text=parser.text()
        self.assertIn('Магазины: 4',text)
        self.assertIn('Школы',text)
        self.assertNotIn('hidden css',text)
        self.assertNotIn('graphic',text)

    def test_preview_skips_page_metadata_and_keeps_body(self):
        metadata='<title>Service title</title><style>hidden</style>'
        for header in (metadata,f'<head>{metadata}</head>'):
            with self.subTest(header=header):
                parser=ReportText()
                parser.feed(f'<html>{header}'
                            '<body><h1>Отчёт по району</h1><p>Магазины: 4</p></body></html>')
                parser.close()
                self.assertNotIn('Service title',parser.text())
                self.assertEqual(parser.text(),'Отчёт по району\n\nМагазины: 4')

    def test_unreadable_report_shows_error_without_opening_window(self):
        with tempfile.TemporaryDirectory() as name:
            missing=Path(name)/'missing.html'
            corrupt=Path(name)/'corrupt.html'
            corrupt.write_bytes(b'\xff\xfe')
            for path in (missing,corrupt):
                with self.subTest(path=path.name), patch('ui.report_viewer.messagebox.showerror') as error, patch('ui.report_viewer.tk.Toplevel') as window:
                    parent=Mock()
                    self.assertIsNone(show_report(parent,path,'Отчёт по району'))
                    window.assert_not_called()
                    error.assert_called_once()
                    self.assertIn('Не удалось прочитать отчёт',error.call_args.args[1])
                    self.assertIs(error.call_args.kwargs['parent'],parent)


class FetchFallbackTests(unittest.TestCase):
    def test_osm_api_fallback_is_cached_and_attributed(self):
        response=Mock()
        response.content=b'<osm><node id="1" lat="55.76" lon="37.65"><tag k="shop" v="supermarket"/></node></osm>'
        with tempfile.TemporaryDirectory() as cache, patch('osm.area_analysis.requests.post',side_effect=requests.Timeout('busy')),patch('osm.area_analysis.requests.get',return_value=response) as fallback:
            bbox=[55.75,37.64,55.77,37.66]
            data=fetch_area(bbox,cache)
            summary=summarize(data,bbox)
            self.assertEqual(summary['counts']['commercial'],1)
            self.assertIn('OSM Map API',summary['source'])
            self.assertEqual(fallback.call_count,1)
            again=fetch_area(bbox,cache)
            self.assertTrue(again['_trafficai']['cache_hit'])
            self.assertEqual(fallback.call_count,1)


if __name__=='__main__':unittest.main()
