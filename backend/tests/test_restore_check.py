"""The restore drill, run for real against a postgres the tests get to lie to.

``deploy/hetzner/caflow-restore-check.sh`` exists because ``caflow-backup``'s
own verification stops at the archive's table of contents. That catches a
truncated file; it does not catch an archive whose TOC is perfect and whose
data section is empty, which is the failure that survives a year of green
nightly runs and is discovered on the one morning it matters.

So the shims here are built to produce exactly that: a ``pg_restore`` that
exits zero having restored a schema and no rows, a dump of a database that is
not this one, a documents tarball that unpacks cleanly and is missing a
fortnight of uploads. Each of them is a passing backup by every check that
existed before this file.

The pretend cluster is a directory. ``createdb`` makes one, ``dropdb`` removes
it, ``pg_restore`` writes a table manifest into it from the dump, and ``psql``
answers the four questions the script actually asks. That the drill drops its
scratch database — on the failures as much as on the passes — is itself tested,
because a drill that leaves a full copy of a CA firm's client records lying
around has created a worse problem than the one it was checking for.
"""

from __future__ import annotations

import os
import subprocess
import tarfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
RESTORE_CHECK = REPO_ROOT / "deploy" / "hetzner" / "caflow-restore-check.sh"

# `sudo -u postgres <command> …` — drop the two flag words and run the rest, so
# the shims below can be ordinary programs on PATH.
FAKE_SUDO = r"""#!/bin/sh
set -eu
[ "${1:-}" = "-u" ] && shift 2
exec "$@"
"""

# A database is a directory holding one `tables` file: one line per table, the
# name then its row count. `alembic_version` carries the revision in the count
# column, which is the only place the shape differs and keeps the shim to one
# file.
FAKE_CREATEDB = r"""#!/bin/sh
set -eu
for arg in "$@"; do case "$arg" in -*) ;; *) db=$arg ;; esac; done
mkdir -p "$FAKE_PG_DIR/$db"
: >"$FAKE_PG_DIR/$db/tables"
"""

FAKE_DROPDB = r"""#!/bin/sh
set -eu
for arg in "$@"; do case "$arg" in -*) ;; *) db=$arg ;; esac; done
rm -rf "${FAKE_PG_DIR:?}/$db"
"""

# Reads the dump on stdin — which in these tests *is* the manifest — and writes
# it into the target database. FAKE_PG_RESTORE_NOISE makes it complain the way
# a real one does across postgres minor versions while still restoring; the
# script deliberately does not use --exit-on-error, and that is worth pinning.
FAKE_PG_RESTORE = r"""#!/bin/sh
set -eu
db=""
prev=""
for arg in "$@"; do
  [ "$prev" = "-d" ] && db=$arg
  prev=$arg
done
cat >"$FAKE_PG_DIR/$db/tables"
if [ -n "${FAKE_PG_RESTORE_NOISE:-}" ]; then
  echo "pg_restore: warning: errors ignored on restore: 3" >&2
  exit 1
fi
"""

# The four questions the script asks, and nothing else.
FAKE_PSQL = r"""#!/bin/sh
set -eu
db=""
query=""
prev=""
for arg in "$@"; do
  [ "$prev" = "-d" ] && db=$arg
  [ "$prev" = "-tAc" ] && query=$arg
  prev=$arg
done
tables="$FAKE_PG_DIR/$db/tables"
[ -f "$tables" ] || { echo "psql: FATAL: database \"$db\" does not exist" >&2; exit 2; }

case "$query" in
  *to_regclass*)
    name=$(printf '%s' "$query" | sed -e "s/.*public\.//" -e "s/'.*//")
    if awk -v n="$name" '$1 == n {found=1} END {exit !found}' "$tables"; then
      echo t
    else
      echo f
    fi
    ;;
  *version_num*)
    rev=$(awk '$1 == "alembic_version" {print $2}' "$tables")
    [ -n "$rev" ] || exit 1
    printf '%s\n' "$rev"
    ;;
  *"count(*)"*)
    name=$(printf '%s' "$query" | sed -e 's/.*FROM //' -e 's/[^a-z_].*//')
    awk -v n="$name" '$1 == n {print $2; found=1} END {exit !found}' "$tables"
    ;;
  *) exit 1 ;;
esac
"""

FAKE_ALEMBIC = r"""#!/bin/sh
set -eu
case "${1:-}" in
  heads) printf '%s (head)\n' "${FAKE_HEAD_REVISION:-a1b2c3d4}" ;;
  *) exit 1 ;;
esac
"""

# What a healthy CAFlow database restores to: every required table present, the
# 18 seeded compliance types, and a revision.
HEALTHY = """firms 1
practitioners 3
clients 40
compliance_types 18
compliance_items 260
documents 512
invoices 22
alembic_version a1b2c3d4
"""


