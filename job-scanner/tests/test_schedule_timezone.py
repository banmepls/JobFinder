"""When the scheduled scans actually run.

The times were always read in the machine's own clock, which is right until the
laptop travels or the user wants scans to follow the market they are applying to.
The rule that outranks all of it: a timezone must never take the app down -
Windows ships no tz database at all without the `tzdata` package, and a zone
saved on one machine can be unknown on the next.
"""

from __future__ import annotations

from scheduler import jobs as sched


def test_no_zone_set_means_the_machines_own_clock():
    assert sched.resolve_timezone("") is None
    assert sched.resolve_timezone(None) is None
    assert sched.resolve_timezone("   ") is None


def test_a_named_zone_is_resolved():
    tz = sched.resolve_timezone("Europe/Tallinn")
    assert tz is not None and "Tallinn" in str(tz)


def test_an_unknown_zone_falls_back_instead_of_raising():
    """A machine without the tz database, or a zone typed by hand, must leave the
    schedule running in the local clock - not stop it being built at all."""
    assert sched.resolve_timezone("Nowhere/Atlantis") is None
    assert sched.resolve_timezone("Europe/Kiev ") is not None   # tolerated: padded


def test_the_offered_list_is_real_zone_names():
    names = sched.available_timezones()
    if not names:            # a machine with no database is a valid answer
        return
    assert "UTC" in names
    for name in names[:50]:
        assert sched.resolve_timezone(name) is not None


class _Recorder:
    """Enough of an APScheduler to see what the trigger was built with."""

    def __init__(self):
        self.jobs = []

    def remove_all_jobs(self):
        self.jobs.clear()

    def get_jobs(self):
        return []

    def add_job(self, func, trigger=None, **kw):
        self.jobs.append(trigger)


def test_the_schedule_is_built_in_the_chosen_zone():
    s = _Recorder()
    sched.configure_scan_jobs(s, {"scan_times": "09:00", "scan_days": "",
                                  "scan_timezone": "America/New_York"})

    assert len(s.jobs) == 1
    assert "New_York" in str(s.jobs[0].timezone)


def test_a_broken_zone_still_schedules_the_scan():
    """The failure this guards: a bad name skipping the schedule entirely, so
    the app looks fine and simply never scans again."""
    s = _Recorder()
    sched.configure_scan_jobs(s, {"scan_times": "09:00, 18:00", "scan_days": "mon",
                                  "scan_timezone": "Nowhere/Atlantis"})

    assert len(s.jobs) == 2
