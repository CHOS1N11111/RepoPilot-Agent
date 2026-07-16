import unittest

from src.repo_sync import sync_remote_branch


class RepoSyncTests(unittest.TestCase):
    def test_dirty_repository_only_fetches(self) -> None:
        self.assertEqual(sync_remote_branch("origin", "feature", clean=False), "fetch-only")

    def test_clean_repository_fast_forwards(self) -> None:
        result = sync_remote_branch("origin", "feature", clean=True)
        self.assertIn("fast-forward", result)


if __name__ == "__main__":
    unittest.main()
