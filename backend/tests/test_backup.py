"""The nightly backup script, run for real against a stubbed ``docker``.

The other deployment tests read files and assert on their contents. This one
cannot: the thing worth testing here is not what ``caflow-backup.sh`` says but
what it does when a step goes wrong — and every interesting failure is a
command that exits zero having written the wrong bytes.

So ``docker`` is replaced by a shim on ``PATH`` that plays the three roles the
script asks of it (``pg_dump``, ``pg_restore``, ``tar``), each of which the
tests can make succeed, fail, or succeed dishonestly. The script itself runs
unmodified, in ``sh``, and writes into a temporary directory.

What that buys is coverage of the two properties a backup has to have:

* a file is named ``caflow-db-….dump`` only after it has been read back, so a
  run that captured an error message instead of a dump fails loudly on the
  night it happens rather than quietly on the day of the restore; and
* a run that failed does not prune, because a fortnight of quiet failures
  followed by a prune is how the last good backup gets deleted.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKUP_SCRIPT = REPO_ROOT / "deploy" / "caflow-backup.sh"

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

FAKE_DOCKER = r"""#!/bin/sh
# Stands in for `docker compose exec -T <service> <command> …`. Which of the
# three roles is being asked for is decided by the command name in the args.
set -eu

mode=""
for arg in "$@"; do
  case "$arg" in
    pg_dump) mode=dump; break ;;
    pg_restore) mode=restore; break ;;
    tar) mode=docs; break ;;
  esac
done

case "$mode" in
  dump)
    if [ "${FAKE_PG_DUMP_STATUS:-0}" != 0 ]; then
      echo "pg_dump: error: connection to server failed" >&2
      exit "$FAKE_PG_DUMP_STATUS"
    fi
    printf '%s' "$FAKE_DB_DUMP"
    ;;
  restore)
    data=$(cat)
    case "$data" in
      PGDMP*) ;;
      *)
        echo "pg_restore: error: did not find magic string in file header" >&2
        exit 1
        ;;
    esac
    printf '%s\n' "$FAKE_TOC"
    ;;
  docs)
    if [ "${FAKE_DOCS_OK:-1}" = 1 ]; then
      tar -czf - -C "$FAKE_DOCS_SRC" documents
    else
      printf 'this is not a gzip stream'
    fi
    ;;
  *)
    exit 0
    ;;
esac
"""


@pytest.fixture
def backup(tmp_path):
    """Runs the real script with a stubbed ``docker``. Returns a callable."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shim = bin_dir / "docker"
    shim.write_text(FAKE_DOCKER)
    shim.chmod(0o755)

    # Something for the stubbed `tar` to actually archive, so the documents
    # backup is a real gzip stream on the happy path.
    docs_src = tmp_path / "volume"
    (docs_src / "documents").mkdir(parents=True)
    (docs_src / "documents" / "bank-statement.pdf").write_bytes(b"%PDF-1.4 pretend")

    backup_dir = tmp_path / "backups"

    def run(**overrides) -> subprocess.CompletedProcess:
        env = {
            **os.environ,
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "BACKUP_DIR": str(backup_dir),
            "COMPOSE_DIR": str(REPO_ROOT),
            "FAKE_DB_DUMP": HEALTHY_DUMP,
            "FAKE_TOC": HEALTHY_TOC,
            "FAKE_DOCS_SRC": str(docs_src),
        }
        env.update({key: str(value) for key, value in overrides.items()})
        return subprocess.run(
            ["sh", str(BACKUP_SCRIPT)],
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )

    run.dir = backup_dir  # type: ignore[attr-defined]
    return run


def _names(backup_dir: Path) -> list[str]:
    return sorted(path.name for path in backup_dir.iterdir()) if backup_dir.exists() else []


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


class TestABackupIsReadBackBeforeItIsNamed:
    """A file of plausible size is not a backup, and the difference is unread.

    Every case here exits zero somewhere along the way — a redirect that caught
    an error message, a disk that filled between the first byte and the last, a
    container answering on a database that is not this one. Without reading the
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

    def test_a_documents_archive_that_does_not_read_back_is_not_kept(self, backup):
        result = backup(FAKE_DOCS_OK=0)

        assert result.returncode != 0
        assert "does not read back" in result.stderr
        names = _names(backup.dir)
        assert not [name for name in names if name.endswith((".tar.gz", ".part"))]
        # The database dump had already been verified and named by then, and
        # keeping it is the right answer — half a backup beats none.
        assert [name for name in names if name.endswith(".dump")]


class TestPruningOnlyFollowsASuccess:
    """The prune is the one destructive line in the file.

    It runs last, after both archives have been written and read back, and
    ``set -e`` means a failure anywhere above it exits first. That ordering is
    what stops a fortnight of quiet failures from ending with the last good
    backup deleted on day fifteen — precisely when it is about to be needed.
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

    def test_a_run_that_fails_on_the_documents_prunes_nothing_either(self, backup):
        backup.dir.mkdir(parents=True)
        stale = backup.dir / "caflow-documents-20250101T000000Z.tar.gz"
        stale.write_text("the last good documents archive")
        _age(stale, days=30)

        assert backup(FAKE_DOCS_OK=0).returncode != 0
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
