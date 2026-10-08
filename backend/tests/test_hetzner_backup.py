"""The shared-box backup, run for real against a stubbed postgres and a stubbed far end.

``deploy/hetzner/caflow-backup.sh`` is the one the timer on 89.167.8.178
actually runs; ``test_backup.py`` covers the Compose variant, which shells into
``docker compose exec`` and is a different file for a different deployment.

Same approach as that one, and for the same reason: what is worth testing here
is not what the script says but what it does when a step goes wrong, and every
interesting failure is a command that exits zero having written the wrong
bytes. So ``sudo``, ``pg_restore``, ``rsync`` and ``ssh`` are replaced by shims
on ``PATH`` that the tests can make succeed, fail, or succeed dishonestly. The
script runs unmodified, in ``sh``, into a temporary directory.

The offsite half is where this file earns its keep. A copy that never left the
box, and a copy the far end accepted and did not keep, both look exactly like
success from an exit status — and both are discovered on the one day the local
disk is gone. So the script asks the far end what it is holding, and these
tests are what make sure it is still asking.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKUP_SCRIPT = REPO_ROOT / "deploy" / "hetzner" / "caflow-backup.sh"

# What a healthy pg_restore --list prints for a CAFlow database: a comment
# header, then one entry per table. The script greps this for the clients
# table, which every CAFlow database has.
HEALTHY_TOC = "\n".join(
    [
        ";     dbname: caflow",
        "211; 0 16440 TABLE DATA public firms caflow",
        "215; 0 16456 TABLE DATA public clients caflow",
        "219; 0 16470 TABLE DATA public compliance_items caflow",
    ]
)

# pg_dump --format=custom writes this magic first; the shim's pg_restore
# refuses anything else, exactly as the real one does. Text rather than the
# real header's bytes because it travels to the shim through the environment,
# which cannot carry a NUL.
HEALTHY_DUMP = "PGDMP-pretend-this-is-a-custom-format-archive"

# `sudo -u postgres pg_dump …`. Only the dump goes through sudo — pg_restore
# --list reads a local file and needs no database at all.
FAKE_SUDO = r"""#!/bin/sh
set -eu
if [ "${FAKE_PG_DUMP_STATUS:-0}" != 0 ]; then
  echo "pg_dump: error: connection to server on socket failed" >&2
  exit "$FAKE_PG_DUMP_STATUS"
fi
printf '%s' "$FAKE_DB_DUMP"
"""

FAKE_PG_RESTORE = r"""#!/bin/sh
set -eu
data=$(cat)
case "$data" in
  PGDMP*) ;;
  *)
    echo "pg_restore: error: did not find magic string in file header" >&2
    exit 1
    ;;
esac
printf '%s\n' "$FAKE_TOC"
"""

# Stands in for the far end. The destination path is real — a directory under
# the test's tmp_path — so "did the bytes land" is a question with a true
# answer rather than one the shim makes up. `rsync` copies into it; `ssh` runs
# sha256sum and find against it.
FAKE_RSYNC = r"""#!/bin/sh
set -eu
if [ "${FAKE_RSYNC_STATUS:-0}" != 0 ]; then
  echo "rsync: connection unexpectedly closed" >&2
  exit "$FAKE_RSYNC_STATUS"
fi
# Last argument is user@host:/path; everything before it that is not a flag or
# the -e argument is a source file.
for last in "$@"; do :; done
dest=${last#*:}
sources=""
skip=0
for arg in "$@"; do
  if [ "$skip" = 1 ]; then skip=0; continue; fi
  case "$arg" in
    -e) skip=1 ;;
    -*) ;;
    "$last") ;;
    *) sources="$sources $arg" ;;
  esac
done
mkdir -p "$dest"
for src in $sources; do
  if [ "${FAKE_RSYNC_TRUNCATES:-0}" = 1 ]; then
    # Accepted the transfer, kept something else. A destination that filled up
    # mid-write looks exactly like this.
    head -c 8 "$src" >"$dest/$(basename "$src")"
  else
    cp "$src" "$dest/$(basename "$src")"
  fi
done
"""

# `ssh -i key -o … user@host <command>`: run the command locally against the
# fake far end, since that is where the fake destination directory lives.
FAKE_SSH = r"""#!/bin/sh
set -eu
if [ "${FAKE_SSH_STATUS:-0}" != 0 ]; then
  echo "ssh: connect to host port 22: Connection timed out" >&2
  exit "$FAKE_SSH_STATUS"
