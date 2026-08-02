"""Release snapshots and the way back, run for real in a temporary directory.

``deploy/hetzner/caflow-release.sh`` is the whole of the rollback story on the
shared box: ``deploy.sh`` calls ``snapshot`` before it overwrites the tree, and
``rollback.sh`` calls ``restore`` when that turns out to have been a mistake.
Both are thin ssh wrappers around this file, which is why this file is the one
with tests — the logic that can be wrong lives here.

The properties worth pinning are the ones that only fail on the day they are
used:

* a snapshot must hold still while the deploy rsyncs over the tree beside it,
  or "roll back" restores the release being rolled back;
* uploaded client documents must not travel with the code, or a rollback is a
  worse outage than whatever prompted it;
* an interrupted snapshot must not be offered as somewhere to go back to; and
* the schema is not a file, so a restore across a migration has to say so
  rather than quietly leaving old code on a new database.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
RELEASE_SCRIPT = REPO_ROOT / "deploy" / "hetzner" / "caflow-release.sh"

# Stands in for `alembic current`, which is the one thing here that needs a
# database. FAKE_REVISION is what it claims the database is on.
FAKE_ALEMBIC = r"""#!/bin/sh
set -eu
case "${1:-}" in
  current) printf '%s\n' "${FAKE_REVISION:-}" ;;
  *) exit 1 ;;
