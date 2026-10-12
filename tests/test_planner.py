"""Tests for the fetch plan: what gets downloaded, what is skipped, what is refused."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ia_reviver.bencode import parse_torrent
from ia_reviver.planner import (
    FETCH,
    HAVE,
    TOUCH,
    UNAVAILABLE,
    NotArchiveTorrent,
    PlanError,
    build_plan,
    needs_help,
)
from ia_reviver.qbittorrent import Torrent
from tests.fakes import FakeArchive, bencode, qbt_file, qbt_torrent

IDENT = "fixture_item"
PAYLOADS = {
    "alpha.txt": b"A" * 4096,
    "beta.txt": b"B" * 2048,
    "empty.txt": b"",
}


class FakeClient:
    """Minimal stand-in for :class:`ia_reviver.qbittorrent.QbitClient`."""

    def __init__(self, files):
        self._files = files

    def files(self, torrent_hash):
        return list(self._files)


class PlannerTestCase(unittest.TestCase):
    def setUp(self):
        raw, manifest = self.__class__.make_item()
        self.raw = raw
        self.manifest = manifest
        self.item_torrent = parse_torrent(raw)
        self.archive = FakeArchive(identifier=IDENT, torrent=self.item_torrent, item_files=manifest)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    @staticmethod
    def make_item():
        from tests.fakes import make_item

        return make_item(PAYLOADS, identifier=IDENT)

    def torrent(self, *, progress=0.0, save_path=None, **kwargs):
        save_path = save_path or str(self.root)
        total = sum(len(p) for p in PAYLOADS.values())
        defaults = {
            "name": IDENT,
            "torrent_hash": self.item_torrent.info_hash,
            "size": total,
            "amount_left": total,
            "progress": progress,
            "save_path": save_path,
            "comment": f"hosted at https://archive.org/details/{IDENT}",
        }
        defaults.update(kwargs)
        return Torrent(qbt_torrent(**defaults))

    def files(self, *, missing=()):
        """qBittorrent file list for the synthetic item, minus ``missing``."""
        out = []
        for name, payload in PAYLOADS.items():
            if name in missing:
                continue
            out.append(qbt_file(f"{IDENT}/{name}", len(payload), 0.0))
        return out

    def build(self, client, torrent, **kwargs):
        return build_plan(client, self.archive, torrent, **kwargs)


class TestPlanning(PlannerTestCase):
    def test_everything_missing_is_scheduled_for_download(self):
        plan = self.build(FakeClient(self.files()), self.torrent())
        self.assertEqual(plan.identifier, IDENT)
        self.assertEqual(len(plan.files), 3)
        # The empty file needs no transfer, only creation, so it is not counted
        # as a download but must still appear in the file list.
        self.assertEqual(plan.to_fetch, 2)
        expected = sum(len(p) for name, p in PAYLOADS.items() if name != "empty.txt")
        self.assertEqual(plan.to_fetch_bytes, expected)
        self.assertTrue(plan.revivable)
        self.assertFalse(plan.stale)
        actions = {f.item_name: f.action for f in plan.files}
        self.assertEqual(actions, {"alpha.txt": FETCH, "beta.txt": FETCH, "empty.txt": TOUCH})

    def test_empty_file_is_created_not_downloaded(self):
        plan = self.build(FakeClient(self.files()), self.torrent())
        empty = next(f for f in plan.files if f.item_name == "empty.txt")
        self.assertEqual(empty.action, TOUCH)
        self.assertEqual(empty.size, 0)
        self.assertNotIn(FETCH, [empty.action])

    def test_root_folder_is_stripped_to_find_the_item_path(self):
        plan = self.build(FakeClient(self.files()), self.torrent())
        names = {f.item_name for f in plan.files}
        self.assertEqual(names, set(PAYLOADS))
        # The on-disk path keeps the root folder, the item path does not.
        alpha = next(f for f in plan.files if f.item_name == "alpha.txt")
        self.assertEqual(alpha.rel, f"{IDENT}/alpha.txt")
        self.assertEqual(alpha.dest, self.root / IDENT / "alpha.txt")

    def test_works_when_qbittorrent_creates_no_root_folder(self):
        files = [qbt_file(name, len(payload)) for name, payload in PAYLOADS.items()]
        plan = self.build(FakeClient(files), self.torrent())
        self.assertEqual({f.item_name for f in plan.files}, set(PAYLOADS))

    def test_present_file_with_matching_md5_is_skipped(self):
        for name, payload in PAYLOADS.items():
            target = self.root / IDENT / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
        plan = self.build(FakeClient(self.files()), self.torrent())
        self.assertEqual(plan.to_fetch, 0)
        self.assertEqual(plan.to_fetch_bytes, 0)
        self.assertTrue(all(f.action == HAVE for f in plan.files))

    def test_present_file_with_wrong_content_is_refetched(self):
        target = self.root / IDENT / "alpha.txt"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"X" * len(PAYLOADS["alpha.txt"]))  # right size, wrong bytes
        plan = self.build(FakeClient(self.files()), self.torrent())
        alpha = next(f for f in plan.files if f.item_name == "alpha.txt")
        self.assertEqual(alpha.action, FETCH)
        self.assertIn("md5", alpha.note)

        # ...and skipping verification would wrongly trust it.
        plan = self.build(FakeClient(self.files()), self.torrent(), verify=False)
        alpha = next(f for f in plan.files if f.item_name == "alpha.txt")
        self.assertEqual(alpha.action, HAVE)

    def test_file_present_with_wrong_size_is_refetched(self):
        target = self.root / IDENT / "beta.txt"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"short")
        plan = self.build(FakeClient(self.files()), self.torrent())
        beta = next(f for f in plan.files if f.item_name == "beta.txt")
        self.assertEqual(beta.action, FETCH)


class TestRefusals(PlannerTestCase):
    def test_non_archive_torrent_is_rejected(self):
        torrent = Torrent(
            qbt_torrent(
                name="ubuntu.iso",
                comment="ubuntu",
                tracker="udp://tracker.example:80/announce",
                magnet_uri="",
            )
        )
        with self.assertRaises(NotArchiveTorrent):
            self.build(FakeClient(self.files()), torrent)

    def test_path_traversal_is_refused(self):
        files = [qbt_file("../../etc/evil", 10)]
        plan = self.build(FakeClient(files), self.torrent())
        self.assertEqual(plan.files[0].action, UNAVAILABLE)
        self.assertIn("unsafe path", plan.files[0].note)
        self.assertFalse(plan.revivable)

    def test_file_absent_from_the_item_is_unavailable(self):
        raw, manifest = self.__class__.make_item()
        partial = {k: v for k, v in manifest.items() if k != "beta.txt"}
        archive = FakeArchive(identifier=IDENT, torrent=parse_torrent(raw), item_files=partial)
        plan = build_plan(FakeClient(self.files()), archive, self.torrent())
        beta = next(f for f in plan.files if f.item_name == "beta.txt")
        self.assertEqual(beta.action, UNAVAILABLE)
        self.assertFalse(plan.revivable)
        self.assertTrue(any("not in the item's file list" in w for w in plan.warnings))

    def test_no_files_reported_is_an_error(self):
        with self.assertRaises(PlanError):
            self.build(FakeClient([]), self.torrent())


class TestStaleness(PlannerTestCase):
    def _use_item(self, raw, manifest):
        """Point the fake archive at a *different* item revision."""
        self.archive._torrent = parse_torrent(raw)
        self.archive._files = manifest

    def test_stale_torrent_is_detected_and_blocks_by_default(self):
        from tests.fakes import make_item

        # Same identifier, different file set, different info-hash.
        other_raw, other_manifest = make_item(
            {"alpha.txt": PAYLOADS["alpha.txt"], "gamma.txt": b"C" * 100}, identifier=IDENT
        )
        self._use_item(other_raw, other_manifest)
        plan = self.build(FakeClient(self.files()), self.torrent())
        self.assertTrue(plan.stale)
        self.assertTrue(any("STALE" in w for w in plan.warnings))
        self.assertTrue(any("--force" in w for w in plan.warnings))

    def test_force_allows_a_stale_torrent_with_a_warning(self):
        from tests.fakes import make_item

        other_raw, other_manifest = make_item(
            {"alpha.txt": PAYLOADS["alpha.txt"], "gamma.txt": b"C" * 100}, identifier=IDENT
        )
        self._use_item(other_raw, other_manifest)
        plan = self.build(FakeClient(self.files()), self.torrent(), force=True)
        self.assertTrue(plan.stale)
        self.assertFalse(any("--force" in w for w in plan.warnings))

    def test_identical_files_but_new_info_hash_only_warns(self):
        from tests.fakes import make_item

        # Same files and sizes, different non-file metadata (piece length) -> a new
        # info-hash. Filling is still safe, so this must not be treated as stale.
        other_raw, other_manifest = make_item(dict(PAYLOADS), identifier=IDENT, piece_length=32768)
        self._use_item(other_raw, other_manifest)
        torrent = self.torrent()
        self.assertNotEqual(parse_torrent(other_raw).info_hash, torrent.hash)
        plan = self.build(FakeClient(self.files()), torrent)
        self.assertFalse(plan.stale)
        self.assertTrue(any("file list and sizes match" in w for w in plan.warnings))


class TestNeedsHelp(unittest.TestCase):
    def test_incomplete_needs_help(self):
        self.assertTrue(needs_help(Torrent(qbt_torrent(progress=0.5, state="stalledDL"))))

    def test_complete_does_not(self):
        self.assertFalse(needs_help(Torrent(qbt_torrent(progress=1.0, state="uploading"))))

    def test_missing_files_state_needs_help_even_at_100_percent(self):
        self.assertTrue(needs_help(Torrent(qbt_torrent(progress=1.0, state="missingFiles"))))

    def test_torrent_without_metadata_needs_help(self):
        self.assertTrue(needs_help(Torrent(qbt_torrent(has_metadata=False, state="metaDL"))))

    def test_error_state_needs_help(self):
        self.assertTrue(needs_help(Torrent(qbt_torrent(progress=1.0, state="error"))))


class TestPlanSummary(unittest.TestCase):
    def test_memory_summary_counts_only_real_downloads(self):
        from ia_reviver.planner import PlannedFile, TorrentPlan
        from ia_reviver.qbittorrent import Torrent

        plan = TorrentPlan(
            torrent=Torrent(qbt_torrent()),
            identifier="x",
            root="x",
            files=[
                PlannedFile("x/a", Path("/tmp/a"), 100, "a", b"a", action=FETCH),
                PlannedFile("x/b", Path("/tmp/b"), 50, "b", b"b", action=HAVE),
            ],
        )
        self.assertEqual(plan.to_fetch, 1)
        self.assertEqual(plan.to_fetch_bytes, 100)
        self.assertIn("1/2 files", plan.memory_summary)

    def test_synthetic_encoder_round_trips_through_the_parser(self):
        # Guards the fixture machinery itself: the independent encoder's output
        # must decode to the same structure the parser expects.
        from ia_reviver.bencode import bdecode

        payload = bencode({"a": [1, b"two"], "b": {"c": b"d"}})
        _, decoded = bdecode(payload)
        self.assertEqual(decoded, {b"a": [1, b"two"], b"b": {b"c": b"d"}})
        # Keys must be emitted in sorted order, as the spec requires.
        self.assertTrue(payload.startswith(b"d1:a"))
        self.assertEqual(bencode({"b": 1, "a": 2}), b"d1:ai2e1:bi1ee")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
