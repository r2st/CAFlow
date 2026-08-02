"""The monitor, run for real against a box the tests get to break.

``deploy/hetzner/caflow-monitor.sh`` is what stands between an outage and
somebody noticing it, so the interesting cases are not "does it detect a
500" — they are the ones where a monitor quietly stops being one:

* a check that passes for the wrong reason, so a dead Celery worker reads as a
  healthy deployment right up until a filing deadline is missed;
* an alert on every tick, which is a monitor that gets muted, which is no
  monitor at all on the night it would have mattered;
* an alert *only* on the first tick, so an outage that lasts all weekend is
  announced once at 02:00 on Saturday and never again.

``curl``, ``systemctl`` and ``df`` are shims on ``PATH``. The script runs
unmodified, in ``sh``, against a state file in a temporary directory.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
MONITOR_SCRIPT = REPO_ROOT / "deploy" / "hetzner" / "caflow-monitor.sh"

# Every outbound request the script makes, in one shim. FAKE_CURL_FAIL lists
# substrings; a URL matching any of them fails the way an unreachable host
# does. Requests are appended to FAKE_CURL_LOG so the alert tests can read back
# what was actually sent, which is the only way to tell "alerted" from
# "decided not to".
FAKE_CURL = r"""#!/bin/sh
set -eu
url=""
for arg in "$@"; do
  case "$arg" in
    http://*|https://*) url=$arg ;;
  esac
done
[ -n "${FAKE_CURL_LOG:-}" ] && printf '%s\n' "$url" >>"$FAKE_CURL_LOG"
for pattern in ${FAKE_CURL_FAIL:-}; do
  case "$url" in
    *"$pattern"*)
      echo "curl: (7) Failed to connect" >&2
      exit 7
      ;;
  esac
done
# Alert deliveries carry a body; record it so a test can read the message.
# Flattened to a single line per delivery, because the tests count deliveries
# and the SendGrid payload is several lines of JSON.
if [ -n "${FAKE_ALERT_BODY:-}" ]; then
  case "$url" in
    *alerts*|*sendgrid*)
      prev=""
      for arg in "$@"; do
        if [ "$prev" = "-d" ]; then
          printf '%s\n' "$(printf '%s' "$arg" | tr '\n' ' ')" >>"$FAKE_ALERT_BODY"
        fi
        prev=$arg
      done
      ;;
  esac
fi
exit 0
"""

# `systemctl is-active [--quiet] <unit>`. FAKE_DEAD_UNITS lists the ones that
# are not running.
FAKE_SYSTEMCTL = r"""#!/bin/sh
set -eu
unit=""
for arg in "$@"; do
  case "$arg" in
    -*) ;;
    is-active) ;;
    *) unit=$arg ;;
  esac
done
for dead in ${FAKE_DEAD_UNITS:-}; do
  if [ "$dead" = "$unit" ]; then
    echo inactive
    exit 3
  fi
done
echo active
"""

FAKE_DF = r"""#!/bin/sh
set -eu
echo "Filesystem 1M-blocks Used Available Capacity Mounted on"
echo "/dev/sda1 38000 17000 ${FAKE_FREE_MB:-20000} 46% /"
"""


@pytest.fixture
def monitor(tmp_path):
    """Runs the real script against stubs. Returns a callable."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (
        ("curl", FAKE_CURL),
        ("systemctl", FAKE_SYSTEMCTL),
        ("df", FAKE_DF),
    ):
        shim = bin_dir / name
        shim.write_text(body)
        shim.chmod(0o755)

    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    fresh = backup_dir / "caflow-db-20260802T021500Z.dump"
    fresh.write_text("last night's dump")

    state_file = tmp_path / "monitor.state"
    curl_log = tmp_path / "curl.log"
    alert_body = tmp_path / "alerts.txt"

    def run(**overrides) -> subprocess.CompletedProcess:
        env = {
            **os.environ,
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "BACKUP_DIR": str(backup_dir),
            "STATE_FILE": str(state_file),
            "HOSTNAME_LABEL": "test-box",
            "FAKE_CURL_LOG": str(curl_log),
            "FAKE_ALERT_BODY": str(alert_body),
            # Both transports on, so "did it alert" is answerable from the log
            # rather than from the absence of one.
            "ALERT_WEBHOOK": "https://alerts.example/hook",
            "ALERT_EMAIL_TO": "ops@example.com",
            "SENDGRID_API_KEY": "SG.not-a-real-key",
            # One failing run is enough, except where a test says otherwise.
            "FAILURES_BEFORE_ALERT": "1",
        }
        env.update({key: str(value) for key, value in overrides.items()})
        return subprocess.run(
            ["sh", str(MONITOR_SCRIPT)],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )

    run.state = state_file  # type: ignore[attr-defined]
    run.backups = backup_dir  # type: ignore[attr-defined]
    run.alerts = alert_body  # type: ignore[attr-defined]
    run.requests = curl_log  # type: ignore[attr-defined]
    return run


