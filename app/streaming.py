"""Preview only complete JSON string characters; never expose model reasoning."""
import json
import re


def polished_preview(text):
    match = re.match(r'\s*\{\s*"polished"\s*:\s*"', text)
    if not match:
        return ''
    start = match.end()
    i = start
    while i < len(text):
        if text[i] == '"':
            return json.loads('"' + text[start:i] + '"')
        if text[i] == '\\':
            size = 6 if text[i:i+2] == '\\u' else 2
            if i + size > len(text):
                break
            i += size
        else:
            i += 1
    try:
        return json.loads('"' + text[start:i] + '"')
    except ValueError:
        return ''


async def collect_response(client, url, body, headers, callback, metrics):
    import time
    started = time.monotonic()
    content = ''
    finish = None
    complete = False
    async with client.stream('POST', url, json={**body, 'stream': True, 'stream_options': {'include_usage': True}}, headers=headers) as response:
        if response.status_code >= 400:
            await response.aread()
            return response
        async for line in response.aiter_lines():
            if not line.startswith('data:'):
                continue
            value = line[5:].strip()
            if value == '[DONE]':
                complete = True
                break
            if not value:
                continue
            chunk = json.loads(value)
            if chunk.get('error'):
                raise ValueError('Provider stream error')
            if chunk.get('usage'):
                metrics['usage'] = chunk['usage']
            choices = chunk.get('choices') or []
            if not choices:
                continue
            choice = choices[0]
            token = choice.get('delta', {}).get('content') or ''
            if token:
                metrics.setdefault('first_content_seconds', round(time.monotonic()-started, 3))
                content += token
                callback(polished_preview(content))
            if choice.get('finish_reason'):
                finish = choice['finish_reason']
    if not complete or finish not in ('stop', 'length'):
        raise ValueError('Incomplete model stream')
    import httpx
    return httpx.Response(200, json={'choices': [{'message': {'content': content}, 'finish_reason': finish}]})
