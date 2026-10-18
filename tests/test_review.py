"""Local-only regressions. Start tools/db-runtime/server.mjs first.

No live model, no remote database, and no user data are used.
"""
import json
import os
import unittest
from unittest.mock import patch
from datetime import datetime, timezone

os.environ['DATABASE_URL'] = 'host=127.0.0.1 port=55439 dbname=shiguang_test user=postgres sslmode=disable'
os.environ['SHIGUANG_PASSWORD'] = 'local-test-only'
os.environ['ZHIPU_API_KEY'] = 'fake-local-only'
os.environ['EMBED_DIM'] = '4'

from fastapi.testclient import TestClient
from app import main, db, llm, parser, dates
from test_database_guard import configure_test_database


def result(**changes):
    data = dict(polished='测试正文', summary={'theme': '测试', 'points': []},
                advice=[], memories=[], events=[], profile_delta='', evidence='低')
    data.update(changes)
    return json.dumps(data, ensure_ascii=False)


class ParseTests(unittest.TestCase):
    def test_trailing_comma_repair_does_not_edit_quoted_content(self):
        value = parser._loads_repair('{"polished":"原文里有 ,} 和 ,]",}')
        self.assertEqual(value['polished'], '原文里有 ,} 和 ,]')

    def test_legacy_polish_cannot_add_memory(self):
        p = self.parse('【1. 整理】\n正文\n【4. 记忆】\n- 偏好测试', '/只润色')
        self.assertEqual(p['memories'], [])

    def parse(self, text, mode=''):
        return parser.parse_json_result(text, ' 原文\n仍在 ', '2026-10-18', mode)

    def test_embedded_quotes_remain_complete(self):
        value = '决定购买：自评"需要"，但还没购买。'
        p = self.parse(result(memories=[dict(text=value, category='当前目标')]))
        self.assertEqual(p['memories'][0]['text'], value)

    def test_nested_summary_never_becomes_response(self):
        p = self.parse('{"polished":"正文有"裸引号"。","summary":{"theme":"主题"},"memories":[]}')
        self.assertIn('裸引号', p['polished'])
        self.assertEqual(p['format'], 'recovered')
        self.assertTrue(p['warning'])

    def test_recovered_response_never_creates_memories(self):
        p = self.parse('{"polished":"正文\n裸换行","summary":{},"memories":[{"text":"不可靠候选"}]}')
        self.assertEqual(p['memories'], [])

    def test_truncated_json_falls_back_to_raw(self):
        p = self.parse('{"polished":"没有闭合')
        self.assertEqual(p['polished'], ' 原文\n仍在 ')
        self.assertEqual(p['format'], 'raw')

    def test_bad_field_types_do_not_crash(self):
        p = self.parse(result(summary=[], memories=[None, 42], advice=[42, None], events=[None]))
        self.assertEqual((p['memories'], p['advice'], p['events']), ([], [], []))

    def test_confidence_is_finite_and_bounded(self):
        p = self.parse(result(memories=[{'text': '一条有效记忆', 'confidence': 9}, {'text': '另一条有效记忆', 'confidence': 'nan'}]))
        self.assertEqual(p['memories'][0]['confidence'], 1.0)
        self.assertIsNone(p['memories'][1]['confidence'])

    def test_polish_mode_cannot_write_memory_or_events(self):
        p = self.parse(result(memories=[{'text': '不应入库'}], events=[{'title': '不应入库'}], growth='不应输出'), '/只润色')
        self.assertEqual((p['memories'], p['events'], p['growth']), ([], [], ''))

    def test_event_date_preserved_and_invalid_date_falls_back(self):
        p = self.parse(result(events=[{'title': '补记', 'date': '2026-10-06'}, {'title': '未知', 'date': '2026-02-30'}]))
        self.assertEqual([e['date'] for e in p['events']], ['2026-10-06', '2026-10-18'])

    def test_rolling_window_is_exactly_seven_dates(self):
        self.assertEqual(dates.period_window('/周总结', '2026-10-18'), ('2026-10-12', '2026-10-18'))

    def test_month_window_across_year(self):
        self.assertEqual(dates.period_window('/月总结', '2026-01-10'), ('2025-12-12', '2026-01-10'))

    def test_full_period_tail_and_requested_bounds_reach_model(self):
        text = '前文' * 400 + '末尾关键决定'
        prompt = llm.build_user_prompt({}, [], [], [], '', '2026-10-18', '/周总结',
            period_entries=[{'date': '2026-10-14', 'text': text}], period_range=('2026-10-12', '2026-10-18'))
        self.assertIn('末尾关键决定', prompt)
        self.assertIn('2026-10-12 至 2026-10-18', prompt)

    def test_period_overflow_fails_instead_of_silent_truncation(self):
        with self.assertRaises(llm.LLMError):
            llm.build_user_prompt({}, [], [], [], '', '2026-10-18', '/周总结', [{'date': '2026-10-18', 'text': '字'*96001}])

    def test_polish_prompt_does_not_include_profile(self):
        prompt = llm.build_user_prompt({'长期背景': ['私人背景']}, [], [], [], '我的原文', '2026-10-18', '/只润色')
        self.assertNotIn('私人背景', prompt)
        self.assertIn('我的原文', prompt)

    def test_legacy_test_guard_rejects_remote(self):
        with patch.dict(os.environ, {'SHIGUANG_TEST_DATABASE_URL': 'host=example.invalid dbname=shiguang_test'}):
            with self.assertRaises(RuntimeError): configure_test_database()


class DatabaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with db.db(): pass
        cls.client = TestClient(main.app, base_url='https://testserver')
        assert cls.client.post('/api/login', json={'password': 'local-test-only'}).status_code == 200

    def setUp(self):
        self.sql('TRUNCATE pending_memories,events,entries,archive,todos,goals,jobs,embeddings RESTART IDENTITY CASCADE')
        self.model = patch.object(llm, 'call_glm', return_value=result()).start()
        patch.object(llm, 'embed_texts', side_effect=lambda texts: [[0.1]*4 for _ in texts]).start()
        self.addCleanup(patch.stopall)

    @staticmethod
    def sql(query, args=()):
        with db.db() as conn:
            with conn.cursor() as cur:
                cur.execute(query, args)
                return cur.fetchall() if cur.description else []

    def submit(self, **data):
        data.setdefault('raw', '  原始口述\n不应该丢失。  ')
        data.setdefault('date', '2026-10-18')
        response = self.client.post('/api/process', json=data)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()['jobId']

    def job(self, jid):
        return self.client.get('/api/jobs/' + jid).json()

    def test_atomic_save_raw_and_terminal_state(self):
        jid = self.submit()
        j = self.job(jid)
        self.assertEqual(j['status'], 'done', j)
        self.assertEqual(j['entry']['raw'], '  原始口述\n不应该丢失。  ')
        self.assertEqual(self.client.get('/api/jobs').json(), [])

    def test_duplicate_delivery_returns_same_job_and_single_entry(self):
        self.submit(request_id='same-submission-0001')
        self.submit(request_id='same-submission-0001')
        self.assertEqual(self.sql('SELECT count(*) FROM entries')[0][0], 1)
        self.assertEqual(self.model.call_count, 1)

    def test_same_text_different_mode_allowed(self):
        self.submit(mode='/只润色', request_id='same-text-polish-01')
        self.submit(mode='/深度复盘', request_id='same-text-deep-0001')
        self.assertEqual(self.sql('SELECT count(*) FROM entries')[0][0], 2)

    def test_id_reuse_with_changed_content_rejected(self):
        self.submit(request_id='same-submission-0001')
        r = self.client.post('/api/process', json={'raw': '其他内容', 'request_id': 'same-submission-0001'})
        self.assertEqual(r.status_code, 409)

    def test_failed_raw_discoverable_and_retry_same_job(self):
        self.model.side_effect = llm.LLMError('测试超时')
        jid = self.submit()
        self.assertEqual(self.job(jid)['status'], 'failed')
        pending = self.client.get('/api/jobs').json()
        self.assertEqual(pending[0]['raw'], '  原始口述\n不应该丢失。  ')
        self.model.side_effect = None
        r = self.client.post('/api/jobs/' + jid + '/retry')
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.job(jid)['status'], 'done')
        self.assertEqual(self.sql('SELECT count(*) FROM jobs')[0][0], 1)
        self.assertEqual(self.sql('SELECT count(*) FROM entries')[0][0], 1)
        self.assertEqual(self.client.post('/api/jobs/' + jid + '/retry').status_code, 409)

    def test_duplicate_worker_cannot_create_second_entry(self):
        jid = self.submit()
        main._run_process(jid, main.ProcessBody(raw='重复执行'))
        self.assertEqual(self.model.call_count, 1)

    def test_stale_worker_cannot_override_new_attempt(self):
        self.sql("INSERT INTO jobs(id,status,raw,attempt) VALUES('stale','running','原文',2)")
        main._job_update('stale', status='failed', error='旧任务', attempt=1)
        self.assertEqual(self.sql("SELECT status FROM jobs WHERE id='stale'")[0][0], 'running')

    def test_stale_queued_job_is_recoverable(self):
        self.sql("INSERT INTO jobs(id,status,raw,updated_at) VALUES('old','queued','旧原文','2020-01-01 00:00:00')")
        row = self.client.get('/api/jobs').json()[0]
        self.assertEqual((row['status'], row['raw']), ('failed', '旧原文'))

    def test_period_excludes_future_and_includes_full_tail(self):
        for day, text in [('2026-10-11', '太早'), ('2026-10-12', '首日'), ('2026-10-18', '前文'*300+'关键结尾'), ('2026-10-19', '未来')]:
            self.sql('INSERT INTO entries(date,raw,polished) VALUES(%s,%s,%s)', (day,text,text))
        self.submit(raw='', mode='/周总结')
        prompt = self.model.call_args.args[0]
        period = prompt.split('# 本次总结期间的全部日志',1)[1]
        self.assertIn('关键结尾', period)
        self.assertIn('共 2 条', period)
        self.assertNotIn('未来', period)
        self.assertNotIn('太早', period)

    def test_event_historical_date_saved(self):
        self.model.return_value = result(events=[{'title':'历史事件','date':'2026-10-06'}])
        self.submit()
        self.assertEqual(self.sql('SELECT date FROM events')[0][0], '2026-10-06')

    def test_edit_without_vector_keeps_raw_and_marks_derived_stale(self):
        jid = self.submit()
        eid = self.job(jid)['entryId']
        with patch.object(db, 'VECTOR_OK', False):
            r = self.client.patch('/api/entries/' + str(eid), json={'polished': '纠错后的正文'})
        self.assertEqual(r.status_code, 200, r.text)
        entry = self.client.get('/api/entries/' + str(eid)).json()
        self.assertEqual(entry['raw'], '  原始口述\n不应该丢失。  ')
        self.assertTrue(entry['derivedStale'])

    def test_profile_noop_save_preserves_ids_sources_and_deprecated(self):
        self.sql("INSERT INTO archive(section,text,source_entries,status) VALUES('长期背景','背景',ARRAY[31,32],'active'),('长期偏好','旧偏好',ARRAY[33],'deprecated')")
        text = '\n\n'.join('## '+s+'\n'+('背景' if s=='长期背景' else '(暂无)') for s in parser.SECTIONS)
        r = self.client.put('/api/profile', json={'text': text})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.sql('SELECT id,source_entries,status FROM archive ORDER BY id'), [(1,[31,32],'active'),(2,[33],'deprecated')])

    def test_removed_profile_item_is_deprecated_not_destroyed(self):
        self.sql("INSERT INTO archive(section,text,source_entries) VALUES('长期背景','旧背景',ARRAY[31])")
        text = '\n\n'.join('## '+s+'\n(暂无)' for s in parser.SECTIONS)
        self.assertEqual(self.client.put('/api/profile', json={'text': text}).status_code, 200)
        self.assertEqual(self.sql('SELECT source_entries,status FROM archive'), [([31], 'deprecated')])

    def test_confirm_edited_memory(self):
        self.model.return_value = result(memories=[dict(text='需要核对的记忆',category='长期背景')])
        self.submit()
        pid = self.client.get('/api/pending').json()[0]['id']
        r = self.client.post('/api/pending/%s/confirm'%pid, json={'cat':'长期背景','text':'经过本人修正的记忆'})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.sql('SELECT text FROM archive')[0][0], '经过本人修正的记忆')

    def test_failed_event_insert_rolls_back_entry(self):
        self.sql("ALTER TABLE events ADD CONSTRAINT test_fail_event CHECK(title <> '强制失败')")
        self.addCleanup(self.sql, 'ALTER TABLE events DROP CONSTRAINT test_fail_event')
        self.model.return_value = result(events=[{'title':'强制失败'}])
        jid = self.submit()
        self.assertEqual(self.job(jid)['status'], 'failed')
        self.assertEqual(self.sql('SELECT count(*) FROM entries')[0][0], 0)
        self.assertTrue(self.job(jid)['raw'])

    def test_empty_polish_and_invalid_date_rejected(self):
        self.assertEqual(self.client.post('/api/process',json={'mode':'/只润色'}).status_code,400)
        self.assertEqual(self.client.post('/api/process',json={'raw':'内容','date':'2026-02-30'}).status_code,400)

    def test_backup_restores_sources_after_id_changes(self):
        self.model.return_value = result(memories=[{'text':'已确认偏好','category':'长期偏好'}])
        jid = self.submit()
        pid = self.client.get('/api/pending').json()[0]['id']
        self.client.post('/api/pending/%s/confirm'%pid,json={'cat':'长期偏好'})
        old_id = self.job(jid)['entryId']
        self.client.post('/api/todos',json={'text':'带来源的待办','src_entry':old_id})
        backup = self.client.get('/api/export/json').json()
        self.sql('TRUNCATE pending_memories,events,entries,archive,todos,goals,jobs,embeddings CASCADE')
        response = self.client.post('/api/import',json=backup)
        self.assertEqual(response.status_code,200,response.text)
        new_id = self.sql('SELECT id FROM entries')[0][0]
        self.assertNotEqual(old_id,new_id)
        self.assertEqual(self.sql('SELECT source_entries FROM archive')[0][0],[new_id])
        self.assertEqual(self.sql('SELECT src_entry FROM todos')[0][0],new_id)
        self.assertEqual(self.client.get('/api/pending').json(),[])
        self.assertEqual(self.sql('SELECT status FROM pending_memories')[0][0],'archived')

    def test_backup_includes_unfinished_raw_and_import_is_idempotent(self):
        self.model.side_effect=llm.LLMError('模拟失败')
        jid=self.submit()
        backup=self.client.get('/api/export/json').json()
        self.assertEqual(backup['unfinishedJobs'][0]['id'],jid)
        self.sql('TRUNCATE jobs')
        for _ in range(2):
            r=self.client.post('/api/import',json=backup)
            self.assertEqual(r.status_code,200,r.text)
        self.assertEqual(self.sql('SELECT count(*) FROM jobs')[0][0],1)
        self.assertEqual(self.job(jid)['raw'],'  原始口述\n不应该丢失。  ')

    def test_two_modes_survive_backup_restore(self):
        self.submit(mode='/只润色')
        self.submit(mode='/深度复盘')
        backup=self.client.get('/api/export/json').json()
        self.sql('TRUNCATE pending_memories,events,entries,archive,todos,goals,jobs,embeddings CASCADE')
        r=self.client.post('/api/import',json=backup)
        self.assertEqual(r.status_code,200,r.text)
        self.assertEqual(self.sql('SELECT count(*) FROM entries')[0][0],2)


if __name__ == '__main__':
    unittest.main(verbosity=2)
