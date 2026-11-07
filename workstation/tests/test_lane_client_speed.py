import unittest
import tempfile
from unittest.mock import patch
from datetime import datetime
from aiobs_collector import lane_client_speed as lane
from aiobs_collector import __main__ as main
from aiobs_collector.lane_usage import dump_ledger
from aiobs_collector.core import Sample
from aiobs_collector.lane_client_speed import day_entries, cumulative_samples, VALUE, TOKENS
from aiobs_collector.lane_usage import day_entries as usage_entries, VALUE as USAGE_VALUE

DAY = '2026-11-08'

class ClientSpeedTests(unittest.TestCase):
    def test_claude_fast_counts_once_and_prices_all_cache_categories(self):
        tokens = dict(input=1_000_000, output=1_000_000, cache_read=1_000_000,
                      cache_write_5m=1_000_000, cache_write_1h=1_000_000)
        claude = {(DAY, 'claude-opus-5-5', 'fast'): tokens}
        doc = {'contributions': []}
        entry = day_entries(doc, claude)[DAY]
        self.assertAlmostEqual(entry[(VALUE, 'claude-code', 'claude-opus-5-5', 'fast')], 74.4)
        self.assertEqual(entry[(TOKENS, 'claude-code', 'claude-opus-5-5', 'fast')], 5_000_000)
        self.assertEqual(entry[(VALUE, 'claude-code', 'claude-opus-5-5', 'fast')],
                         usage_entries(doc, claude, {})[DAY][(USAGE_VALUE, 'claude-code', 'claude-opus-5-5', '')])

    def test_droid_suffix_and_unknown_model_keep_authoritative_value(self):
        doc = {'contributions': [{'date': DAY, 'clients': [
            {'client':'droid','modelId':'claude-opus-5-5-fast','tokens':{'output':1_000_000},'cost':999},
            {'client':'droid','modelId':'unknown-fast','tokens':{'output':100},'cost':3},
            {'client':'codex','modelId':'gpt-6-astra','tokens':{'output':100},'cost':5}]}]}
        entry = day_entries(doc, {})[DAY]
        self.assertEqual(entry[(VALUE,'droid','claude-opus-5-5-fast','fast')],40)
        self.assertEqual(entry[(VALUE,'droid','unknown-fast','fast')],3)
        self.assertFalse(any(k[1]=='codex' for k in entry))

    def test_unknown_claude_price_splits_estimate_by_tokens_without_duplication(self):
        doc={'contributions':[{'date':DAY,'clients':[{'client':'claude','modelId':'future-model','tokens':{},'cost':12}]}]}
        claude={(DAY,'future-model','fast'):{'output':3},(DAY,'future-model','standard'):{'output':1}}
        entry=day_entries(doc,claude)[DAY]
        self.assertEqual(entry[(VALUE,'claude-code','future-model','fast')],9)
        self.assertEqual(entry[(VALUE,'claude-code','future-model','standard')],3)

    def test_cumulative_keeps_model_and_speed_distinct(self):
        days={'2026-11-06':{(VALUE,'droid','m-fast','fast'):2},
              '2026-11-07':{(VALUE,'droid','m-fast','fast'):3}}
        samples=cumulative_samples(days,1_794_182_400_000)
        self.assertEqual(samples[-1].value,5)
        self.assertEqual(samples[-1].labels['speed'],'fast')
        self.assertEqual(samples[-1].labels['model'],'m-fast')

    def test_history_filter(self):
        self.assertEqual(day_entries({}, {('2026-10-01','claude-opus-5-5','fast'):{'output':1}}, DAY),{})

    def test_frozen_days_are_not_revalued_and_recent_history_is_recomputed(self):
        now = datetime.fromisoformat('2026-11-08T12:00:00').timestamp()
        frozen = {'2026-11-06': {(VALUE,'claude-code','claude-opus-5-5','fast'):123}}
        state = {lane.STATE_KEY: dump_ledger('2026-11-06', frozen)}
        with tempfile.TemporaryDirectory() as root, patch.object(lane.time,'time',return_value=now), \
             patch.object(lane,'tokscale_graph',return_value={}), \
             patch.object(lane,'parse_claude_usage',return_value={}) as parse:
            instance=lane.ClientSpeedLane()
            samples=instance.collect({'AIOBS_CLAUDE_PROJECTS':root,'AIOBS_TOKSCALE_VERSION':'test'},state)
        self.assertEqual(parse.call_args.args[1],'2026-11-07')
        self.assertEqual(instance.state_data['days']['2026-11-06'][0][-1],123)
        self.assertEqual(samples[-1].value,123)

    def test_client_speed_routes_to_own_lane_and_repushed_yesterday(self):
        now=int(datetime.fromisoformat('2026-11-08T12:00:00').timestamp()*1000)
        yesterday=int(datetime.fromisoformat('2026-11-07T23:59:59').timestamp()*1000)
        sample=Sample(VALUE,{'provider':'claude-code','model':'m','speed':'fast'},2,yesterday)
        self.assertEqual(main._lane_for_sample(sample),'client-speed')
        state={'push:client-speed:max_ts_ms':now,'push:client-speed:max_ts_count':10}
        self.assertEqual(main.filter_for_push([sample],state,now,False),[sample])

    def test_droid_ultrafast_uses_verified_base_multiplier(self):
        doc={'contributions':[{'date':DAY,'clients':[
            {'client':'droid','modelId':'gpt-6-astra-ultrafast','tokens':{'output':1_000_000}}]}]}
        self.assertEqual(day_entries(doc,{})[DAY][(VALUE,'droid','gpt-6-astra-ultrafast','ultrafast')],300)
