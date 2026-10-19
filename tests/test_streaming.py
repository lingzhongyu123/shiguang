import asyncio
import json
import unittest
from app.streaming import polished_preview, collect_response
from app.llm import output_budget


class Response:
    status_code = 200
    def __init__(self, lines): self.lines = lines
    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass
    async def aiter_lines(self):
        for line in self.lines: yield line


class Client:
    def __init__(self, lines): self.lines = lines
    def stream(self, *args, **kwargs): return Response(self.lines)


def event(delta=None, finish=None):
    return 'data: ' + json.dumps({'choices': [{'delta': delta or {}, 'finish_reason': finish}]})


class StreamingTests(unittest.TestCase):
    def test_partial_escape(self):
        self.assertEqual(polished_preview('{"polished":"你好\\u4'), '你好')
        self.assertEqual(polished_preview('{"polished":"甲\\n乙"'), '甲\n乙')
        self.assertEqual(polished_preview('{"summary":"secret"'), '')

    def test_stream_only_previews_content_and_requires_end(self):
        previews = []
        metrics = {}
        lines = [event({'reasoning_content': 'hidden'}), event({'content': '{"polished":"正文'}), event({'content': '"}'}), event(finish='stop'), 'data: [DONE]']
        response = asyncio.run(collect_response(Client(lines), 'unused', {}, {}, previews.append, metrics))
        self.assertEqual(previews[-1], '正文')
        self.assertNotIn('hidden', ''.join(previews))
        self.assertEqual(response.json()['choices'][0]['finish_reason'], 'stop')
        self.assertIn('first_content_seconds', metrics)

    def test_broken_stream_is_not_success(self):
        with self.assertRaises(ValueError):
            asyncio.run(collect_response(Client([event({'content': '{"polished":"x"}'})]), 'unused', {}, {}, lambda _: None, {}))

    def test_mode_budgets(self):
        self.assertEqual(output_budget('/只润色'), 16384)
        self.assertEqual(output_budget('/快速'), 16384)
        self.assertEqual(output_budget(''), 32768)
        self.assertEqual(output_budget('/月总结', True), 32768)


if __name__ == '__main__': unittest.main()
