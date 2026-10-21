"""Import a decrypted backup twice in a fresh localhost-only database."""
import json
import os
import sys
from pathlib import Path
from psycopg2.extensions import parse_dsn
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    dsn = parse_dsn(os.environ['DATABASE_URL'])
    if dsn.get('host') not in ('localhost', '127.0.0.1') or dsn.get('dbname') != 'shiguang_restore':
        raise SystemExit('Restore requires a disposable localhost shiguang_restore database.')
    backup = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
    from app import db, main as service
    from fastapi.testclient import TestClient
    with db.db() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT count(*) FROM entries')
            if cur.fetchone()[0]: raise SystemExit('Restore database must be empty.')
    client = TestClient(service.app, base_url="https://testserver")
    client.post('/api/login', json={'password':os.environ['SHIGUANG_PASSWORD']}).raise_for_status()
    client.post('/api/import', json=backup).raise_for_status()
    first = client.get('/api/export/json').json()
    client.post('/api/import', json=backup).raise_for_status()
    second = client.get('/api/export/json').json()
    for key in ('entries', 'pending', 'todos', 'events', 'goals', 'unfinishedJobs'):
        assert len(first.get(key, [])) == len(second.get(key, [])), 'Repeated import changed count: '+key
    expected = {(e.get('date',''), e.get('mode',''), e.get('raw',''), e.get('polished','')) for e in backup['entries']}
    actual = {(e.get('date',''), e.get('mode',''), e.get('raw',''), e.get('polished','')) for e in first['entries']}
    assert expected == actual, 'Entry content mismatch'
    def signatures(data):
        ids = {e['id']:(e.get('date',''), e.get('mode',''), e.get('raw','')) for e in data['entries']}
        return {(m['section'],m['text']):{ids[i] for i in m.get('sources',[]) if i in ids} for m in data['profile'].get('items',[])}
    assert signatures(backup) == signatures(first), 'Memory evidence mismatch'
    if backup.get('formatVersion', 1) >= 2:
        expected_pending = {(p['text'], p.get('status','pending')) for p in backup.get('pending',[])}
        actual_pending = {(p['text'], p.get('status','pending')) for p in first.get('pending',[])}
        assert expected_pending == actual_pending, 'Pending state mismatch'
    for job in backup.get('unfinishedJobs',[]):
        assert any(j['id']==job['id'] and j.get('raw')==job.get('raw') for j in first.get('unfinishedJobs',[])), 'Unfinished raw missing'
    print('RESTORE PASS: raw/body, memory evidence, unfinished raw and repeated import verified.')
    print('Restored entry count:', len(first['entries']))


if __name__ == '__main__': main()
