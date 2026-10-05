"""Unit tests for rsync_client.py. Uses mocked subprocess.run so no real
rsync binary is required to run these tests."""

import subprocess
import unittest
from unittest.mock import patch, MagicMock

import rsync_client
from rsync_client import RsyncError


def _completed(stdout="", stderr="", returncode=0):
    return MagicMock(stdout=stdout, stderr=stderr, returncode=returncode)


class TestBuildExcludeArgs(unittest.TestCase):
    def test_builds_one_flag_per_pattern(self):
        args = rsync_client.build_exclude_args(["*.tmp", "node_modules/"])
        self.assertEqual(args, ["--exclude=*.tmp", "--exclude=node_modules/"])

    def test_empty_or_none_patterns(self):
        self.assertEqual(rsync_client.build_exclude_args(None), [])
        self.assertEqual(rsync_client.build_exclude_args([]), [])


class TestBuildBaseArgs(unittest.TestCase):
    def test_defaults_exclude_owner_and_group(self):
        args = rsync_client.build_base_args()
        self.assertNotIn("--owner", args)
        self.assertNotIn("--group", args)
        self.assertIn("--numeric-ids", args)
        self.assertIn("--whole-file", args)
        self.assertIn("--delete", args)
        self.assertIn("--delete-excluded", args)
        self.assertIn("--timeout=300", args)

    def test_owner_group_permissions_toggle(self):
        args = rsync_client.build_base_args(
            preserve_owner=True, preserve_group=True, preserve_permissions=False,
        )
        self.assertIn("--owner", args)
        self.assertIn("--group", args)
        self.assertNotIn("--perms", args)

    def test_no_delete_omits_delete_flags(self):
        args = rsync_client.build_base_args(delete=False)
        self.assertNotIn("--delete", args)
        self.assertNotIn("--delete-excluded", args)

    def test_no_whole_file(self):
        args = rsync_client.build_base_args(whole_file=False)
        self.assertIn("--no-whole-file", args)
        self.assertNotIn("--whole-file", args)

    def test_extra_args_appended(self):
        args = rsync_client.build_base_args(extra_args=["--bwlimit=1000"])
        self.assertIn("--bwlimit=1000", args)


class TestRun(unittest.TestCase):
    @patch("rsync_client.subprocess.run")
    def test_raises_on_binary_missing(self, mock_run):
        mock_run.side_effect = FileNotFoundError()
        with self.assertRaises(RsyncError):
            rsync_client._run(["--version"])

    @patch("rsync_client.subprocess.run")
    def test_raises_on_timeout(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd=["rsync"], timeout=5)
        with self.assertRaises(RsyncError):
            rsync_client._run(["--version"], timeout=5)

    @patch("rsync_client.subprocess.run")
    def test_raises_on_fatal_exit_code(self, mock_run):
        mock_run.return_value = _completed(stderr="some fatal error", returncode=1)
        with self.assertRaises(RsyncError) as ctx:
            rsync_client._run(["--version"])
        self.assertIn("some fatal error", str(ctx.exception))

    @patch("rsync_client.subprocess.run")
    def test_partial_transfer_codes_do_not_raise(self, mock_run):
        for code in rsync_client.PARTIAL_TRANSFER_CODES:
            mock_run.return_value = _completed(returncode=code)
            result = rsync_client._run(["--version"])
            self.assertEqual(result.returncode, code)


class TestParseStats(unittest.TestCase):
    def test_parses_files_and_bytes(self):
        stdout = (
            "Number of regular files transferred: 3\n"
            "Total transferred file size: 1,234 bytes\n"
        )
        files, total_bytes = rsync_client._parse_stats(stdout)
        self.assertEqual(files, 3)
        self.assertEqual(total_bytes, 1234)

    def test_parses_human_readable_unit_suffix(self):
        stdout = (
            "Number of regular files transferred: 5\n"
            "Total transferred file size: 10.49M bytes\n"
        )
        files, total_bytes = rsync_client._parse_stats(stdout)
        self.assertEqual(files, 5)
        self.assertEqual(total_bytes, 10490000)

    def test_missing_stats_defaults_to_zero(self):
        files, total_bytes = rsync_client._parse_stats("no stats here")
        self.assertEqual(files, 0)
        self.assertEqual(total_bytes, 0)


class TestSync(unittest.TestCase):
    @patch("rsync_client.subprocess.run")
    def test_returns_result_dict(self, mock_run):
        stdout = (
            "Number of regular files transferred: 2\n"
            "Total transferred file size: 100 bytes\n"
        )
        mock_run.return_value = _completed(stdout=stdout, returncode=0)
        result = rsync_client.sync("/src", "/dst")
        self.assertFalse(result["partial"])
        self.assertEqual(result["files_transferred"], 2)
        self.assertEqual(result["bytes_transferred"], 100)
        self.assertEqual(result["returncode"], 0)

    @patch("rsync_client.subprocess.run")
    def test_partial_flag_set_on_23_24(self, mock_run):
        mock_run.return_value = _completed(returncode=23)
        result = rsync_client.sync("/src", "/dst")
        self.assertTrue(result["partial"])

    @patch("rsync_client.subprocess.run")
    def test_appends_trailing_slashes(self, mock_run):
        mock_run.return_value = _completed(returncode=0)
        rsync_client.sync("/src/no/slash", "/dst/no/slash")
        args = mock_run.call_args.args[0]
        self.assertIn("/src/no/slash/", args)
        self.assertIn("/dst/no/slash/", args)


class TestCountDiffs(unittest.TestCase):
    @patch("rsync_client.subprocess.run")
    def test_counts_itemize_lines_only(self, mock_run):
        stdout = (
            ">f.st...... a.txt\n"
            "cd+++++++++ subdir/\n"
            "\n"
            "some non-matching stray line\n"
        )
        mock_run.return_value = _completed(stdout=stdout, returncode=0)
        count = rsync_client.count_diffs("/src", "/dst")
        self.assertEqual(count, 2)

    @patch("rsync_client.subprocess.run")
    def test_zero_when_no_diffs(self, mock_run):
        mock_run.return_value = _completed(stdout="", returncode=0)
        count = rsync_client.count_diffs("/src", "/dst")
        self.assertEqual(count, 0)

    @patch("rsync_client.subprocess.run")
    def test_checksum_flag_passed_through(self, mock_run):
        mock_run.return_value = _completed(stdout="", returncode=0)
        rsync_client.count_diffs("/src", "/dst", checksum=True)
        args = mock_run.call_args.args[0]
        self.assertIn("--checksum", args)


if __name__ == "__main__":
    unittest.main()
