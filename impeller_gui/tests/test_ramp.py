import unittest

from impeller_control.ramp import RampMode, RampPhase, RampProfile, RampSettings, rate_from_duration


class LinearRampTests(unittest.TestCase):
    def test_setpoint_follows_rate_and_clamps_at_target(self):
        profile = RampProfile(RampSettings(start_rpm=0, target_rpm=300, rate_rpm_per_s=10.0, hold_s=20.0))
        self.assertEqual(profile.ramp_seconds, 30.0)
        self.assertEqual(profile.total_seconds, 50.0)
        self.assertEqual(profile.setpoint_at(0.0), 0)
        self.assertEqual(profile.setpoint_at(10.0), 100)
        self.assertEqual(profile.setpoint_at(29.99), 300)
        self.assertEqual(profile.setpoint_at(30.0), 300)
        self.assertEqual(profile.setpoint_at(500.0), 300)

    def test_phases(self):
        profile = RampProfile(RampSettings(start_rpm=0, target_rpm=300, rate_rpm_per_s=10.0, hold_s=20.0))
        self.assertEqual(profile.phase_at(0.0), RampPhase.RAMPING)
        self.assertEqual(profile.phase_at(29.9), RampPhase.RAMPING)
        self.assertEqual(profile.phase_at(30.0), RampPhase.HOLDING)
        self.assertEqual(profile.phase_at(49.9), RampPhase.HOLDING)
        self.assertEqual(profile.phase_at(50.0), RampPhase.FINISHED)

    def test_indefinite_hold_never_finishes(self):
        profile = RampProfile(RampSettings(start_rpm=100, target_rpm=200, rate_rpm_per_s=50.0, hold_s=None))
        self.assertIsNone(profile.total_seconds)
        self.assertEqual(profile.phase_at(1e6), RampPhase.HOLDING)
        self.assertEqual(profile.progress_at(1.0), 0.5)
        self.assertEqual(profile.progress_at(10.0), 1.0)

    def test_ramp_down(self):
        profile = RampProfile(RampSettings(start_rpm=400, target_rpm=100, rate_rpm_per_s=100.0, hold_s=0.0))
        self.assertEqual(profile.setpoint_at(1.0), 300)
        self.assertEqual(profile.setpoint_at(3.0), 100)
        self.assertEqual(profile.phase_at(3.0), RampPhase.FINISHED)

    def test_zero_span_goes_straight_to_hold(self):
        profile = RampProfile(RampSettings(start_rpm=250, target_rpm=250, rate_rpm_per_s=10.0, hold_s=5.0))
        self.assertEqual(profile.ramp_seconds, 0.0)
        self.assertEqual(profile.setpoint_at(0.0), 250)
        self.assertEqual(profile.phase_at(0.0), RampPhase.HOLDING)
        self.assertEqual(profile.phase_at(5.0), RampPhase.FINISHED)

    def test_rate_from_duration(self):
        self.assertAlmostEqual(rate_from_duration(0, 300, 30.0), 10.0)
        self.assertAlmostEqual(rate_from_duration(300, 0, 15.0), 20.0)
        with self.assertRaises(ValueError):
            rate_from_duration(0, 300, 0.0)


class SteppedRampTests(unittest.TestCase):
    def test_steps_and_clamping(self):
        profile = RampProfile(
            RampSettings(start_rpm=0, target_rpm=120, mode=RampMode.STEPPED, step_rpm=50, step_dwell_s=10.0, hold_s=5.0)
        )
        self.assertEqual(profile.step_count, 3)
        self.assertEqual(profile.ramp_seconds, 20.0)
        self.assertEqual(profile.setpoint_at(0.0), 0)  # t <= 0 returns the start value
        self.assertEqual(profile.setpoint_at(0.01), 50)
        self.assertEqual(profile.setpoint_at(9.99), 50)
        self.assertEqual(profile.setpoint_at(10.0), 100)
        self.assertEqual(profile.setpoint_at(20.0), 120)
        self.assertEqual(profile.setpoint_at(60.0), 120)
        self.assertEqual(profile.phase_at(19.9), RampPhase.RAMPING)
        self.assertEqual(profile.phase_at(20.0), RampPhase.HOLDING)
        self.assertEqual(profile.phase_at(25.0), RampPhase.FINISHED)

    def test_single_step(self):
        profile = RampProfile(
            RampSettings(start_rpm=100, target_rpm=130, mode=RampMode.STEPPED, step_rpm=50, step_dwell_s=4.0, hold_s=1.0)
        )
        self.assertEqual(profile.step_count, 1)
        self.assertEqual(profile.ramp_seconds, 0.0)
        self.assertEqual(profile.setpoint_at(0.5), 130)


class ValidationTests(unittest.TestCase):
    def test_rejects_out_of_range_and_bad_rates(self):
        with self.assertRaises(ValueError):
            RampProfile(RampSettings(start_rpm=0, target_rpm=5000))
        with self.assertRaises(ValueError):
            RampProfile(RampSettings(start_rpm=-1, target_rpm=100))
        with self.assertRaises(ValueError):
            RampProfile(RampSettings(start_rpm=0, target_rpm=100, rate_rpm_per_s=0.0))
        with self.assertRaises(ValueError):
            RampProfile(RampSettings(start_rpm=0, target_rpm=100, mode=RampMode.STEPPED, step_rpm=0))
        with self.assertRaises(ValueError):
            RampProfile(RampSettings(start_rpm=0, target_rpm=100, hold_s=-1.0))

    def test_describe_mentions_key_numbers(self):
        text = RampProfile(RampSettings(start_rpm=0, target_rpm=300, rate_rpm_per_s=10.0, hold_s=60.0)).describe()
        self.assertIn("0 -> 300 RPM", text)
        self.assertIn("10 RPM/s", text)
        self.assertIn("hold 60 s", text)
        self.assertIn("then stop", text)


if __name__ == "__main__":
    unittest.main()
