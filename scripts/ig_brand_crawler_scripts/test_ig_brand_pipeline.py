import csv
import tempfile
import unittest
from pathlib import Path

from scripts.ig_brand_crawler_scripts.ig_brand_pipeline import (
    BRAND_FIELDS, crawl_profiles, discover_candidates, export_profiles,
    extract_mentions, load_attempts, read_csv, safe_output, write_csv,
)


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.source = Path(self.temp.name) / "source"
        self.source.mkdir()
        self.output = Path(self.temp.name) / "output"

    def source_csv(self, name, rows):
        write_csv(self.source / f"{name}-FullVideoInfo.csv",
                  ["id", "post_owner.username", "text"], rows)

    def test_mentions_and_email_boundaries(self):
        self.assertEqual(extract_mentions(
            "@Brand.tw、＠OTHER @brand.tw. x@y.com a.b+tag@host.tw "
            "@" + "x" * 31 + " @bad..id @a_b @one@two"),
            ["brand.tw", "other", "brand.tw", "a_b", "one"])

    def test_discovery_excludes_all_owners_and_preserves_reviews(self):
        self.source_csv("alice", [dict(id="1", **{
            "post_owner.username": "ALICE", "text": "@Bob @Brand @brand @renamed"})])
        self.source_csv("bob", [dict(id="2", **{
            "post_owner.username": "renamed", "text": "@brand @Alice"})])
        before = {p: p.read_bytes() for p in self.source.iterdir()}
        stats = discover_candidates(self.source, self.output)
        self.assertEqual(stats["candidates"], 1)
        candidate = read_csv(self.output / "brand_candidates.csv")[0]
        self.assertEqual((candidate["mention_count"], candidate["post_count"],
                          candidate["influencer_count"]), ("3", "2", "2"))
        write_csv(self.output / "brand_review.csv", ["brand_id", "decision", "notes"],
                  [dict(brand_id="brand", decision="brand", notes="checked")])
        discover_candidates(self.source, self.output)
        self.assertEqual(read_csv(self.output / "brand_review.csv")[0]["notes"], "checked")
        self.assertEqual(before, {p: p.read_bytes() for p in self.source.iterdir()})

    def test_database_output_guard(self):
        for output in [self.source, self.source / "child", self.source.parent]:
            with self.assertRaises(ValueError):
                safe_output(self.source, output)

    def test_resume_stop_retry_and_brand_export(self):
        self.output.mkdir()
        write_csv(self.output / "brand_review.csv", ["brand_id", "decision", "notes"],
                  [dict(brand_id="brand", decision="brand", notes="checked")])
        calls = []

        def fetcher(loader, username):
            calls.append(username)
            if username == "blocked":
                raise RuntimeError("429 rate limited")
            return dict(brand_id=username, status="partial", brand_name="Brand",
                        number_of_followers=0, missing_fields={"business_type_of_the_brand": "not_provided"})

        kwargs = dict(source_dir=self.source, output_dir=self.output,
                      min_delay=0, max_delay=0, fetcher=fetcher)
        first = crawl_profiles(["brand", "brand", "blocked", "later"], None, **kwargs)
        self.assertEqual(len(first), 2)
        self.assertEqual(calls, ["brand", "blocked"])
        crawl_profiles(["brand", "blocked"], None, **kwargs)
        self.assertEqual(calls, ["brand", "blocked"])
        rows = read_csv(self.output / "brands.csv")
        self.assertEqual(set(rows[0]), set(BRAND_FIELDS))
        self.assertEqual(rows[0]["number_of_followers"], "0")
        self.assertEqual(rows[0]["business_type_of_the_brand"], "")
        # A failed refresh must preserve the older usable profile.
        def failure(*args):
            raise RuntimeError("network down")
        crawl_profiles(["brand"], None, self.source, self.output,
                       retry_failed=True, min_delay=0, max_delay=0, fetcher=failure)
        self.assertEqual(read_csv(self.output / "brands.csv")[0]["brand_name"], "Brand")
        self.assertEqual(len(load_attempts(self.output / "profile_attempts.jsonl")), 3)


if __name__ == "__main__":
    unittest.main()
