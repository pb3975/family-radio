"""Synthetic B8 civil-time and cached synchronization boundaries."""
import calendar
import os
import time
import unittest
from unittest.mock import patch

from host.clock import ClockSync, system_synchronized
from host.radio import Controller, local_clock_minute


class Player:
    def pause(self, value):
        pass


class ClockTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(time.tzset)

    def test_snapshots_hide_unknown_time_and_include_injected_civil_minute(self):
        c = Controller(Player(), lambda volume: None, wall_time=lambda: 1794737280)
        c.receive('B8\tREADY', 0)
        self.assertEqual(c.snapshot().decode().strip().split('\t')[-1], '0')
        with patch.dict(os.environ, TZ='UTC'):
            time.tzset()
            c.clock_ready = lambda: True
            fields = c.snapshot().decode().strip().split('\t')
            self.assertEqual(len(fields), 29)
            self.assertEqual(fields[-1], '29912288')
            c.wall_time = lambda: 0
            self.assertEqual(c.snapshot().decode().strip().split('\t')[-1], '0')
        time.tzset()

    def test_local_calendar_rollover_and_daylight_saving_follow_host_timezone(self):
        def minute(utc):
            return local_clock_minute(calendar.timegm(time.strptime(utc, '%Y-%m-%d %H:%M')))
        with patch.dict(os.environ, TZ='America/New_York'):
            time.tzset()
            self.assertEqual(minute('2026-11-15 05:00')-minute('2026-11-15 04:59'), 1)
            # The civil clock follows the autumn repeated hour and spring skipped hour.
            self.assertEqual(minute('2026-11-01 06:00')-minute('2026-11-01 05:59'), -59)
            self.assertEqual(minute('2026-03-08 07:00')-minute('2026-03-08 06:59'), 61)
            self.assertEqual(local_clock_minute(float('nan')), 0)
            self.assertEqual(local_clock_minute(10**30), 0)
        time.tzset()

    def test_cached_sync_fails_closed_before_check_on_failure_and_when_stale(self):
        now = [0]
        result = [True]
        clock = ClockSync(probe=lambda: result[0], now=lambda: now[0])
        self.assertFalse(clock.ready())
        clock.refresh()
        self.assertTrue(clock.ready())
        now[0] = 60
        self.assertFalse(clock.ready())
        result[0] = False
        clock.refresh()
        self.assertFalse(clock.ready())
        result[0] = True
        clock.refresh()
        c = Controller(Player(), lambda volume: None, clock_ready=clock.ready)
        c.receive('B8\tREADY', 0)
        clock.probe = lambda: self.fail('Snapshots must not run the OS probe')
        c.snapshot()

    def test_missing_sync_service_is_unknown(self):
        with patch('host.clock.subprocess.run', side_effect=OSError):
            self.assertFalse(system_synchronized())

    def test_old_protocol_cannot_handshake(self):
        c = Controller(Player(), lambda volume: None)
        c.receive('B7\tREADY', 0)
        self.assertFalse(c.session)
