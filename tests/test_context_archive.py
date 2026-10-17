"""Tests for notiontool/context_archive.py (Phase 5b batch 1, Part C)."""
import ast
import copy
import hashlib
import json
import math
import os
import pathlib
import sqlite3
import unittest
from unittest import mock

from notiontool import context_archive as ca

NOW = 1800000000.0
DAY = 86400
SLIM_KEYS = {'id', 'summary', 'content_sha256', 'estimated_tokens', 'updated',
             'retired_at', 'slimmed', 'slim_reason'}


def canon(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def sha(value):
    return hashlib.sha256(canon(value).encode('utf-8')).hexdigest()


def record(ident='thread-a', **extra):
    base = dict(
        id=ident,
        observations=3,
        summary='Kept summary for ' + ident,
        entities={'e1': dict(id='e1', sequence=1, text='hello'),
                  'e2': dict(id='e2', sequence=2, text='world')},
        latest=dict(session=dict(sequence=2), messages=['hi']),
        native_thread='thread-old',
        native_messages=[dict(role='user', text='old message')],
        native_tokens=12,
        updated=NOW - 10,
        estimated_tokens=42,
    )
    base.update(extra)
    return base


def padded(size):
    rec = record('thread-big')
    rec['latest'] = dict(pad='')
    rec['latest']['pad'] = 'x' * (size - ca.record_size(rec))
    assert ca.record_size(rec) == size
    return rec


class SlimRetiredTests(unittest.TestCase):
    def test_slim_keeps_hash_and_summary(self):
        rec = record(retired_at=NOW - DAY - 1)
        out = ca.slim_record(rec, NOW)
        self.assertTrue(out['slimmed'])
        self.assertEqual(out['slim_reason'], 'retired')
        self.assertEqual(out['summary'], 'Kept summary for thread-a')
        self.assertEqual(out['content_sha256'], sha(rec))
        self.assertEqual(out['id'], 'thread-a')
        self.assertEqual(out['estimated_tokens'], 42)
        self.assertEqual(out['updated'], NOW - 10)

    def test_slim_drops_heavy_fields(self):
        out = ca.slim_record(record(retired_at=NOW - 2 * DAY), NOW)
        self.assertEqual(set(out), SLIM_KEYS)
        for field in ('entities', 'latest', 'native_messages', 'native_thread'):
            self.assertNotIn(field, out)

    def test_retired_exactly_keep_s_is_kept(self):
        rec = record(retired_at=NOW - DAY)
        out = ca.slim_record(rec, NOW)
        self.assertFalse(out['slimmed'])
        out.pop('slimmed')
        self.assertEqual(out, rec)

    def test_retired_keep_s_plus_one_is_slimmed(self):
        out = ca.slim_record(record(retired_at=NOW - DAY - 1), NOW)
        self.assertTrue(out['slimmed'])

    def test_custom_keep_s(self):
        rec = record(retired_at=NOW - 61)
        self.assertTrue(ca.slim_record(rec, NOW, keep_s=60)['slimmed'])
        self.assertFalse(ca.slim_record(rec, NOW, keep_s=3600)['slimmed'])

    def test_non_numeric_retired_at_is_ignored(self):
        for value in (None, str(NOW - 10 * DAY), True):
            out = ca.slim_record(record(retired_at=value), NOW)
            self.assertFalse(out['slimmed'], value)

    def test_missing_estimated_tokens_falls_back(self):
        rec = record(retired_at=NOW - 2 * DAY)
        del rec['estimated_tokens']
        out = ca.slim_record(rec, NOW)
        self.assertEqual(out['estimated_tokens'], math.ceil(len(canon(rec).encode('utf-8')) / 3))
        self.assertGreater(out['estimated_tokens'], 0)


class LargeRecordTests(unittest.TestCase):
    def test_limit_is_two_mib(self):
        self.assertEqual(ca.SLIM_MAX_BYTES, 2 * 1024 * 1024)

    def test_over_two_mb_not_retired_is_slimmed(self):
        rec = padded(ca.SLIM_MAX_BYTES + 1)
        self.assertNotIn('retired_at', rec)
        out = ca.slim_record(rec, NOW)
        self.assertTrue(out['slimmed'])
        self.assertEqual(out['slim_reason'], 'too_large')
        self.assertEqual(out['content_sha256'], sha(rec))
        self.assertEqual(out['summary'], 'Kept summary for thread-big')
        self.assertNotIn('latest', out)

    def test_exactly_two_mb_is_kept(self):
        out = ca.slim_record(padded(ca.SLIM_MAX_BYTES), NOW)
        self.assertFalse(out['slimmed'])


class NoMutationTests(unittest.TestCase):
    def test_input_not_mutated_on_slim_and_keep(self):
        for rec in (record(retired_at=NOW - 2 * DAY), record()):
            before = copy.deepcopy(rec)
            out = ca.slim_record(rec, NOW)
            self.assertEqual(rec, before)
            if 'entities' in out:
                out['entities']['e1']['text'] = 'changed'
            self.assertEqual(rec, before)

    def test_already_slimmed_stays_as_is(self):
        slim = ca.slim_record(record(retired_at=NOW - 2 * DAY), NOW)
        again = ca.slim_record(slim, NOW + 10 * DAY)
        self.assertEqual(again, slim)
        self.assertIsNot(again, slim)


def entities(count):
    return {'e%d' % i: dict(id='e%d' % i, sequence=i, text='x' * 300) for i in range(1, count + 1)}


class MainUsageTests(unittest.TestCase):
    def test_counts_only_after_compaction(self):
        ents = entities(1000)
        full = ca.main_usage(ents, 0, 1)
        after = ca.main_usage(ents, 999, 1)
        self.assertEqual(after['estimated_tokens'], ca.estimate_tokens({'e1000': ents['e1000']}))
        self.assertEqual(after['turns'], 1)
        self.assertGreater(full['estimated_tokens'], 100 * after['estimated_tokens'])

    def test_resets_to_zero_after_compaction(self):
        self.assertEqual(ca.main_usage(entities(1000), 1000, 1), {'estimated_tokens': 0, 'turns': 1})

    def test_missing_or_invalid_sequence_not_counted(self):
        ents = {'a': {'text': 'no sequence'}, 'b': {'sequence': '5'}, 'c': {'sequence': True},
                'd': 'not a dict', 'e': {'seq': 7, 'text': 'fallback'}}
        out = ca.main_usage(ents, 0, 3)
        self.assertEqual(out['estimated_tokens'], ca.estimate_tokens({'e': ents['e']}))
        self.assertEqual(out['turns'], 3)

    def test_sequence_preferred_over_seq(self):
        out = ca.main_usage({'x': {'sequence': 1, 'seq': 9}}, 5, 2)
        self.assertEqual(out, {'estimated_tokens': 0, 'turns': 2})

    def test_empty_and_none(self):
        self.assertEqual(ca.main_usage({}, 0, 0), {'estimated_tokens': 0, 'turns': 0})
        self.assertEqual(ca.main_usage(None, 0, 0), {'estimated_tokens': 0, 'turns': 0})


class EstimateTests(unittest.TestCase):
    def test_estimate_tokens_counts_utf8_bytes(self):
        self.assertEqual(ca.estimate_tokens(''), 0)
        self.assertEqual(ca.estimate_tokens('abc'), 1)
        self.assertEqual(ca.estimate_tokens('abcd'), 2)
        self.assertEqual(ca.estimate_tokens('\u00e4'), 1)
        self.assertEqual(ca.estimate_tokens('\u00e4\u00e4'), 2)
        self.assertEqual(ca.estimate_tokens('\u20ac\u20ac'), 2)


class PrunePlanTests(unittest.TestCase):
    def test_dry_run_has_no_side_effects(self):
        recs = [record('t-c'), record('t-a', retired_at=NOW - 2 * DAY), record('t-b', retired_at=NOW - DAY)]
        before = copy.deepcopy(recs)
        fail = AssertionError('side effect')
        with mock.patch('builtins.open', side_effect=fail), \
                mock.patch('sqlite3.connect', side_effect=fail), \
                mock.patch('os.replace', side_effect=fail), \
                mock.patch('os.remove', side_effect=fail):
            first = ca.prune_plan(recs, NOW)
            second = ca.prune_plan(recs, NOW)
        self.assertEqual(first, [('t-a', 'slim'), ('t-b', 'keep'), ('t-c', 'keep')])
        self.assertEqual(first, second)
        self.assertEqual(recs, before)

    def test_invalid_records_and_dict_input(self):
        recs = {'x1': record('x1'), 'x2': 'broken', 'x3': {'summary': 'no id'}}
        self.assertEqual(ca.prune_plan(recs, NOW),
                         [('x1', 'keep'), ('x2', 'skip_invalid'), ('x3', 'skip_invalid')])
        self.assertEqual(ca.prune_plan([None, record('y')], NOW), [('', 'skip_invalid'), ('y', 'keep')])

    def test_already_slimmed_is_keep(self):
        slim = ca.slim_record(record('z', retired_at=NOW - 2 * DAY), NOW)
        self.assertEqual(ca.prune_plan([slim], NOW + DAY), [('z', 'keep')])


class ModuleSafetyTests(unittest.TestCase):
    def test_only_stdlib_imports(self):
        tree = ast.parse(pathlib.Path(ca.__file__).read_text(encoding='utf-8'))
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(alias.name.split('.')[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                names.add((node.module or '').split('.')[0])
        self.assertLessEqual(names, {'copy', 'hashlib', 'json', 'math'})


if __name__ == '__main__':
    unittest.main()
