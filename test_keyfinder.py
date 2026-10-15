import json
import plistlib
import unittest

import keyfinder as kf


class FakeStore:
    name = "fake"
    supports_difficulty = True

    def __init__(self, suggestions, apps=None):
        self.suggestions = suggestions
        self.apps = apps or {}
        self.calls = []

    def suggest(self, term):
        self.calls.append(term)
        return self.suggestions.get(term, [])

    def top_apps(self, term, limit=10):
        return self.apps.get(term, [])


class ParserTests(unittest.TestCase):
    def test_apple_hints_plist(self):
        raw = plistlib.dumps({"hints": [{"term": "meditation"}, {"term": "meditation app"}]})
        self.assertEqual(kf.parse_apple_hints(raw), ["meditation", "meditation app"])

    def test_apple_hints_json(self):
        raw = json.dumps({"hints": [{"term": "calm"}, {"priority": 1}]}).encode()
        self.assertEqual(kf.parse_apple_hints(raw), ["calm"])

    def test_play_suggest(self):
        inner = json.dumps([[[["meditation", None], ["meditation app", None]]]])
        raw = ")]}'\n\n" + json.dumps([["wrb.fr", "IJ4APc", inner, None], ["di", 42]])
        self.assertEqual(kf.parse_play_suggest(raw.encode()), ["meditation", "meditation app"])

    def test_play_suggest_empty(self):
        raw = ")]}'\n\n" + json.dumps([["wrb.fr", "IJ4APc", None, None]])
        self.assertEqual(kf.parse_play_suggest(raw), [])
        raw = ")]}'\n\n" + json.dumps([["wrb.fr", "IJ4APc", "[[null]]", None]])
        self.assertEqual(kf.parse_play_suggest(raw), [])

    def test_itunes_search(self):
        raw = json.dumps({"results": [{"trackName": "Calm", "artistName": "Calm.com",
                                       "userRatingCount": 1500000, "averageUserRating": 4.8}]})
        self.assertEqual(kf.parse_itunes_search(raw),
                         [{"title": "Calm", "developer": "Calm.com", "ratings": 1500000, "rating": 4.8}])


class ScoringTests(unittest.TestCase):
    def test_popularity_prefers_short_prefix_and_top_position(self):
        early = kf.popularity_score("meditation", 2, 0)
        late = kf.popularity_score("meditation", 8, 0)
        low_pos = kf.popularity_score("meditation", 2, 7)
        self.assertGreater(early, late)
        self.assertGreater(early, low_pos)
        self.assertEqual(kf.popularity_score("x", 1, 0), 100)
        self.assertEqual(kf.popularity_score("anything", None, 0), 5)

    def test_difficulty(self):
        strong = [{"title": "Meditation Pro", "ratings": 1_000_000}] * 10
        weak = [{"title": "Timer", "ratings": 3}] * 10
        self.assertEqual(kf.difficulty_score("meditation", strong), 100)
        self.assertLess(kf.difficulty_score("meditation", weak), 15)
        self.assertEqual(kf.difficulty_score("meditation", []), 0)


class ResearchTests(unittest.TestCase):
    def setUp(self):
        self.store = FakeStore(
            suggestions={
                "m": ["music", "messenger"],
                "me": ["messenger", "meditation"],
                "meditation": ["meditation", "meditation app", "meditation music"],
                "meditation a": ["meditation app", "meditation app free"],
                "meditation m": ["meditation music"],
            },
            apps={
                "meditation": [{"title": "Calm - Meditation", "ratings": 1_500_000}] * 10,
                "meditation app free": [{"title": "Zen Timer", "ratings": 40}] * 10,
            },
        )

    def test_research_expands_probes_and_scores(self):
        r = kf.Researcher(self.store)
        results = {k.term: k for k in r.research("  Meditation ")}
        self.assertEqual(set(results),
                         {"meditation", "meditation app", "meditation music", "meditation app free"})
        # "meditation" surfaced after typing "me" at position 2 (index 1)
        self.assertEqual((results["meditation"].min_prefix, results["meditation"].position), (2, 1))
        # suggested only from the seed and its expansions
        self.assertEqual(results["meditation app"].min_prefix, len("meditation"))
        self.assertEqual(results["meditation app free"].min_prefix, len("meditation a"))
        # unrelated terms seen while probing are not added
        self.assertNotIn("music", results)
        # easy long-tail beats the dominated head term on opportunity
        self.assertGreater(results["meditation app free"].opportunity, results["meditation"].opportunity)
        # each query is made once
        self.assertEqual(len(self.store.calls), len(set(self.store.calls)))

    def test_limit_and_no_probe(self):
        r = kf.Researcher(self.store, probe=False, difficulty=False, limit=2)
        results = r.research("meditation")
        self.assertLessEqual(len(results), 3)  # limit + the seed itself
        self.assertNotIn("m", self.store.calls)
        self.assertTrue(all(k.difficulty is None for k in results))

    def test_play_store_has_no_difficulty(self):
        play = kf.GooglePlay(fetcher=None)
        self.assertFalse(kf.Researcher(play).difficulty)


if __name__ == "__main__":
    unittest.main()
