import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace

from sumo.signal_plan import (_problem, circular_runs, validate_durations, _evaluate, _stage_scenario)


LIMITS = {'green': (15,60), 'yellow': (3,5), 'red': (10,90), 'all_red': (1,10)}


def network(path, phases, pedestrian=False):
    root=ET.Element('net')
    tl=ET.SubElement(root,'tlLogic',id='center',type='static',programID='0')
    for state,duration in phases: ET.SubElement(tl,'phase',state=state,duration=str(duration))
    for index in range(2): ET.SubElement(root,'connection',tl='center',linkIndex=str(index),attrib={'from':f'in{index}','to':f'out{index}'})
    if pedestrian:
        ET.SubElement(root,'edge',id='cross',function='crossing')
        ET.SubElement(root,'connection',tl='center',linkIndex='2',attrib={'from':'cross','to':'walk'})
    ET.ElementTree(root).write(path,encoding='utf-8')


class SignalPlanTests(unittest.TestCase):
    def test_red_across_cycle_boundary(self):
        self.assertEqual(circular_runs(['rG','yG','Gr','Gr','yr','rG'],0,'red'),[[5,0]])

    def test_full_color_limits_and_cycle(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'net.xml'
            network(path,[('Gr',30),('yr',3),('rr',2),('rG',30),('ry',3),('rr',2)])
            problem=_problem(path,LIMITS,70)
            self.assertTrue(validate_durations(problem,problem['feasible']))
            self.assertFalse(validate_durations(problem,[3,3,2,57,3,2]))
            self.assertEqual({r['color'] for r in problem['runs']},{'green','yellow','red','all_red'})

    def test_split_yellow_and_all_red_are_continuous_intervals(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'net.xml'
            network(path,[('Gr',30),('yr',1),('yr',2),('rr',.5),('rr',.5),('rG',30),('ry',3),('rr',1)])
            problem=_problem(path,dict(LIMITS,all_red=(1,3)),68,step=.5)
            self.assertTrue(validate_durations(problem,problem['baseline']))

    def test_infeasible_red_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'net.xml'
            network(path,[('Gr',30),('yr',3),('rr',2),('rG',30),('ry',3),('rr',2)])
            with self.assertRaisesRegex(ValueError,'несовместимы'):
                _problem(path,dict(LIMITS,red=(1,10)),70)

    def test_original_clearance_cannot_be_shortened(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'net.xml'
            network(path,[('Gr',30),('yr',3),('rr',8),('rG',30),('ry',3),('rr',8)])
            with self.assertRaisesRegex(ValueError,'исходный'):
                _problem(path,dict(LIMITS,all_red=(1,3)),82)

    def test_missing_all_red_rejects_positive_minimum(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'net.xml'
            network(path,[('Gr',30),('yr',3),('rG',30),('ry',3)])
            with self.assertRaisesRegex(ValueError,'нет фазы «всем красный»'):
                _problem(path,dict(LIMITS,all_red=(1,3)),66)

    def test_missing_all_red_allows_explicit_zero_minimum(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'net.xml'
            network(path,[('Gr',30),('yr',3),('rG',30),('ry',3)])
            problem=_problem(path,dict(LIMITS,all_red=(0,3)),66)
            self.assertTrue(validate_durations(problem,problem['feasible']))
            self.assertFalse(any(r['color']=='all_red' for r in problem['runs']))

    def test_missing_yellow_rejects_positive_minimum_for_that_direction(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'net.xml'
            network(path,[('Gr',30),('rr',1),('rG',30),('ry',3),('rr',1)])
            with self.assertRaisesRegex(ValueError,'У направления 0 .*нет жёлтого интервала'):
                _problem(path,LIMITS,65)

    def test_always_green_direction_is_not_a_finite_interval(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'net.xml'
            network(path,[('Gr',30),('GG',30)])
            with self.assertRaisesRegex(ValueError,'красного интервала'):
                _problem(path,LIMITS,60)

    def test_exported_plan_is_multiple_of_simulation_step(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'net.xml'
            network(path,[('Gr',30),('yr',3),('rr',2),('rG',30),('ry',3),('rr',2)])
            problem=_problem(path,LIMITS,70,step=.1)
            self.assertTrue(validate_durations(problem,problem['feasible']))
            self.assertTrue(all(abs(v/.1-round(v/.1))<1e-6 for v in problem['feasible']))
            with self.assertRaisesRegex(ValueError,'кратен'):
                _problem(path,LIMITS,70.05,step=.1)

    def test_split_green_preserves_pedestrian_times(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'net.xml'
            network(path,[('GrG',25),('Grr',5),('yrr',3),('rrr',1),
                          ('rGG',25),('rGr',5),('ryr',3),('rrr',1)],True)
            problem=_problem(path,LIMITS,68)
            self.assertTrue(validate_durations(problem,problem['baseline']))
            for i in [0,1,4,5]: self.assertGreaterEqual(problem['bounds'][i][0],problem['baseline'][i])

    def test_real_time_cost_with_point_one_second_step(self):
        with tempfile.TemporaryDirectory() as temp:
            config=Path(temp)/'config.xml'
            config.write_text('<configuration><time><begin value="0"/><end value="0.3"/></time></configuration>')
            clock=[0.]
            def step(): clock[0]+=.1
            engine=SimpleNamespace(start=lambda cmd:None, close=lambda:None,getVersion=lambda:(22,'test'),simulationStep=step,
                simulation=SimpleNamespace(getTime=lambda:clock[0],getMinExpectedNumber=lambda:1,getPendingVehicles=lambda:['pending'],getArrivedNumber=lambda:0),
                vehicle=SimpleNamespace(getIDList=lambda:['stopped'],getSpeed=lambda veh:0,getIDCount=lambda:1))
            result=_evaluate(config,engine,42,None)
            self.assertAlmostEqual(result['waiting_vehicle_seconds'],.6)
            self.assertAlmostEqual(clock[0],.3)

    def test_scenario_copies_pedestrians_and_isolates_detector_output(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            network(root/'net.xml',[('Gr',30),('yr',3),('rG',30),('ry',3)])
            (root/'cars.xml').write_text('<routes/>')
            (root/'peds.xml').write_text('<routes/>')
            (root/'det.xml').write_text('<additional><inductionLoop id="d" lane="x" pos="1" file="old_output.xml"/></additional>')
            config=root/'config.xml'
            config.write_text('<configuration><input><net-file value="net.xml"/><route-files value="cars.xml,peds.xml"/><additional-files value="det.xml"/></input></configuration>')
            staged=_stage_scenario(config,root/'run')
            entries=ET.parse(staged).getroot()
            for name in entries.find('input/route-files').get('value').split(','): self.assertTrue((staged.parent/name).exists())
            new_det=ET.parse(staged.parent/entries.find('input/additional-files').get('value')).getroot()
            self.assertNotEqual(new_det[0].get('file'),'old_output.xml')
            self.assertEqual(ET.parse(root/'det.xml').getroot()[0].get('file'),'old_output.xml')


if __name__=='__main__': unittest.main()