fi
# Drop the flags and the user@host, keep the remote command.
while [ $# -gt 0 ]; do
  case "$1" in
    -i|-o) shift 2 ;;
    -*) shift ;;
    *) break ;;
  esac
done
shift  # user@host
exec sh -c "$*"
"""


@pytest.fixture
def backup(tmp_path):
    """Runs the real script against stubs. Returns a callable."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (
        ("sudo", FAKE_SUDO),
        ("pg_restore", FAKE_PG_RESTORE),
        ("rsync", FAKE_RSYNC),
        ("ssh", FAKE_SSH),
    ):
        shim = bin_dir / name
        shim.write_text(body)
        shim.chmod(0o755)

    # Something for the real `tar` to actually archive, laid out the way the
    # box lays it out: STORAGE_DIR is a `documents` directory inside var/.
    app_dir = tmp_path / "opt" / "CAFlow"
    storage = app_dir / "backend" / "var" / "documents"
    storage.mkdir(parents=True)
    (storage / "bank-statement.pdf").write_bytes(b"%PDF-1.4 pretend")

    backup_dir = tmp_path / "backups"
    offsite_dir = tmp_path / "offsite"

    def run(**overrides) -> subprocess.CompletedProcess:
        env = {
            **os.environ,
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "BACKUP_DIR": str(backup_dir),
            "APP_DIR": str(app_dir),
            "OFFSITE_DEST": f"backup@offsite.example:{offsite_dir}",
            "OFFSITE_SSH_KEY": str(tmp_path / "key"),
            "FAKE_DB_DUMP": HEALTHY_DUMP,
            "FAKE_TOC": HEALTHY_TOC,
        }
        # conftest exports STORAGE_DIR for the app under test, and the script
        # reads the same name. Dropped rather than overwritten so the default
        # path — derive it from APP_DIR, which is what caflow-backup.service
        # actually sets — is the one under test here.
        env.pop("STORAGE_DIR", None)
        env.update({key: str(value) for key, value in overrides.items()})
        return subprocess.run(
            ["sh", str(BACKUP_SCRIPT)],
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )

    run.dir = backup_dir  # type: ignore[attr-defined]
    run.offsite = offsite_dir  # type: ignore[attr-defined]
    run.storage = storage  # type: ignore[attr-defined]
    return run


def _names(directory: Path) -> list[str]:
    return sorted(path.name for path in directory.iterdir()) if directory.exists() else []


def _age(path: Path, days: float) -> None:
    stamp = time.time() - days * 86400
    os.utime(path, (stamp, stamp))


class TestAGoodRun:
    def test_it_writes_both_archives_and_says_where(self, backup):
        result = backup()

        assert result.returncode == 0, result.stderr
        names = _names(backup.dir)
        assert any(name.startswith("caflow-db-") and name.endswith(".dump") for name in names)
        assert any(
            name.startswith("caflow-documents-") and name.endswith(".tar.gz") for name in names
        )
        assert "caflow: backed up to" in result.stdout

    def test_it_leaves_no_part_files_behind(self, backup):
        backup()

        assert not [name for name in _names(backup.dir) if name.endswith(".part")]

    def test_the_documents_archive_really_is_one(self, backup):
        backup()

        archive = next(path for path in backup.dir.iterdir() if path.name.endswith(".tar.gz"))
        listing = subprocess.run(
            ["tar", "-tzf", str(archive)], capture_output=True, text=True, check=True
        )
        assert "documents/bank-statement.pdf" in listing.stdout

    def test_the_archive_unpacks_as_documents_not_as_an_absolute_path(self, backup):
        """`tar -C` the parent, so a restore does not depend on the box's layout.

        An archive built from an absolute path unpacks into whatever
        ``/opt/CAFlow/backend/var`` happens to be on the machine doing the
        restore — which, on the day it matters, is a different machine.
        """
        backup()

        archive = next(path for path in backup.dir.iterdir() if path.name.endswith(".tar.gz"))
        listing = subprocess.run(
            ["tar", "-tzf", str(archive)], capture_output=True, text=True, check=True
        )
        for entry in listing.stdout.split():
            assert entry.startswith("documents/") or entry == "documents"

    def test_an_explicit_storage_dir_wins_over_the_one_derived_from_app_dir(self, backup, tmp_path):
        """STORAGE_DIR is a setting in .env, so it can point anywhere.

        The default assumes the layout the deploy produces. The day a document
        volume gets moved off the root filesystem — which is the day it gets
        big enough to matter — the backup has to follow it, and finding out
        that it did not is a restore that comes back empty.
        """
        elsewhere = tmp_path / "mnt" / "bulk" / "documents"
        elsewhere.mkdir(parents=True)
        (elsewhere / "ledger.pdf").write_bytes(b"%PDF-1.4 moved")

        assert backup(STORAGE_DIR=str(elsewhere)).returncode == 0

        archive = next(path for path in backup.dir.iterdir() if path.name.endswith(".tar.gz"))
        listing = subprocess.run(
            ["tar", "-tzf", str(archive)], capture_output=True, text=True, check=True
        )
        assert "documents/ledger.pdf" in listing.stdout
        assert "bank-statement.pdf" not in listing.stdout