@pytest.fixture
def drill(tmp_path):
    """The real script, against a pretend cluster and a real backup directory."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (
        ("sudo", FAKE_SUDO),
        ("createdb", FAKE_CREATEDB),
        ("dropdb", FAKE_DROPDB),
        ("pg_restore", FAKE_PG_RESTORE),
        ("psql", FAKE_PSQL),
        ("alembic", FAKE_ALEMBIC),
    ):
        shim = bin_dir / name
        shim.write_text(body)
        shim.chmod(0o755)

    pg_dir = tmp_path / "cluster"
    # The live database, which the drill compares its restored counts against.
    (pg_dir / "caflow").mkdir(parents=True)
    (pg_dir / "caflow" / "tables").write_text(HEALTHY)

    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    dump = backup_dir / "caflow-db-20260802T021500Z.dump"
    dump.write_text(HEALTHY)

    app_dir = tmp_path / "opt" / "CAFlow"
    storage = app_dir / "backend" / "var" / "documents"
    storage.mkdir(parents=True)

    state_file = tmp_path / "restore-check.state"

    def write_documents(archived: int, on_disk: int) -> Path:
        """A documents tarball holding `archived` files, next to `on_disk` live ones."""
        for existing in storage.iterdir():
            existing.unlink()
        for index in range(on_disk):
            (storage / f"doc-{index}.pdf").write_text("x")

        archive = backup_dir / "caflow-documents-20260802T021500Z.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            for index in range(archived):
                member = storage / f"doc-{index}.pdf"
                if not member.exists():
                    member.write_text("x")
                tar.add(member, arcname=f"documents/doc-{index}.pdf")
        # Written for the archive's benefit only; put the live directory back.
        for existing in storage.iterdir():
            existing.unlink()
        for index in range(on_disk):
            (storage / f"doc-{index}.pdf").write_text("x")
        return archive

    def run(**overrides) -> subprocess.CompletedProcess:
        env = {
            **os.environ,
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "FAKE_PG_DIR": str(pg_dir),
            "BACKUP_DIR": str(backup_dir),
            "PGDATABASE": "caflow",
            "SCRATCH_DB": "caflow_restore_check",
            "APP_DIR": str(app_dir),
            "STORAGE_DIR": str(storage),
            "ALEMBIC_CMD": "alembic",
            "ENV_FILE": str(app_dir / ".env"),
            "STATE_FILE": str(state_file),
        }
        env.update({key: str(value) for key, value in overrides.items()})
        return subprocess.run(
            ["sh", str(RESTORE_CHECK)],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )

    run.dump = dump  # type: ignore[attr-defined]
    run.backups = backup_dir  # type: ignore[attr-defined]
    run.cluster = pg_dir  # type: ignore[attr-defined]
    run.state = state_file  # type: ignore[attr-defined]
    run.storage = storage  # type: ignore[attr-defined]
    run.write_documents = write_documents  # type: ignore[attr-defined]
    return run


class TestABackupThatIsActuallyRestorable:
    def test_it_passes_and_says_what_it_found(self, drill):
        drill.write_documents(archived=512, on_disk=512)

        result = drill()

        assert result.returncode == 0, result.stderr
        assert "restored clean" in result.stdout
        assert "40 clients" in result.stdout
        assert "18 compliance types" in result.stdout

    def test_it_records_when_the_drill_last_passed(self, drill):
        """The file caflow-monitor watches.

        Its age is the age of the last time anyone knew the firm could be
        brought back, which is the number worth alerting on — a drill failing
        every Sunday and a drill not running at all look identical without it.
        """
        drill.write_documents(archived=512, on_disk=512)

        drill()

        recorded = drill.state.read_text()
        assert "passed=" in recorded
        assert "clients=40" in recorded

    def test_a_noisy_pg_restore_that_still_restored_is_not_a_failure(self, drill):
        """Ownership and extension complaints across postgres minor versions.

        --exit-on-error would fail the drill on the first of them, which is how
        a check like this gets switched off inside a month. What landed is
        interrogated instead, which is the better question anyway.
        """
        drill.write_documents(archived=512, on_disk=512)

        result = drill(FAKE_PG_RESTORE_NOISE="1")

        assert result.returncode == 0, result.stderr
        assert "checking what landed anyway" in result.stderr


class TestWhatOnlyARestoreCanCatch:
    def test_an_archive_whose_data_section_is_empty(self, drill):
        """The whole reason this script exists.

        The table of contents is intact, so `pg_restore --list` passes and
        caflow-backup calls it a good backup. Every table restores. Not one row
        does. Nothing before this file could tell the difference.
        """
        drill.dump.write_text(HEALTHY.replace("compliance_types 18", "compliance_types 0"))

        result = drill()

        assert result.returncode != 0
        assert "no seeded compliance types" in result.stderr
        assert "data section is not" in result.stderr

    def test_a_dump_of_a_database_that_is_not_this_one(self, drill):
        """Restores perfectly. Holds somebody else's — or nobody's — clients.

        An older cluster on a box that has two, a database that was mid-restore
        when the dump ran, a pg_dump pointed somewhere nobody meant. The archive
        is valid in every way an archive can be checked.
        """
        drill.dump.write_text(HEALTHY.replace("clients 40", "clients 2"))

        result = drill()

        assert result.returncode != 0
        assert "probably not of this database" in result.stderr

    def test_a_missing_table_is_not_a_restored_database(self, drill):
        drill.dump.write_text(HEALTHY.replace("invoices 22\n", ""))

        result = drill()

        assert result.returncode != 0
        assert "invoices" in result.stderr

    def test_a_night_of_new_clients_is_not_a_discrepancy(self, drill):
        """The dump is always a little behind the live database. That is what a
        nightly backup is, and a check that flags it is a check nobody reads."""
        drill.write_documents(archived=512, on_disk=512)
        drill.dump.write_text(HEALTHY.replace("clients 40", "clients 38"))

        assert drill().returncode == 0

    def test_a_documents_archive_missing_most_of_the_uploads(self, drill):
        """Unpacks cleanly, and is not a backup of the documents directory.

        A CA firm's uploads are the half of this that cannot be regenerated
        from anything. `tar -tzf` proves the gzip stream ends where it should
        and says nothing at all about what is inside it.
        """
        drill.write_documents(archived=10, on_disk=512)

        result = drill()

        assert result.returncode != 0
        assert "10 files" in result.stderr

    def test_a_new_deployment_with_no_documents_yet_is_fine(self, drill):
        drill.write_documents(archived=0, on_disk=0)

        assert drill().returncode == 0


class TestTheSchemaTheCodeExpects:
    def test_a_dump_predating_a_migration_says_so_without_failing(self, drill):
        """Normal, and the answer is "replay the migrations after restoring".

        Worth knowing in advance rather than after the restore, which is the
        moment it otherwise surfaces — as a working database and a broken site.
        """
        drill.write_documents(archived=512, on_disk=512)

        result = drill(FAKE_HEAD_REVISION="e5f6a7b8")

        assert result.returncode == 0, result.stderr
        assert "code expects e5f6a7b8" in result.stderr
        assert "upgrade head" in result.stderr

    def test_a_matching_revision_is_not_mentioned(self, drill):
        drill.write_documents(archived=512, on_disk=512)

        result = drill(FAKE_HEAD_REVISION="a1b2c3d4")

        assert "code expects" not in result.stderr


class TestItDoesNotLeaveACopyOfTheFirmsDataBehind:
    def scratch(self, drill) -> Path:
        return drill.cluster / "caflow_restore_check"

    def test_the_scratch_database_is_dropped_on_a_pass(self, drill):
        drill.write_documents(archived=512, on_disk=512)

        drill()

        assert not self.scratch(drill).exists()

    def test_and_on_a_failure(self, drill):
        """The case that matters more.

        A drill that fails and leaves a half-restored copy of a CA firm's client
        records on the disk has replaced the problem it was checking for with a
        worse one — and failures are exactly when nobody is watching.
        """
        drill.dump.write_text(HEALTHY.replace("compliance_types 18", "compliance_types 0"))

        result = drill()

        assert result.returncode != 0
        assert not self.scratch(drill).exists()

    def test_keep_scratch_is_the_deliberate_exception(self, drill):
        """For the morning someone is standing in front of a failure."""
        drill.write_documents(archived=512, on_disk=512)

        drill(KEEP_SCRATCH="1")

        assert self.scratch(drill).exists()

    def test_it_refuses_to_point_the_drill_at_production(self, drill):
        """This file's whole job is to drop a database.

        The names are compared before anything else happens, so a configuration
        that would aim it at the live one stops at the top rather than at the
        `dropdb` further down.
        """
        result = drill(SCRATCH_DB="caflow")

        assert result.returncode != 0
        assert "refusing" in result.stderr
        assert (drill.cluster / "caflow" / "tables").read_text() == HEALTHY


class TestWhenThereIsNothingToDrill:
    def test_no_dump_at_all_is_a_failure_not_a_pass(self, drill):
        for dump in drill.backups.iterdir():
            dump.unlink()

        result = drill()

        assert result.returncode != 0
        assert "no database dump" in result.stderr

    def test_a_failed_drill_does_not_refresh_the_state_file(self, drill):
        """Otherwise the monitor's freshness check reads a failing drill as a
        passing one, and the alert that should have fired never does."""
        drill.dump.write_text(HEALTHY.replace("compliance_types 18", "compliance_types 0"))

        drill()

        assert not drill.state.exists()

    def test_the_newest_dump_is_the_one_drilled(self, drill):
        """Not whichever the shell happened to list first."""
        (drill.backups / "caflow-db-20260101T021500Z.dump").write_text(
            HEALTHY.replace("compliance_types 18", "compliance_types 0")
        )
        drill.write_documents(archived=512, on_disk=512)

        result = drill()

        assert result.returncode == 0, result.stderr
        assert "20260802T021500Z" in result.stdout