esac
"""


@pytest.fixture
def box(tmp_path):
    """A pretend /opt/CAFlow, with a runner for the script against it."""
    app_dir = tmp_path / "opt" / "CAFlow"
    (app_dir / "backend" / "app").mkdir(parents=True)
    (app_dir / "backend" / "var" / "documents").mkdir(parents=True)
    (app_dir / "frontend" / "dist").mkdir(parents=True)

    (app_dir / "backend" / "app" / "main.py").write_text("# release one\n")
    (app_dir / "backend" / "requirements.txt").write_text("fastapi==0.1\n")
    (app_dir / "frontend" / "dist" / "index.html").write_text("<!-- one -->")
    (app_dir / ".env").write_text("SECRET_KEY=the-live-one\n")
    (app_dir / "backend" / "var" / "documents" / "client-pan.pdf").write_bytes(b"%PDF live")

    alembic = tmp_path / "bin" / "alembic"
    alembic.parent.mkdir()
    alembic.write_text(FAKE_ALEMBIC)
    alembic.chmod(0o755)

    releases_dir = tmp_path / "opt" / "CAFlow-releases"

    def run(*args, **overrides) -> subprocess.CompletedProcess:
        env = {
            **os.environ,
            "APP_DIR": str(app_dir),
            "RELEASES_DIR": str(releases_dir),
            "ALEMBIC_CMD": str(alembic),
            "FAKE_REVISION": "a1b2c3d4e5f6",
        }
        env.update({key: str(value) for key, value in overrides.items()})
        return subprocess.run(
            ["sh", str(RELEASE_SCRIPT), *args],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )

    run.app = app_dir  # type: ignore[attr-defined]
    run.releases = releases_dir  # type: ignore[attr-defined]
    return run


def _snapshot_names(box) -> list[str]:
    if not box.releases.exists():
        return []
    return sorted(path.name for path in box.releases.iterdir())


def _deploy_over(box, marker: str) -> None:
    """What deploy.sh does to the tree: rsync, which renames over its targets.

    Written the same way deliberately. A snapshot survives a deploy because
    rsync replaces the directory entry rather than writing through it — so a
    test that edited the file in place would prove something the deploy does
    not do, and would keep passing if the snapshot stopped working.
    """
    for relative, content in (
        ("backend/app/main.py", f"# {marker}\n"),
        ("frontend/dist/index.html", f"<!-- {marker} -->"),
    ):
        target = box.app / relative
        temporary = target.with_suffix(target.suffix + ".tmp")
        temporary.write_text(content)
        temporary.replace(target)


class TestTakingASnapshot:
    def test_it_records_the_tree_the_git_sha_and_the_revision(self, box):
        result = box("snapshot", "abc1234")

        assert result.returncode == 0, result.stderr
        name = _snapshot_names(box)[0]
        meta = (box.releases / name / "meta").read_text()
        assert "git=abc1234" in meta
        assert "revision=a1b2c3d4e5f6" in meta
        assert (box.releases / name / "tree" / "backend" / "app" / "main.py").exists()

    def test_it_costs_inodes_and_not_data(self, box):
        """Hardlinks, so a snapshot per deploy does not fill a 38 GB shared disk."""
        box("snapshot", "abc1234")

        name = _snapshot_names(box)[0]
        live = box.app / "backend" / "app" / "main.py"
        copied = box.releases / name / "tree" / "backend" / "app" / "main.py"
        assert live.stat().st_ino == copied.stat().st_ino

    def test_it_holds_still_while_a_deploy_overwrites_the_tree(self, box):
        """The property the whole mechanism rests on.

        rsync renames a temporary file over its target rather than writing
        through it, so the snapshot's hardlink keeps pointing at the old
        content. If that ever stopped being true, `restore` would put back the
        release being rolled back — and it would look like it worked.
        """
        box("snapshot", "release-one")
        _deploy_over(box, "release two")

        name = _snapshot_names(box)[0]
        snapshotted = box.releases / name / "tree" / "backend" / "app" / "main.py"
        assert snapshotted.read_text() == "# release one\n"
        assert (box.app / "backend" / "app" / "main.py").read_text() == "# release two\n"

    @pytest.mark.parametrize(
        "excluded",
        [".env", "backend/var/documents/client-pan.pdf"],
    )
    def test_secrets_and_client_documents_stay_out_of_it(self, box, excluded):
        """Neither is code, and restoring either would be its own incident.

        A rolled-back .env restores a rotated signing key. A rolled-back
        documents directory throws away every upload since the snapshot — a
        client asked to send their bank statements again, which is the exact
        thing the backups exist to prevent.
        """
        box("snapshot", "abc1234")

        name = _snapshot_names(box)[0]
        assert not (box.releases / name / "tree" / excluded).exists()

    def test_the_frontend_bundle_travels_with_the_backend(self, box):
        """dist/ is gitignored and built on the laptop, so nothing else has it.

        A rollback that restored the API and left the new bundle in place would
        pair old routes with a frontend written against the new ones, which is
        a subtler outage than being down.
        """
        box("snapshot", "abc1234")

        name = _snapshot_names(box)[0]
        assert (box.releases / name / "tree" / "frontend" / "dist" / "index.html").exists()

    def test_a_second_snapshot_in_the_same_second_gets_its_own_directory(self, box):
        for _ in range(3):
            assert box("snapshot", "abc1234").returncode == 0

        assert len(_snapshot_names(box)) == 3

    def test_a_missing_app_dir_is_refused_rather_than_snapshotted_empty(self, box, tmp_path):
        result = box("snapshot", "abc1234", APP_DIR=str(tmp_path / "nowhere"))

        assert result.returncode != 0
        assert "does not exist" in result.stderr

    def test_a_database_it_cannot_reach_does_not_stop_the_deploy(self, box):
        """No revision is a real answer, and better than refusing to snapshot.

        The alternative is a deploy that cannot proceed because the thing it
        was about to fix is broken.
        """
        result = box("snapshot", "abc1234", FAKE_REVISION="could not connect to server")

        assert result.returncode == 0
        name = _snapshot_names(box)[0]
        assert "revision=\n" in (box.releases / name / "meta").read_text()


class TestRetention:
    def test_it_keeps_the_most_recent_ones(self, box):
        for index in range(7):
            box("snapshot", f"sha{index}", KEEP_RELEASES=3)

        assert len(_snapshot_names(box)) == 3

    def test_the_ones_it_keeps_are_the_newest(self, box):
        for index in range(5):
            box("snapshot", f"sha{index}", KEEP_RELEASES=2)

        kept = "".join((box.releases / name / "meta").read_text() for name in _snapshot_names(box))
        assert "git=sha4" in kept
        assert "git=sha3" in kept
        assert "git=sha0" not in kept

    def test_a_snapshot_does_not_prune_itself(self, box):
        """Several snapshots inside one second, which is what these tests are.

        The names carry a `-1` suffix to stay distinct, and a suffixed name
        sorts after the plain one from the *following* second — so ordering
        them by name puts them in the wrong order, and a name freed by the
        prune is reclaimed by the next snapshot, which then looks like the
        oldest thing in the directory and deletes what it has just written.
        Ordering by mtime is what "newest" means here.
        """
        for index in range(5):
            box("snapshot", f"sha{index}", KEEP_RELEASES=2)

        kept = "".join((box.releases / name / "meta").read_text() for name in _snapshot_names(box))
        assert "git=sha4" in kept, "the newest snapshot pruned itself"

    def test_nothing_that_is_not_a_snapshot_is_deleted(self, box):
        """The directory is under /opt, and the prune is an `rm -rf` in a loop."""
        box.releases.mkdir(parents=True, exist_ok=True)
        bystander = box.releases / "notes-from-the-migration.txt"
        bystander.write_text("do not delete me")

        for index in range(4):
            box("snapshot", f"sha{index}", KEEP_RELEASES=1)

        assert bystander.exists()


class TestListing:
    def test_it_shows_the_newest_first(self, box):
        box("snapshot", "older")
        box("snapshot", "newer")

        listed = box("list").stdout.splitlines()

        assert "newer" in listed[0]
        assert "older" in listed[1]

    def test_an_empty_directory_says_so_rather_than_printing_nothing(self, box):
        result = box("list")

        assert "no complete snapshots" in result.stderr

    def test_an_interrupted_snapshot_is_not_offered(self, box):
        """The `complete` marker is written last, after the copy.

        A snapshot interrupted partway through is half a deployment. Restoring
        one during an outage would turn a rollback into a fresh incident, in
        the state of mind least able to notice.
        """
        box("snapshot", "good")
        half_copied = box.releases / "20250101T000000Z"
        (half_copied / "tree").mkdir(parents=True)
        (half_copied / "meta").write_text("stamp=20250101T000000Z\ngit=half\nrevision=\n")

        assert "half" not in box("list").stdout


class TestRestoring:
    def test_previous_means_the_state_before_the_last_deploy(self, box):
        box("snapshot", "release-one")
        _deploy_over(box, "release two")

        assert box("restore", "previous").returncode == 0

        assert (box.app / "backend" / "app" / "main.py").read_text() == "# release one\n"
        assert (box.app / "frontend" / "dist" / "index.html").read_text() == "<!-- one -->"

    def test_a_named_snapshot_can_be_restored(self, box):
        box("snapshot", "release-one")
        name = _snapshot_names(box)[0]
        _deploy_over(box, "release two")

        assert box("restore", name).returncode == 0
        assert (box.app / "backend" / "app" / "main.py").read_text() == "# release one\n"

    def test_a_change_that_kept_the_byte_count_is_still_rolled_back(self, box):
        """rsync's default quick check is size-and-mtime, and this defeats it.

        ``# release one`` and ``# release two`` are the same length, and a
        deploy and a rollback minutes apart can land inside the same mtime
        comparison. The changes people actually roll back — a version bump, an
        inverted boolean, a flipped comparison — are disproportionately the
        ones that keep the byte count, so the restore reads the bytes rather
        than trusting the metadata.
        """
        box("snapshot", "release-one")
        _deploy_over(box, "release two")
        assert len("# release one\n") == len("# release two\n")

        box("restore", "previous")

        assert (box.app / "backend" / "app" / "main.py").read_text() == "# release one\n"

    def test_a_file_added_after_the_snapshot_is_removed(self, box):
        """--delete, because a rollback that leaves the new release behind is not one.

        A module deleted between the two versions would still import, and a
        migration file that only exists in the newer release would still be
        discoverable by `alembic upgrade head`.
        """
        box("snapshot", "release-one")
        stray = box.app / "backend" / "app" / "added_in_release_two.py"
        stray.write_text("# only in the release being rolled back\n")

        box("restore", "previous")

        assert not stray.exists()

    def test_the_live_env_file_survives_the_restore(self, box):
        """Excluded from the restore by the same list that excluded it from the copy.

        One list rather than two: the failure mode of the two drifting apart is
        a rollback that overwrites the live secrets with a snapshot that never
        contained them — which, with rsync --delete, means deleting them.
        """
        box("snapshot", "release-one")
        box.app.joinpath(".env").write_text("SECRET_KEY=rotated-since\n")

        box("restore", "previous")

        assert box.app.joinpath(".env").read_text() == "SECRET_KEY=rotated-since\n"

    def test_documents_uploaded_since_the_snapshot_survive_the_restore(self, box):
        """The single worst thing a rollback could do, and the easiest to do by accident."""
        box("snapshot", "release-one")
        since = box.app / "backend" / "var" / "documents" / "uploaded-this-morning.pdf"
        since.write_bytes(b"%PDF new")

        box("restore", "previous")

        assert since.exists()
        assert (box.app / "backend" / "var" / "documents" / "client-pan.pdf").exists()

    def test_restoring_nothing_is_refused(self, box):
        result = box("restore", "previous")

        assert result.returncode != 0
        assert "nothing to roll back to" in result.stderr

    def test_an_incomplete_snapshot_is_refused_by_name_too(self, box):
        half_copied = box.releases / "20250101T000000Z"
        (half_copied / "tree").mkdir(parents=True)

        result = box("restore", "20250101T000000Z")

        assert result.returncode != 0
        assert "not a complete snapshot" in result.stderr


class TestTheSchemaIsNotAFile:
    """The half of a rollback that a directory copy cannot perform.

    Old code against a new schema is not the previous release; it is a new
    failure, in a state nobody has ever run. So a restore across a migration
    finishes the file half, refuses to pretend the rest happened, and hands
    over the command and the caveat.
    """

    def test_it_warns_when_the_database_has_moved_on(self, box):
        box("snapshot", "release-one", FAKE_REVISION="aaaa1111")

        result = box("restore", "previous", FAKE_REVISION="bbbb2222")

        assert result.returncode == 3
        assert "aaaa1111" in result.stderr
        assert "bbbb2222" in result.stderr

    def test_the_code_is_restored_anyway(self, box):
        """Stopping short would leave the box on the broken release *and* mid-rollback."""
        box("snapshot", "release-one", FAKE_REVISION="aaaa1111")
        _deploy_over(box, "release two")

        box("restore", "previous", FAKE_REVISION="bbbb2222")

        assert (box.app / "backend" / "app" / "main.py").read_text() == "# release one\n"

    def test_it_does_not_run_the_downgrade_itself(self, box):
        """`alembic downgrade` drops columns, and the rows in them are not coming back.

        That is a decision belonging to a person who can look at what has been
        written since the deploy — so the command is printed, with what it
        costs, and not executed.
        """
        box("snapshot", "release-one", FAKE_REVISION="aaaa1111")

        result = box("restore", "previous", FAKE_REVISION="bbbb2222")

        assert "downgrade aaaa1111" in result.stderr
        assert "including anything written since" in result.stderr

    def test_an_unchanged_schema_is_a_clean_rollback(self, box):
        box("snapshot", "release-one", FAKE_REVISION="aaaa1111")

        assert box("restore", "previous", FAKE_REVISION="aaaa1111").returncode == 0

    def test_a_revision_that_cannot_be_read_does_not_block_the_restore(self, box):
        """Unknown is not "different". A database that cannot be reached during an
        outage is the likeliest case there is, and it must not be the thing that
        stops the rollback."""
        box("snapshot", "release-one", FAKE_REVISION="aaaa1111")

        assert box("restore", "previous", FAKE_REVISION="").returncode == 0


class TestTheCommandLine:
    @pytest.mark.parametrize("args", [(), ("frobnicate",), ("restore",)])
    def test_a_call_it_does_not_understand_prints_usage_and_stops(self, box, args):
        result = box(*args)

        assert result.returncode == 2
        assert "usage:" in result.stderr