class TestABackupIsReadBackBeforeItIsNamed:
    """A file of plausible size is not a backup, and the difference is unread.

    Every case here exits zero somewhere along the way — a redirect that caught
    an error message, a disk that filled between the first byte and the last, a
    pg_dump pointed at a database that is not this one. Without reading the
    file back, all three land in the directory under a good name and are
    discovered on the day they are needed.
    """

    def test_a_dump_that_is_not_an_archive_is_not_kept(self, backup):
        result = backup(FAKE_DB_DUMP="ERROR:  could not connect to server")

        assert result.returncode != 0
        assert "not a restorable DoAide Reach archive" in result.stderr
        assert _names(backup.dir) == []

    def test_an_empty_dump_is_not_kept(self, backup):
        result = backup(FAKE_DB_DUMP="")

        assert result.returncode != 0
        assert _names(backup.dir) == []

    def test_an_archive_of_the_wrong_database_is_not_kept(self, backup):
        """Valid, restorable, and missing the one table this app cannot lose."""
        result = backup(FAKE_TOC=";     dbname: postgres\n205; 0 16400 TABLE DATA public grafana x")

        assert result.returncode != 0
        assert _names(backup.dir) == []

    def test_a_failing_pg_dump_still_leaves_nothing(self, backup):
        result = backup(FAKE_PG_DUMP_STATUS=1)

        assert result.returncode != 0
        assert _names(backup.dir) == []

    def test_nothing_is_copied_offsite_when_the_dump_failed(self, backup):
        """The offsite half is downstream of the verification, not beside it.

        Shipping an unverified dump to the one place it will be trusted from is
        worse than shipping nothing: the far end then holds a file with a good
        name, a plausible size, and an error message inside it.
        """
        assert backup(FAKE_DB_DUMP="ERROR:  permission denied").returncode != 0

        assert _names(backup.offsite) == []


class TestPruningOnlyFollowsASuccess:
    """The prune is the one destructive line in the local half.

    It runs after both archives have been written and read back, and ``set -e``
    means a failure anywhere above it exits first. That ordering is what stops
    a fortnight of quiet failures from ending with the last good backup deleted
    on day fifteen — precisely when it is about to be needed.
    """

    def test_an_old_backup_is_pruned_after_a_good_run(self, backup):
        backup.dir.mkdir(parents=True)
        stale = backup.dir / "caflow-db-20250101T000000Z.dump"
        stale.write_text("old but valid")
        _age(stale, days=30)

        assert backup().returncode == 0
        assert not stale.exists()

    def test_a_failed_run_prunes_nothing(self, backup):
        backup.dir.mkdir(parents=True)
        stale = backup.dir / "caflow-db-20250101T000000Z.dump"
        stale.write_text("the last good backup")
        _age(stale, days=30)

        assert backup(FAKE_DB_DUMP="ERROR:  relation does not exist").returncode != 0
        assert stale.exists()

    def test_a_recent_backup_survives_a_good_run(self, backup):
        backup.dir.mkdir(parents=True)
        yesterday = backup.dir / "caflow-db-20260801T000000Z.dump"
        yesterday.write_text("yesterday")
        _age(yesterday, days=1)

        assert backup().returncode == 0
        assert yesterday.exists()

    def test_nothing_that_is_not_ours_is_touched(self, backup):
        """The directory may be shared; the prune matches on our own prefix."""
        backup.dir.mkdir(parents=True)
        someone_else = backup.dir / "postgres-nightly-20250101.dump"
        someone_else.write_text("not ours")
        _age(someone_else, days=400)

        assert backup().returncode == 0
        assert someone_else.exists()


