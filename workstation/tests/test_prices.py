import unittest
from unittest.mock import patch

from aiobs_collector import prices
from aiobs_collector.prices import Rate, normalize_model, rate_for, value_usd

DAY = "2026-10-05"


class RateTableTests(unittest.TestCase):
    def test_anthropic_rates_follow_the_official_page(self):
        opus = rate_for("claude-opus-5-5", DAY)
        self.assertEqual((opus.input, opus.output), (4, 20))
        self.assertAlmostEqual(opus.cache_read, 0.20)  # 0.05x on Opus 5.5
        self.assertAlmostEqual(opus.cache_write_5m, 5.0)  # 1.25x
        self.assertAlmostEqual(opus.cache_write_1h, 8.0)  # 2x
        self.assertAlmostEqual(rate_for("claude-fable-5-1", DAY).cache_read, 0.25)  # 0.025x
        self.assertAlmostEqual(rate_for("claude-fable-5", DAY).cache_read, 1.0)  # 0.1x
        sonnet = rate_for("claude-sonnet-4-5", DAY)
        self.assertEqual((sonnet.input, sonnet.output), (3, 15))

    def test_openai_rates_follow_the_official_page(self):
        astra = rate_for("gpt-6-astra", DAY)
        self.assertEqual((astra.input, astra.cache_read, astra.cache_write_5m, astra.output), (10, 1, 12.5, 50))
        sol = rate_for("gpt-6.1-sol", DAY)
        self.assertEqual((sol.input, sol.cache_read, sol.output), (2, 0.10, 10))

    def test_rates_added_2026_10_15_follow_the_official_pages(self):
        # OpenAI's gpt-5.6-luna model page and Kimi API pricing, fetched 2026-10-15.
        luna = rate_for("gpt-5.6-luna", DAY)
        self.assertEqual((luna.input, luna.cache_read, luna.cache_write_5m, luna.output), (0.20, 0.02, 0.25, 1.20))
        self.assertEqual(luna.multiplier("fast"), 1.0)  # no Fast price is listed for GPT-5.6 Luna
        kimi = rate_for("kimi-k3", DAY)
        self.assertEqual((kimi.input, kimi.cache_read, kimi.cache_write_5m, kimi.cache_write_1h, kimi.output),
                         (3, 0.30, 3, 6, 15))

    def test_luna_rate_reproduces_the_estimate_it_replaces(self):
        # tokscale's third-party estimate for Codex gpt-5.6-luna on 2026-10-14 was $9.03.
        tokens = {"input": 10_243_823, "cache_read": 289_149_184, "output": 998_780}
        self.assertAlmostEqual(value_usd(rate_for("gpt-5.6-luna", DAY), tokens), 9.03, places=2)

    def test_value_reproduces_a_known_day(self):
        # tokscale's own figure for Opus 5.5 on 2026-10-05, all writes as 5-minute ones: $233.2391006
        opus = rate_for("claude-opus-5-5", DAY)
        tokens = {"input": 5334, "output": 2_147_860, "cache_read": 658_961_898, "cache_write_5m": 11_693_637}
        self.assertAlmostEqual(value_usd(opus, tokens), 233.2391006, places=5)

    def test_one_hour_cache_writes_cost_twice_the_input_rate(self):
        opus = rate_for("claude-opus-5-5", DAY)
        self.assertAlmostEqual(value_usd(opus, {"cache_write_1h": 1_000_000}), 8.0)
        self.assertAlmostEqual(value_usd(opus, {"cache_write_5m": 1_000_000}), 5.0)

    def test_speed_multipliers(self):
        astra = rate_for("gpt-6-astra", DAY)
        million_out = {"output": 1_000_000}
        self.assertAlmostEqual(value_usd(astra, million_out), 50.0)
        self.assertAlmostEqual(value_usd(astra, million_out, "fast"), 100.0)
        self.assertAlmostEqual(value_usd(astra, million_out, "ultrafast"), 300.0)
        self.assertAlmostEqual(value_usd(astra, million_out, "unknown"), 50.0)
        self.assertAlmostEqual(value_usd(rate_for("claude-opus-5-5", DAY), million_out, "fast"), 40.0)
        # A tier the model has no price for is billed at standard.
        self.assertAlmostEqual(value_usd(rate_for("gpt-6.1-sol", DAY), million_out, "ultrafast"), 10.0)

    def test_normalize_model(self):
        cases = {
            "gpt-5-6-sol": ("gpt-5.6-sol", "standard"),
            "gpt-6-1-sol": ("gpt-6.1-sol", "standard"),
            "gpt-6-astra": ("gpt-6-astra", "standard"),
            "gpt-5.6-sol": ("gpt-5.6-sol", "standard"),
            "claude-opus-5-5-fast": ("claude-opus-5-5", "fast"),
            "anthropic/claude-sonnet-4-5-20250929": ("claude-sonnet-4-5", "standard"),
            "openai/gpt-6-astra": ("gpt-6-astra", "standard"),
        }
        for raw, expected in cases.items():
            self.assertEqual(normalize_model(raw), expected, raw)

    def test_models_without_an_official_rate_return_none(self):
        # DeepSeek prices by time of day and gpt-5.5 by request size past 272K; daily aggregates resolve neither.
        for model in ("deepseek-v4-1-flash", "gpt-5.5", "glm-5-2", "claude-opus-4-6", "codex-auto-review", "grok-4.6"):
            self.assertIsNone(rate_for(model, DAY), model)

    def test_opus_4_8_and_4_7_follow_the_official_page(self):
        # $5 / $25, cache read 0.1x; fast mode $10 / $50 on 4.8 only (fetched 2026-10-05)
        for model in ("claude-opus-4-8", "claude-opus-4-7"):
            rate = rate_for(model, DAY)
            self.assertEqual((rate.input, rate.output), (5, 25), model)
            self.assertAlmostEqual(rate.cache_read, 0.5)
        self.assertEqual(rate_for("claude-opus-4-8", DAY).multiplier("fast"), 2.0)
        self.assertEqual(rate_for("claude-opus-4-7", DAY).multiplier("fast"), 1.0)

    def test_since_picks_the_entry_in_force_that_day(self):
        table = {"m": (Rate(1, 1, 1, 1, 1, since="2026-01-01"), Rate(2, 2, 2, 2, 2, since="2026-10-01"))}
        with patch.dict(prices.TABLE, table):
            self.assertIsNone(rate_for("m", "2025-12-31"))
            self.assertEqual(rate_for("m", "2026-09-30").input, 1)
            self.assertEqual(rate_for("m", "2026-10-01").input, 2)


if __name__ == "__main__":
    unittest.main()