def _alerts(monitor) -> list[str]:
    """One entry per alert.

    The fixture configures both transports, so every alert is delivered twice.
    Counting webhook payloads alone keeps "how many times was somebody told"
    a question with an answer of one.
    """
    if not monitor.alerts.exists():
        return []
    return [line for line in monitor.alerts.read_text().splitlines() if line.startswith('{"text"')]


def _alerted(monitor) -> bool:
    return bool(_alerts(monitor))


class TestAHealthyBox:
    def test_it_passes_and_says_nothing_to_anyone(self, monitor):
        result = monitor()

        assert result.returncode == 0, result.stderr
        assert "all checks passed" in result.stdout
        assert not _alerted(monitor)

    def test_it_probes_both_the_api_and_the_public_url(self, monitor):
        """Two probes, because they fail for different reasons.

        The bridge address answers for uvicorn and the database behind it. The
        public URL answers for Caddy, the certificate, the vhost and DNS — and
        an app nobody can reach is, from a client's side, an app that is down.
        """
        monitor()

        requested = monitor.requests.read_text()
        assert "172.18.0.1:3010/health/ready" in requested
        assert "caflow.aiknol.com" in requested


class TestWhatEachCheckCatches:
    def test_a_database_the_api_cannot_reach(self, monitor):
        result = monitor(FAKE_CURL_FAIL="health/ready")

        assert result.returncode != 0
        assert "/health/ready" in result.stderr

    def test_a_front_door_that_is_shut_while_the_app_is_fine(self, monitor):
        """The certificate expired, or a Caddy reload dropped the vhost.

        Nothing the readiness probe can see, and a total outage for every
        client. This is the check that separates "the app is up" from "the app
        is reachable", which are not the same claim.
        """
        result = monitor(FAKE_CURL_FAIL="caflow.aiknol.com")

        assert result.returncode != 0
        assert "front door" in result.stderr

    @pytest.mark.parametrize("unit", ["caflow-api", "caflow-web", "caflow-worker", "caflow-beat"])
    def test_a_unit_that_is_not_running(self, monitor, unit):
        result = monitor(FAKE_DEAD_UNITS=unit)

        assert result.returncode != 0
        assert unit in result.stderr

    def test_a_dead_worker_is_invisible_to_every_other_check(self, monitor):
        """The failure this file exists for, and the one nothing else sees.

        A stopped Celery worker serves every page perfectly: logins work,
        filings render, uploads save. It sends no reminders at all. Without a
        unit check, the first sign is a client who was never chased for the
        documents behind a filing whose date has now passed.
        """
        healthy_in_every_other_way = monitor(FAKE_DEAD_UNITS="caflow-worker")

        assert healthy_in_every_other_way.returncode != 0
        assert "caflow-worker" in healthy_in_every_other_way.stderr
        # ...and nothing about HTTP, because HTTP was fine.
        assert "/health/ready" not in healthy_in_every_other_way.stderr

    def test_a_backup_that_has_silently_stopped(self, monitor):
        """Two days without a dump. Nothing else on the box would mention it."""
        stale = time.time() - 3 * 86400
        for dump in monitor.backups.iterdir():
            os.utime(dump, (stale, stale))

        result = monitor()

        assert result.returncode != 0
        assert "nightly backup has stopped" in result.stderr

    def test_a_backup_directory_that_was_never_created(self, monitor, tmp_path):
        result = monitor(BACKUP_DIR=str(tmp_path / "nowhere"))

        assert result.returncode != 0
        assert "no backup has ever run here" in result.stderr

    def test_a_backup_from_last_night_is_fresh_enough(self, monitor):
        """The window is a day and a half, not a day.

        The timer carries RandomizedDelaySec=600 and Persistent=true, so a run
        legitimately lands up to ten minutes late and a rebooted box catches up
        afterwards. A 24-hour window would alert on both.
        """
        yesterday = time.time() - 25 * 3600
        for dump in monitor.backups.iterdir():
            os.utime(dump, (yesterday, yesterday))

        assert monitor().returncode == 0

    def test_a_disk_about_to_fill(self, monitor):
        """Not one failure — Postgres refusing writes, uploads failing, and the
        next backup writing a zero-length file, all at once and all at 02:15."""
        result = monitor(FAKE_FREE_MB=500)

        assert result.returncode != 0
        assert "500MB free" in result.stderr

    def test_a_disk_with_room_is_not_flagged(self, monitor):
        assert monitor(FAKE_FREE_MB=20000).returncode == 0

    def test_free_space_that_cannot_be_read_is_a_failure_not_a_pass(self, monitor):
        """`df` printing something unexpected must not read as "plenty of room".

        An unparsed number compared with `-ge` would either abort the run or,
        worse, quietly succeed — and a disk check that cannot fail is not one.
        """
        result = monitor(FAKE_FREE_MB="n/a")

        assert result.returncode != 0
        assert "could not read free space" in result.stderr


