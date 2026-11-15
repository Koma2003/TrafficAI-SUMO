"""Regression checks for conserved demand, directional speed and legacy KPIs."""
import contextlib
import io
import json
import tempfile
import types
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

from sumo import net_builder, route_builder
from sumo.compare import compare_results


class DemandTests(unittest.TestCase):
    def make_routes(self, rates, destinations, sidewalk=False):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            offset = int(sidewalk)
            lanes = '<lane id="in_main_0_0" index="0" allow="pedestrian"/>' if sidewalk else ''
            lanes += ''.join(f'<lane id="in_main_0_{index+offset}" index="{index+offset}"/>'
                             for index in range(len(rates)))
            connections = ''.join(f'<connection from="in_main_0" to="{target}" fromLane="{lane+offset}"/>'
                                  for lane, targets in destinations.items() for target in targets)
            network = root / 'network.net.xml'
            network.write_text(f'<net><edge id="in_main_0">{lanes}</edge>{connections}</net>', encoding='utf-8')
            geometry = root / 'geometry.json'
            geometry.write_text(json.dumps([{'street_name': 'main', 'angle_deg': 0, 'direction': 'ВЪЕЗД',
                                             'lanes': len(rates), 'lane_flow_rates': rates}]), encoding='utf-8')
            routes = root / 'routes.rou.xml'
            with contextlib.redirect_stdout(io.StringIO()), \
                    patch.object(route_builder, 'generate_detectors_manual', return_value=False), \
                    patch.object(route_builder, 'generate_pedestrian_flows', return_value=0):
                route_builder.generate_routes_from_network(str(network), str(routes),
                                                            str(root / 'scenario.sumocfg'), str(geometry))
            return [dict(flow.attrib) for flow in ET.parse(routes).getroot().findall('flow')]

    @staticmethod
    def totals(flows):
        totals = {}
        for flow in flows:
            lane = int(flow['departLane'])
            totals[lane] = totals.get(lane, 0) + 3600 / float(flow['period'])
        return totals

    def test_one_lane_two_turns_conserves_total_demand(self):
        flows = self.make_routes([450], {0: ['out_a', 'out_b']})
        self.assertEqual(len(flows), 2)
        self.assertAlmostEqual(self.totals(flows)[0], 450)
        self.assertTrue(all(abs(3600 / float(flow['period']) - 225) < 1e-6 for flow in flows))

    def test_duplicate_connections_and_different_lane_targets(self):
        # OSM turn-lane arrays are left-to-right, SUMO lane indices are reversed.
        flows = self.make_routes([600, 300], {0: ['out_a', 'out_b', 'out_a'],
                                             1: ['out_a', 'out_b', 'out_c']})
        self.assertEqual(len(flows), 5)
        self.assertAlmostEqual(self.totals(flows)[0], 300)
        self.assertAlmostEqual(self.totals(flows)[1], 600)

    def test_sidewalk_offset_and_fractional_shares(self):
        flows = self.make_routes([123], {0: ['out_a', 'out_b', 'out_c']}, sidewalk=True)
        self.assertAlmostEqual(self.totals(flows)[1], 123, places=7)

    def test_one_route_and_zero_intensity(self):
        self.assertAlmostEqual(self.totals(self.make_routes([450], {0: ['out_a']}))[0], 450)
        self.assertEqual(self.make_routes([0], {0: ['out_a', 'out_b']}), [])


class DirectionalSpeedTests(unittest.TestCase):
    def build_edges(self, approaches):
        captured = {}
        def convert(command, *args):
            path = command[command.index('--edge-files') + 1]
            captured.update({edge.get('id'): float(edge.get('speed')) * 3.6
                             for edge in ET.parse(path).getroot()})
            return types.SimpleNamespace(returncode=0, stdout='', stderr='')
        with tempfile.TemporaryDirectory() as temp, contextlib.redirect_stdout(io.StringIO()), \
                patch.object(net_builder, 'run_netconvert', side_effect=convert), \
                patch.object(net_builder, 'add_pedestrian_phases_to_net'):
            net_builder.build_network(approaches, str(Path(temp) / 'network.net.xml'))
        return captured

    @staticmethod
    def approach(direction, speed=None):
        result = {'street_name': 'main', 'angle_deg': 0, 'direction': direction,
                  'lanes': 1, 'turn_lanes': [['through']]}
        if speed is not None:
            result['maxspeed'] = speed
        return result

    def test_incoming_and_outgoing_limits_remain_independent(self):
        approaches = [self.approach('ВЪЕЗД', 30), self.approach('ВЫЕЗД', 60)]
        for ordering in [approaches, list(reversed(approaches))]:
            with self.subTest(order=ordering[0]['direction']):
                speeds = self.build_edges(ordering)
                self.assertAlmostEqual(speeds['in_main_0'], 30, delta=.03)
                self.assertAlmostEqual(speeds['out_main_0'], 60, delta=.03)

    def test_missing_speed_uses_own_direction_default(self):
        speeds = self.build_edges([self.approach('ВЪЕЗД'), self.approach('ВЫЕЗД', 20)])
        self.assertAlmostEqual(speeds['in_main_0'], 50, delta=.03)
        self.assertAlmostEqual(speeds['out_main_0'], 20, delta=.03)


