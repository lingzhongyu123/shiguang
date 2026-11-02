import unittest

from app.llm import build_user_prompt, history_excerpt


class ContextTests(unittest.TestCase):
    def prompt(self, recent=(), related=(), **kwargs):
        return build_user_prompt({}, [], recent, related, '今天有新进展',
                                 '2026-11-02', '', **kwargs)

    def test_history_preserves_outcome_after_old_cutoff(self):
        text = '背景' * 100 + '最后解决了问题，不再需要催促'
        result = self.prompt(related=[dict(date='2026-11-01', source_type='entry',
                                          source_id=1, content=text)])
        self.assertIn(text, result)

    def test_duplicate_entry_is_not_duplicate_evidence(self):
        result = self.prompt(
            recent=[dict(id=1, date='2026-11-01', polished='完整新正文', raw='原文')],
            related=[dict(date='2026-11-01', source_type='entry', source_id='1', content='重复片段'),
                     dict(date='', source_type='memory', source_id=1, content='已确认偏好')])
        self.assertIn('完整新正文', result)
        self.assertNotIn('重复片段', result)
        self.assertIn('已确认偏好', result)

    def test_long_history_keeps_end_and_labels_gap(self):
        result = history_excerpt('开头' + '甲' * 3000 + '最后已经完成')
        self.assertTrue(result.startswith('开头'))
        self.assertTrue(result.endswith('最后已经完成'))
        self.assertIn('中间内容省略', result)
        self.assertLess(len(result), 2100)

    def test_all_eight_distinct_retrievals_retained(self):
        result = self.prompt(related=[dict(date='', source_type='entry', source_id=i,
                                          content=f'独立记录{i}结束') for i in range(8)])
        for i in range(8):
            self.assertIn(f'独立记录{i}结束', result)

    def test_period_records_are_not_excerpted(self):
        text = '期间正文' * 1000
        self.assertIn(text, self.prompt(period_entries=[dict(date='2026-11-01', text=text)]))


if __name__ == '__main__':
    unittest.main()