class TestItAlertsOnChangesRatherThanOnATimer:
    """A monitor that alerts every tick is a monitor that gets muted.

    And a muted monitor is the same as no monitor on the night it would have
    mattered. So the rule is: say it when it starts, say it again if it gets
    worse, say it again if it is still broken hours later, and say it once more
    when it comes back.
    """

    def test_the_first_failing_run_alerts(self, monitor):
        monitor(FAKE_DEAD_UNITS="caflow-worker")

        assert _alerted(monitor)
        assert "unhealthy" in "".join(_alerts(monitor))

    def test_the_same_outage_on_the_next_run_does_not_alert_again(self, monitor):
        monitor(FAKE_DEAD_UNITS="caflow-worker")
        before = len(_alerts(monitor))

        monitor(FAKE_DEAD_UNITS="caflow-worker")

        assert len(_alerts(monitor)) == before

    def test_an_outage_that_grows_a_second_cause_alerts_again(self, monitor):
        """New information, so it is said again.

        "The worker is down" and "the worker is down and so is the database"
        are different pages, and the second one changes what the person woken
        up should do first.
        """
        monitor(FAKE_DEAD_UNITS="caflow-worker")
        before = len(_alerts(monitor))

        monitor(FAKE_DEAD_UNITS="caflow-worker", FAKE_CURL_FAIL="health/ready")

        assert len(_alerts(monitor)) > before

    def test_a_second_unit_going_down_is_a_different_outage(self, monitor):
        """The signature carries the unit name, not just the word "unit".

        Otherwise beat stopping an hour after the worker did folds into the
        outage already reported and is never mentioned — and "reminders are not
        being sent" and "reminders are not being queued at all" want different
        first moves from whoever is looking.
        """
        monitor(FAKE_DEAD_UNITS="caflow-worker")
        before = len(_alerts(monitor))

        monitor(FAKE_DEAD_UNITS="caflow-worker caflow-beat")

        assert len(_alerts(monitor)) > before
        assert "caflow-beat" in _alerts(monitor)[-1]

    def test_a_still_broken_outage_is_repeated_after_the_repeat_window(self, monitor):
        """Otherwise a weekend-long outage is announced once, on Saturday.

        Silence and "resolved" look identical in an inbox, and the whole point
        of the state file is that this one stays distinguishable.
        """
        monitor(FAKE_DEAD_UNITS="caflow-worker")
        before = len(_alerts(monitor))

        monitor(FAKE_DEAD_UNITS="caflow-worker", ALERT_REPEAT_HOURS=0)

        assert len(_alerts(monitor)) > before

    def test_recovery_is_announced_once(self, monitor):
        monitor(FAKE_DEAD_UNITS="caflow-worker")
        monitor()
        recovery = [line for line in _alerts(monitor) if "recovered" in line]

        assert len(recovery) == 1

        monitor()
        assert len([line for line in _alerts(monitor) if "recovered" in line]) == 1

    def test_a_recovery_is_not_announced_for_an_outage_nobody_was_told_about(self, monitor):
        """Below the alert threshold, so nothing was ever said to recover from.

        Announcing the all-clear for a blip that was deliberately swallowed
        would put the noise back that the threshold exists to remove.
        """
        monitor(FAKE_DEAD_UNITS="caflow-worker", FAILURES_BEFORE_ALERT=3)
        monitor()

        assert not _alerted(monitor)


class TestTheThresholdBeforeAnyoneIsTold:
    """Two consecutive failing runs by default, because a deploy restarts four units.

    A monitor that pages on every routine `systemctl restart` teaches whoever
    receives it to ignore it, which costs more than the ten minutes of latency
    the threshold buys back.
    """

    def test_one_failing_run_below_the_threshold_tells_nobody(self, monitor):
        result = monitor(FAKE_DEAD_UNITS="caflow-api", FAILURES_BEFORE_ALERT=2)

        assert result.returncode != 0  # ...but the exit status still says so
        assert not _alerted(monitor)

    def test_the_run_that_crosses_it_does_alert(self, monitor):
        monitor(FAKE_DEAD_UNITS="caflow-api", FAILURES_BEFORE_ALERT=2)
        monitor(FAKE_DEAD_UNITS="caflow-api", FAILURES_BEFORE_ALERT=2)

        assert _alerted(monitor)

    def test_a_blip_that_clears_before_the_threshold_resets_the_count(self, monitor):
        """Two failures a day apart are not two consecutive failures."""
        monitor(FAKE_DEAD_UNITS="caflow-api", FAILURES_BEFORE_ALERT=2)
        monitor()
        monitor(FAKE_DEAD_UNITS="caflow-api", FAILURES_BEFORE_ALERT=2)

        assert not _alerted(monitor)


