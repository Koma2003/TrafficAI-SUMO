import random
import unittest
from osm.area_analysis import classify, summarize, validate_bbox, report_html
from sumo.phase_bounds import bounded_durations
from sumo.rl_signals import duration_choices


class BoundsTests(unittest.TestCase):
    def test_projection_keeps_budget_and_bounds(self):
        rng=random.Random(42)
        for _ in range(200):
            n=rng.randint(1,12)
            total=rng.uniform(n*15,n*60)
            result=bounded_durations([rng.uniform(-100,200) for _ in range(n)],total,15,60)
            self.assertAlmostEqual(sum(result),total,places=7)
            self.assertTrue(all(15<=v<=60 for v in result))

    def test_impossible_cycle_rejected(self):
        with self.assertRaises(ValueError): bounded_durations([3,3],6,15,60)

    def test_rl_choices_respect_limits(self):
        self.assertEqual(duration_choices(15,40,10),[15,25,35,40])
        with self.assertRaises(ValueError): duration_choices(5,3,1)


class AreaTests(unittest.TestCase):
    def test_categories(self):
        for tags,expected in [({'shop':'supermarket'},'commercial'),({'amenity':'school'},'social'),
                              ({'building':'apartments'},'residential'),({'office':'company'},'business'),
                              ({'highway':'bus_stop'},'transport'),({'leisure':'park'},'leisure')]:
            self.assertEqual(classify(tags),expected)

    def test_area_limit(self):
        for box in [[55,37,54,38],[0,0,10,10],[float('nan'),1,2,3]]:
            with self.assertRaises(ValueError): validate_bbox(box)

    def test_report_counts_deduplicates_and_escapes(self):
        obj={'type':'node','id':1,'lat':54.79,'lon':55.88,'tags':{'shop':'supermarket','name':'<script>alert(1)</script>'}}
        result=summarize({'elements':[obj,obj]},[54.78,55.87,54.80,55.89])
        self.assertEqual(result['counts']['commercial'],1)
        self.assertNotIn('<script>',report_html(result))


if __name__=='__main__': unittest.main()