class TestTheCopyThatLeavesTheBox:
    """Fourteen days of dumps on the disk they came from are one bet, taken fourteen times.

    They cover a dropped table and nothing else. The failure this half exists
    for is the one that takes the whole machine — a disk, a Hetzner incident, a
    `rm -rf` with an empty variable — and on that day the only copy that
    matters is the one somewhere else.
    """

    def test_both_archives_land_on_the_far_end(self, backup):
        result = backup()

        assert result.returncode == 0, result.stderr
        names = _names(backup.offsite)
        assert any(name.endswith(".dump") for name in names)
        assert any(name.endswith(".tar.gz") for name in names)
        assert "verified offsite" in result.stdout

    def test_the_bytes_that_arrived_are_the_bytes_that_were_sent(self, backup):
        backup()

        for local in backup.dir.iterdir():
            assert (backup.offsite / local.name).read_bytes() == local.read_bytes()

    def test_a_far_end_that_kept_something_else_fails_the_run(self, backup):
        """rsync says it transferred; the destination says it holds eight bytes.

        A destination that is full, read-only, or a stale mount of somewhere
        else accepts a transfer and has nothing afterwards. The exit status
        cannot tell the difference, so the script asks the far end what it is
        holding — and this is the test that keeps it asking.
        """
        result = backup(FAKE_RSYNC_TRUNCATES=1)

        assert result.returncode != 0
        assert "does not match" in result.stderr

    def test_a_transfer_that_fails_outright_fails_the_run(self, backup):
        result = backup(FAKE_RSYNC_STATUS=12)

        assert result.returncode != 0
        assert "offsite copy failed" in result.stderr

    def test_an_unreachable_far_end_fails_the_run(self, backup):
        result = backup(FAKE_SSH_STATUS=255)

        assert result.returncode != 0

    def test_a_failed_offsite_copy_still_leaves_the_local_backup(self, backup):
        """Half a backup beats none, and the local half was already verified.

        The run exits non-zero so the timer records a failure and the monitor
        says so — but deleting a good local archive because the network was
        down would be the script destroying the thing it exists to protect.
        """
        assert backup(FAKE_RSYNC_STATUS=12).returncode != 0

        assert [name for name in _names(backup.dir) if name.endswith(".dump")]

    def test_an_old_offsite_copy_is_pruned_after_a_verified_one_lands(self, backup):
        backup.offsite.mkdir(parents=True)
        stale = backup.offsite / "caflow-db-20250101T000000Z.dump"
        stale.write_text("older than the offsite window")
        _age(stale, days=60)

        assert backup().returncode == 0
        assert not stale.exists()

    def test_the_offsite_window_outlasts_the_local_one(self, backup):
        """Kept longer there than here, deliberately.

        The offsite copy is the one that survives the event that takes this
        box, so pruning it on the local schedule would throw away the more
        valuable copy first.
        """
        backup.offsite.mkdir(parents=True)
        older_than_local = backup.offsite / "caflow-db-20250601T000000Z.dump"
        older_than_local.write_text("past the local window, inside the offsite one")
        _age(older_than_local, days=20)

        assert backup().returncode == 0
        assert older_than_local.exists()

    def test_a_failed_offsite_copy_prunes_nothing_on_the_far_end(self, backup):
        backup.offsite.mkdir(parents=True)
        stale = backup.offsite / "caflow-db-20250101T000000Z.dump"
        stale.write_text("the last good offsite backup")
        _age(stale, days=60)

        assert backup(FAKE_RSYNC_TRUNCATES=1).returncode != 0
        assert stale.exists()


class TestNoOffsiteDestinationConfigured:
    """The window between installing this file and having a key on the far end.

    Real, and short — but a state that reads exactly like success from an exit
    status alone, which is how it stops being short.
    """

    def test_the_local_backup_still_runs(self, backup):
        result = backup(OFFSITE_DEST="")

        assert result.returncode == 0
        assert [name for name in _names(backup.dir) if name.endswith(".dump")]

    def test_it_says_so_every_single_night(self, backup):
        result = backup(OFFSITE_DEST="")

        assert "OFFSITE_DEST is not set" in result.stderr
        assert "only copy" in result.stderr

    def test_it_does_not_claim_an_offsite_copy_it_did_not_make(self, backup):
        result = backup(OFFSITE_DEST="")

        assert "verified offsite" not in result.stdout
