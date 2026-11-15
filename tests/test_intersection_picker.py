import unittest
from unittest.mock import Mock, patch

from osm.intersection_finder import selected_city


class IntersectionPickerTests(unittest.TestCase):
    def test_gui_city_does_not_wait_for_console_input(self):
        with patch('builtins.input',side_effect=AssertionError('Unexpected console input')):
            self.assertEqual(selected_city(['--city',' Москва ']),'Москва')

    def test_noninteractive_launch_uses_default_city(self):
        with patch('osm.intersection_finder.sys.stdin',Mock(isatty=lambda:False)), patch('builtins.input',side_effect=AssertionError('Unexpected console input')):
            self.assertEqual(selected_city([]),'Москва')

    def test_launch_without_console_uses_default_city(self):
        with patch('osm.intersection_finder.sys.stdin',None):
            self.assertEqual(selected_city([]),'Москва')

    def test_terminal_launch_keeps_city_prompt(self):
        with patch('osm.intersection_finder.sys.stdin',Mock(isatty=lambda:True)), patch('builtins.input',return_value=' Казань '):
            self.assertEqual(selected_city([]),'Казань')


if __name__=='__main__':unittest.main()