class LaneStatisticsTests(unittest.TestCase):
    def stats(self):
        return compare_results.LaneStatistics(['det_a', 'det_b'], {'lane_a': 'det_a', 'lane_b': 'det_b'})

    def test_one_hundred_seconds_is_one_stop_not_one_hundred(self):
        stats = self.stats()
        stats.record_vehicle('car', 'lane_a', 0, 5, 1)
        for waiting in range(1, 101):
            stats.record_vehicle('car', 'lane_a', waiting, 0, 1)
        averages = stats.averages('det_a')
        self.assertEqual(averages['delay_seconds_per_vehicle'], 100)
        self.assertEqual(averages['stops_per_vehicle'], 1)
        stats.record_vehicle('car', 'lane_a', 0, 5, .5)
        stats.record_vehicle('car', 'lane_a', .5, 0, .5)
        self.assertEqual(stats.stop_count['det_a'], 2)
        self.assertEqual(stats.total_delay['det_a'], 100.5)

    def test_subsecond_steps_preserve_seconds_and_stop_count(self):
        for step in [.1, .25, 1]:
            with self.subTest(step=step):
                stats = self.stats()
                stats.record_vehicle('car', 'lane_a', 0, 5, step)
                for index in range(int(10 / step)):
                    stats.record_vehicle('car', 'lane_a', (index + 1) * step, 0, step)
                self.assertAlmostEqual(stats.averages('det_a')['delay_seconds_per_vehicle'], 10)
                self.assertEqual(stats.stop_count['det_a'], 1)

    def test_lane_change_does_not_create_a_new_stop_or_move_old_waiting(self):
        stats = self.stats()
        stats.record_vehicle('car', 'lane_a', 0, 5, 1)
        stats.record_vehicle('car', 'lane_a', 1, 0, 1)
        stats.record_vehicle('car', 'lane_b', 2, 0, 1)
        self.assertEqual(stats.total_delay, {'det_a': 1, 'det_b': 1})
        self.assertEqual(stats.stop_count, {'det_a': 1, 'det_b': 0})

    def test_first_seen_waiting_is_not_backdated_and_no_observations_are_zero(self):
        stats = self.stats()
        self.assertEqual(stats.averages('det_a'), {'delay_seconds_per_vehicle': 0,
                         'stops_per_vehicle': 0, 'mean_queue_vehicles': 0})
        stats.record_vehicle('car', 'lane_a', 100, 0, .5)
        self.assertEqual(stats.total_delay['det_a'], .5)
        self.assertEqual(stats.stop_count['det_a'], 0)

    def test_queue_average_is_time_weighted(self):
        stats = self.stats()
        stats.record_queue('det_a', 3, .1)
        stats.record_queue('det_a', 1, .9)
        self.assertAlmostEqual(stats.averages('det_a')['mean_queue_vehicles'], 1.2)

    def test_report_with_zero_arrivals_uses_observed_cars_and_actual_time(self):
        state = {'time': 0.0, 'step_setting': None, 'steps': 0}
        def start(command):
            config = ET.parse(command[command.index('-c') + 1]).getroot()
            state['step_setting'] = float(config.find('time/step-length').get('value'))
        def simulation_step():
            state['time'] += .5
            state['steps'] += 1
        fake = types.SimpleNamespace(start=start, close=lambda: None, simulationStep=simulation_step,
            simulation=types.SimpleNamespace(getTime=lambda: state['time'], getArrivedNumber=lambda: 0,
                                             getMinExpectedNumber=lambda: 1),
            vehicle=types.SimpleNamespace(getIDList=lambda: ['car'], getLaneID=lambda vehicle: 'lane_a',
                getWaitingTime=lambda vehicle: max(0, state['time'] - .5),
                getSpeed=lambda vehicle: 5 if state['steps'] == 1 else 0),
            lane=types.SimpleNamespace(getLastStepHaltingNumber=lambda lane: int(state['steps'] > 1)),
            inductionloop=types.SimpleNamespace(getLastStepVehicleNumber=lambda detector: 0))
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            network = root / 'network.net.xml'
            network.write_text('<net/>', encoding='utf-8')
            config = root / 'source.sumocfg'
            config.write_text('<configuration><time><step-length value="0.5"/></time></configuration>', encoding='utf-8')
            with patch.object(compare_results, 'traci', fake), patch.object(compare_results, 'CONFIG_FILE', config), \
                    patch.object(compare_results, 'SIMULATION_DURATION', 3), \
                    patch.object(compare_results, 'DETECTOR_IDS', ['det_lane_a']), \
                    patch.object(compare_results, 'LANE_TO_DETECTOR', {'lane_a': 'det_lane_a'}), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertTrue(compare_results.collect_statistics(network, str(root / 'observed')))
            text = (root / 'observed_delays.txt').read_text(encoding='utf-8')
        self.assertEqual(state['step_setting'], .5)
        self.assertEqual(state['steps'], 6)
        self.assertIn('СРЕДНЯЯ ЗАДЕРЖКА НА МАШИНУ: 2.50 сек', text)
        self.assertIn('Среднее количество остановок на машину: 1.00', text)
        self.assertIn('Прибывших автомобилей: 0', text)


if __name__ == '__main__':
    unittest.main()