class TestDelivery:
    def test_the_journal_always_gets_it_even_with_nothing_configured(self, monitor):
        """The one transport that cannot itself be misconfigured.

        It is also what `journalctl -u caflow-monitor` shows when someone
        finally goes looking, which on a box with no paging setup is how this
        gets read at all.
        """
        result = monitor(
            FAKE_DEAD_UNITS="caflow-beat", ALERT_WEBHOOK="", ALERT_EMAIL_TO="", SENDGRID_API_KEY=""
        )

        assert "caflow-beat" in result.stderr
        assert "went to the journal only" in result.stderr

    def test_a_webhook_that_is_down_does_not_take_the_monitor_with_it(self, monitor):
        """The alert still reached the journal, and the verdict is still the verdict.

        Letting a failed notification abort the run would lose the state
        update, so the next tick would re-alert — turning one unreachable
        webhook into a loop.
        """
        result = monitor(FAKE_DEAD_UNITS="caflow-beat", FAKE_CURL_FAIL="alerts.example")

        assert "the webhook itself failed" in result.stderr
        assert monitor.state.exists()

    def test_the_email_says_what_is_broken_not_just_that_something_is(self, monitor):
        monitor(FAKE_DEAD_UNITS="caflow-worker")

        assert "caflow-worker" in "".join(_alerts(monitor))

    def test_the_alert_names_the_box_it_came_from(self, monitor):
        """Eight applications on this hardware; several have their own alerts."""
        monitor(FAKE_DEAD_UNITS="caflow-worker")

        assert "test-box" in "".join(_alerts(monitor))

    def test_the_sendgrid_payload_is_json_the_api_would_accept(self, monitor):
        """Hand-rolled JSON, because the box has no jq and this needs no dependency.

        Hand-rolled is also how a quote or a backslash in a systemd error
        message turns the alert into a 400 from SendGrid — silently, at the
        exact moment the alert mattered. So the payload gets parsed here.
        """
        import json

        monitor(FAKE_DEAD_UNITS="caflow-worker")

        raw = [line for line in monitor.alerts.read_text().splitlines() if "personalizations" in line]
        assert raw, "no SendGrid delivery was attempted"
        payload = json.loads(raw[-1])
        assert payload["personalizations"][0]["to"][0]["email"] == "ops@example.com"
        assert "caflow-worker" in payload["content"][0]["value"]
        assert payload["subject"].startswith("CAFlow is unhealthy")

    def test_a_quote_in_a_failure_message_does_not_break_the_payload(self, monitor):
        r"""systemd error text is not JSON-safe, and the escaping is one sed apart.

        A monitor whose alerts silently 400 at SendGrid is worse than one with
        no alerting configured at all — it looks configured. Backslashes have
        to be escaped before quotes, too, or the escape introduced for a quote
        is itself escaped a step later and the payload breaks a different way.
        """
        import json

        monitor(UNITS=r'caflow-api it-said-"no"-and-\left', FAKE_DEAD_UNITS=r'it-said-"no"-and-\left')

        raw = [
            line for line in monitor.alerts.read_text().splitlines() if "personalizations" in line
        ]
        assert raw, "no SendGrid delivery was attempted"
        payload = json.loads(raw[-1])
        assert r'it-said-"no"-and-\left' in payload["content"][0]["value"]

    def test_no_email_is_attempted_without_a_key_to_send_it_with(self, monitor):
        monitor(FAKE_DEAD_UNITS="caflow-worker", SENDGRID_API_KEY="")

        assert "sendgrid" not in monitor.requests.read_text()


class TestTheStateFile:
    def test_a_missing_state_file_is_a_clean_start_not_a_crash(self, monitor):
        assert not monitor.state.exists()

        assert monitor().returncode == 0

    def test_a_corrupt_state_file_is_a_clean_start_not_a_crash(self, monitor):
        """It lives in /var/lib and survives reboots, so it will be corrupt one day.

        A monitor that dies on its own state file is one that stops monitoring
        permanently, silently, and for a reason nobody would think to look for.
        """
        monitor.state.write_text("not a number\n\x00\nnor this\n")

        result = monitor(FAKE_DEAD_UNITS="caflow-api")

        assert result.returncode != 0
        assert "caflow-api" in result.stderr
        assert _alerted(monitor)
