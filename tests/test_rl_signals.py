"""Controller invariants using a small deterministic TraCI test double."""
import copy
import json
import random
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sumo import rl_signals as rl


class FakeTraCI:
    def __init__(self, step=.1):
        self.step = step
        self.time = 0.0
        self.steps = 0
        self.closed = False
        self.signals = []
        self.simulation = SimpleNamespace(
            getDeltaT=lambda: self.step, getTime=lambda: self.time,
            getPendingVehicles=lambda: ['pending'], getArrivedNumber=lambda: 0)
        self.trafficlight = SimpleNamespace(
            getControlledLanes=lambda tls: ['lane_0', 'lane_0'],
            setRedYellowGreenState=lambda tls, state: self.signals.append((self.time, state)))
        self.lane = SimpleNamespace(getLastStepHaltingNumber=lambda lane: 2)

    def start(self, command):
        self.time = 0.0

    def simulationStep(self):
        self.steps += 1
        self.time = round(self.steps * self.step, 10)

    def getVersion(self):
        return 22, 'SUMO test'

    def close(self):
        self.closed = True


def phase(clearance=0):
    return {'green': 'Gr', 'yellow': 'yr', 'green_seconds': 15,
            'yellow_seconds': 3, 'all_red_seconds': clearance, 'baseline': [15, 3, max(2, clearance)]}


class TimingTests(unittest.TestCase):
    def test_tenth_second_steps_preserve_seconds_and_cost_integral(self):
        fake = FakeTraCI(.1)
        with patch.object(rl, 'traci', fake):
            result = rl.run_episode('unused.sumocfg', 'x', [phase()], None, {}, random.Random(1), 1, 20)
        self.assertEqual(fake.steps, 200)
        self.assertEqual(fake.signals, [(0.0, 'Gr'), (15.0, 'yr'), (18.0, 'rr')])
        self.assertAlmostEqual(result['elapsed_seconds'], 20)
        self.assertAlmostEqual(result['queue_and_insertion_wait_vehicle_seconds'], 60)
        self.assertTrue(fake.closed)

    def test_unrepresentable_duration_fails_instead_of_overshooting_bounds(self):
        fake = FakeTraCI(.3)
        with patch.object(rl, 'traci', fake), self.assertRaisesRegex(ValueError, 'кратными'):
            rl.run_episode('unused.sumocfg', 'x', [phase()], None, {}, random.Random(1), 1, 20)
        self.assertEqual(fake.steps, 0)
        self.assertTrue(fake.closed)

    def test_runtime_context_mismatch_is_rejected_before_first_step(self):
        fake = FakeTraCI(.1)
        with patch.object(rl, 'traci', fake), self.assertRaisesRegex(ValueError, 'simulation_step_seconds'):
            rl.run_episode('unused.sumocfg', 'x', [phase()], None, {}, random.Random(1), 1, 20,
                           expected_runtime={'simulation_step_seconds': 1, 'state_layout': ['lane_0'],
                                             'sumo_version': [22, 'SUMO test']})
        self.assertEqual(fake.steps, 0)
        self.assertTrue(fake.closed)

    def test_fixed_controller_cannot_shorten_original_clearance(self):
        fake = FakeTraCI(.1)
        original = phase(8)
        original['baseline'] = [15, 3, 2]
        with patch.object(rl, 'traci', fake), self.assertRaisesRegex(ValueError, 'исходный'):
            rl.run_episode('unused.sumocfg', 'x', [original], None, {}, random.Random(1), 1, 20)
        self.assertEqual(fake.steps, 0)


class ProgramAndPolicyTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.config = self.root / 'demo.sumocfg'
        self.config.write_text('<configuration><input><net-file value="demo.net.xml"/>'
                               '<route-files value="demo.rou.xml"/></input></configuration>', encoding='utf-8')
        self.network = self.root / 'demo.net.xml'
        self.network.write_text('<net><tlLogic id="x" type="static">'
                                '<phase duration="30" state="Gr"/><phase duration="3" state="yr"/>'
                                '<phase duration="8" state="rr"/>'
                                '<phase duration="30" state="rG"/><phase duration="3" state="ry"/>'
                                '<phase duration="2" state="rr"/></tlLogic></net>', encoding='utf-8')
        (self.root / 'demo.rou.xml').write_text('<routes/>', encoding='utf-8')

    def test_existing_eight_second_clearance_is_loaded_and_enforced_per_transition(self):
        tls, phases = rl.load_program(self.config)
        self.assertEqual(tls, 'x')
        self.assertEqual([p['all_red_seconds'] for p in phases], [8, 2])
        actions = [(15, 3, 2), (15, 3, 8), (15, 3, 9)]
        self.assertEqual(rl.valid_action_indices(actions, phases[0]), [1, 2])
        self.assertEqual(rl.valid_action_indices(actions, phases[1]), [0, 1, 2])
        with self.assertRaises(ValueError):
            rl.valid_action_indices(actions[:1], phases[0])

    def test_complex_transition_is_rejected_instead_of_discarded(self):
        self.network.write_text(self.network.read_text().replace('state="yr"', 'state="yG"'), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'переходные'):
            rl.load_program(self.config)

    def test_policy_rejects_changed_network_bounds_horizon_and_old_schema(self):
        tls, phases = rl.load_program(self.config)
        bounds = {'green': [15, 45], 'yellow': [3, 5], 'red': [2, 10]}
        context = rl.policy_context(self.config, tls, phases, bounds, 10, 360)
        rl.validate_policy_context(context, copy.deepcopy(context))
        for key, value in [('bounds', {}), ('horizon_seconds', 180), ('controller_version', 'old')]:
            changed = dict(context, **{key: value})
            with self.assertRaisesRegex(ValueError, key):
                rl.validate_policy_context(context, changed)
        self.network.write_text(self.network.read_text() + '\n', encoding='utf-8')
        changed = rl.policy_context(self.config, tls, phases, bounds, 10, 360)
        with self.assertRaisesRegex(ValueError, 'input_fingerprint'):
            rl.validate_policy_context(context, changed)
        with self.assertRaises(ValueError):
            rl.validate_policy_context(None, context)

    def test_untried_zero_is_not_selected_over_trained_negative_value(self):
        self.assertEqual(rl.choose_action([-10, 0], [1, 0], [0, 1], [(15, 3, 2), (25, 3, 2)]), (0, False))
        self.assertEqual(rl.choose_action([0, 0], [0, 0], [0, 1], [(15, 3, 2), (25, 3, 2)]), (0, True))

    def test_policy_requires_visit_counts_and_matching_action_space(self):
        with self.assertRaises(ValueError):
            rl.validate_policy_table({'actions': [[15, 3, 2]], 'q': {'0:0': [0]}}, [(15, 3, 2)])
        q, visits = rl.validate_policy_table({'actions': [[15, 3, 2]], 'q': {'0:0': [-1]},
                                              'visits': {'0:0': [1]}}, [(15, 3, 2)])
        self.assertEqual(visits['0:0'], [1])


if __name__ == '__main__':
    unittest.main()
