"""Opt-in only: legacy destructive tests must use a dedicated local database."""
import os
from psycopg2.extensions import parse_dsn, make_dsn


def configure_test_database():
    value = os.environ.get('SHIGUANG_TEST_DATABASE_URL', '')
    if not value:
        raise RuntimeError('Set SHIGUANG_TEST_DATABASE_URL to a disposable local test database. Production DATABASE_URL is never used.')
    parts = parse_dsn(value)
    if parts.get('host') not in ('localhost', '127.0.0.1', '::1'):
        raise RuntimeError('Destructive legacy tests are restricted to localhost.')
    if parts.get('dbname') != 'shiguang_test':
        raise RuntimeError('Database must be named shiguang_test.')
    parts['options'] = '-c search_path=sgtest'
    os.environ['DATABASE_URL'] = make_dsn(**parts)
