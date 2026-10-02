import unittest

from impeller_control.profile import CustomProfile, EndBehavior, InterpolationMode
from impeller_control.ramp import RampMode, RampPhase, RampProfile, RampSettings


class LinearProfileTests(unittest.TestCase):
    def setUp(self):
        self.profile = CustomProfile.from_pairs([(0, 0), (10, 300), (30, 300), (40, 100)])
        self.profile.validate()

    def test_interpolates_between_points(self):
        self.assertEqual(self.profile.setpoint_at(0), 0)
        self.assertEqual(self.profile.setpoint_at(5), 150)
        self.assertEqual(self.profile.setpoint_at(10), 300)
        self.assertEqual(self.profile.setpoint_at(20), 300)
        self.assertEqual(self.profile.setpoint_at(35), 200)
        self.assertEqual(self.profile.setpoint_at(40), 100)
        self.assertEqual(self.profile.setpoint_at(99), 100)  # clamped after the end
        self.assertEqual(self.profile.setpoint_at(-5), 0)

    def test_phases_and_end_behaviour(self):
        self.assertEqual(self.profile.duration, 40)
        self.assertEqual(self.profile.total_seconds, 40)
        self.assertTrue(self.profile.stop_at_end)
        self.assertEqual(self.profile.phase_at(39.9), RampPhase.RAMPING)
        self.assertEqual(self.profile.phase_at(40), RampPhase.FINISHED)

        hold = CustomProfile.from_pairs([(0, 0), (10, 300)], end=EndBehavior.HOLD)
        self.assertIsNone(hold.total_seconds)
        self.assertFalse(hold.stop_at_end)
        self.assertEqual(hold.phase_at(10), RampPhase.HOLDING)
        self.assertEqual(hold.setpoint_at(1000), 300)

    def test_unsorted_input_is_sorted(self):
        p = CustomProfile.from_pairs([(10, 300), (0, 0), (5, 100)])
        self.assertEqual([pt.t for pt in p.points], [0, 5, 10])


class StepProfileTests(unittest.TestCase):
    def test_holds_until_next_point(self):
        p = CustomProfile.from_pairs([(0, 100), (10, 200), (20, 50)], mode=InterpolationMode.STEP)
        self.assertEqual(p.setpoint_at(0), 100)
        self.assertEqual(p.setpoint_at(9.99), 100)
        self.assertEqual(p.setpoint_at(10), 200)
        self.assertEqual(p.setpoint_at(19.99), 200)
        self.assertEqual(p.setpoint_at(20), 50)


class SmoothProfileTests(unittest.TestCase):
    def test_passes_through_points_and_never_overshoots(self):
        pairs = [(0, 0), (10, 300), (20, 300), (30, 500), (40, 100)]
        p = CustomProfile.from_pairs(pairs, mode=InterpolationMode.SMOOTH)
        for t, rpm in pairs:
            self.assertAlmostEqual(p.value_at(t), rpm, places=6)
        # Monotone cubic: within each segment the value stays between its end points.
        for i in range(len(pairs) - 1):
            (t0, r0), (t1, r1) = pairs[i], pairs[i + 1]
            lo, hi = min(r0, r1), max(r0, r1)
            for k in range(1, 20):
                t = t0 + (t1 - t0) * k / 20
                v = p.value_at(t)
                self.assertGreaterEqual(v, lo - 1e-6, f"t={t}")
                self.assertLessEqual(v, hi + 1e-6, f"t={t}")
        # The flat segment 10..20 stays flat.
        self.assertAlmostEqual(p.value_at(15), 300, places=6)

    def test_two_points_falls_back_to_linear(self):
        p = CustomProfile.from_pairs([(0, 0), (10, 100)], mode=InterpolationMode.SMOOTH)
        self.assertAlmostEqual(p.value_at(5), 50)

    def test_sample_covers_whole_duration(self):
        p = CustomProfile.from_pairs([(0, 0), (10, 100)], mode=InterpolationMode.SMOOTH)
        samples = p.sample(0.5)
        self.assertEqual(samples[0], (0.0, 0.0))
        self.assertEqual(samples[-1], (10.0, 100.0))


class ValidationAndJsonTests(unittest.TestCase):
    def test_validation_errors(self):
        with self.assertRaises(ValueError):
            CustomProfile.from_pairs([(0, 0)]).validate()
        with self.assertRaises(ValueError):
            CustomProfile.from_pairs([(1, 0), (5, 100)]).validate()  # first point not at 0
        with self.assertRaises(ValueError):
            CustomProfile.from_pairs([(0, 0), (5, 5000)]).validate()
        with self.assertRaises(ValueError):
            CustomProfile.from_pairs([(0, 0), (0.01, 100)]).validate()  # too close
        with self.assertRaises(ValueError):
            CustomProfile.from_pairs([(0, 0), (5, -1)]).validate()

    def test_json_round_trip(self):
        p = CustomProfile.from_pairs([(0, 0), (12.5, 250), (40, 0)], mode=InterpolationMode.SMOOTH, end=EndBehavior.HOLD, max_rpm=700)
        q = CustomProfile.from_json(p.to_json())
        self.assertEqual(q.mode, InterpolationMode.SMOOTH)
        self.assertEqual(q.end, EndBehavior.HOLD)
        self.assertEqual(q.max_rpm, 700)
        self.assertEqual([(pt.t, pt.rpm) for pt in q.points], [(0, 0), (12.5, 250), (40, 0)])

    def test_describe(self):
        text = CustomProfile.from_pairs([(0, 0), (10, 300)]).describe()
        self.assertIn("2 points", text)
        self.assertIn("peak 300", text)
        self.assertIn("then stop", text)


class RampToPointsTests(unittest.TestCase):
    def test_linear_ramp_becomes_three_points(self):
        pts = RampProfile(RampSettings(start_rpm=100, target_rpm=400, rate_rpm_per_s=30.0, hold_s=20.0)).to_pairs()
        self.assertEqual(pts, [(0.0, 100.0), (10.0, 400.0), (30.0, 400.0)])
        profile = CustomProfile.from_pairs(pts)
        profile.validate()
        self.assertEqual(profile.setpoint_at(5), 250)

    def test_stepped_ramp_becomes_steps(self):
        pts = RampProfile(
            RampSettings(start_rpm=0, target_rpm=100, mode=RampMode.STEPPED, step_rpm=50, step_dwell_s=5.0, hold_s=0.0)
        ).to_pairs()
        self.assertEqual(pts, [(0.0, 50.0), (5.0, 100.0)])
        CustomProfile.from_pairs(pts, mode=InterpolationMode.STEP).validate()

    def test_stop_at_end_mirrors_settings(self):
        self.assertTrue(RampProfile(RampSettings(0, 100, hold_s=1.0, stop_after_hold=True)).stop_at_end)
        self.assertFalse(RampProfile(RampSettings(0, 100, hold_s=None, stop_after_hold=True)).stop_at_end)
        self.assertFalse(RampProfile(RampSettings(0, 100, hold_s=1.0, stop_after_hold=False)).stop_at_end)


if __name__ == "__main__":
    unittest.main()
