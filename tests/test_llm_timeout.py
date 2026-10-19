import asyncio
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from app import llm


class TimeoutTests(unittest.TestCase):
    def run_call(self, outcomes, deep=False):
        client = AsyncMock()
        client.post.side_effect = outcomes
        client.__aenter__.return_value = client
        with patch.object(llm.config, 'ZHIPU_API_KEY', 'fake'), patch.object(llm.httpx, 'AsyncClient', return_value=client) as factory:
            result = llm.call_glm('private diary', deep)
        return result, client, factory

    def response(self):
        return httpx.Response(200, json={'choices': [{'message': {'content': 'ok'}, 'finish_reason': 'stop'}]})

    def test_single_generation_has_full_budget(self):
        result, client, factory = self.run_call([self.response()])
        self.assertEqual(result, 'ok')
        self.assertEqual(client.post.await_count, 1)
        self.assertGreater(factory.call_args.kwargs['timeout'].read, 175)

    def test_daily_uses_documented_low_reasoning_parameter(self):
        with patch.object(llm.config, 'GLM_MODEL', 'glm-5.3-flash'):
            _, client, _ = self.run_call([self.response()])
        body = client.post.call_args.kwargs['json']
        self.assertEqual(body['reasoning_effort'], 'low')
        self.assertEqual(body['thinking'], {'type': 'enabled'})
        self.assertEqual(body['max_tokens'], 16384)

    def test_deep_uses_high_reasoning(self):
        with patch.object(llm.config, 'GLM_MODEL', 'glm-5.3-flash'), patch.object(llm.config, 'GLM_MODEL_DEEP', 'glm-5.3'):
            _, client, _ = self.run_call([self.response()], deep=True)
        body = client.post.call_args.kwargs['json']
        self.assertEqual(body['reasoning_effort'], 'high')
        self.assertEqual(body['model'], 'glm-5.3')

    def test_read_timeout_never_restarts_generation(self):
        client = AsyncMock()
        client.__aenter__.return_value = client
        client.post.side_effect = httpx.ReadTimeout('private transport details')
        with patch.object(llm.config, 'ZHIPU_API_KEY', 'fake'), patch.object(llm.httpx, 'AsyncClient', return_value=client):
            with self.assertRaises(llm.LLMError):
                llm.call_glm('private diary')
        self.assertEqual(client.post.await_count, 1)

    def test_fast_connect_failure_can_retry(self):
        result, client, _ = self.run_call([httpx.ConnectTimeout('x'), self.response()])
        self.assertEqual(result, 'ok')
        self.assertEqual(client.post.await_count, 2)

    def test_deep_does_not_retry_connection(self):
        with self.assertRaises(llm.LLMError):
            self.run_call([httpx.ConnectTimeout('x'), self.response()], deep=True)

    def test_total_deadline_cancels_inflight_request(self):
        async def slow(*args):
            await asyncio.sleep(1)
        with patch.object(llm, 'NORMAL_TIMEOUT', 0.01), patch.object(llm, '_call_glm', side_effect=slow):
            with self.assertRaisesRegex(llm.LLMError, '模型处理超时'):
                llm.call_glm('private diary')

    def test_diagnostic_logs_do_not_include_content(self):
        with self.assertLogs(llm.logger, level='WARNING') as logs:
            self.run_call([self.response()])
        output = '\n'.join(logs.output)
        self.assertIn('model=', output)
        self.assertIn('elapsed=', output)
        self.assertNotIn('private diary', output)
        self.assertNotIn('fake', output)


if __name__ == '__main__':
    unittest.main()
