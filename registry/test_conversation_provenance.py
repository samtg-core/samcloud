"""#932: account != conversation, across records, audiences and reconnects."""
import hashlib
import json

import db
import main
import pytest
from test_registry import client


def fp(s):
    return hashlib.sha256(s.encode()).hexdigest()[:8]


@pytest.mark.parametrize('kind,path,body', [
    ('ticket', '/api/v1/tickets', {'summary': 'object'}),
    ('doc', '/api/v1/docs', {'title': 'object', 'body': 'record'}),
    ('task', None, {'title': 'object'}),
])
def test_sibling_comments_and_provenance(client, kind, path, body):
    if path is None:
        slug = client.post('/api/v1/projects', json={'name': 'provenance'}).json()['slug']
        path = f'/api/v1/projects/{slug}/tasks'
    made = client.post(path, json=body, headers={'X-Session-Id': 'receiver'}).json()
    oid = made['id']
    url = f'/api/v1/{kind}s/{oid}'
    tid = client.get(url).json()['thread_id']
    who = 'admin'
    # A sibling is born elsewhere, then contributes to this object.
    home = client.post('/api/v1/tickets', json={'summary': 'sibling home'},
                       headers={'X-Session-Id': 'writer'}).json()['thread_id']
    def post(text, sid):
        r = client.post(url + '/comments', json={'body': text}, headers={'X-Session-Id': sid})
        assert r.status_code == 200, r.text
        return r.json()['comment_id']
    cid = post('sibling contribution @admin', 'writer')
    c = next(c for c in client.get(url).json()['comments'] if c['id'] == cid)
    assert c['session_fp'] == fp('writer')
    assert c['home_thread_id'] == home and home != tid
    assert 'conversation_id' not in c and 'session_id' not in c
    feed = client.get('/api/v1/comments', params={'plane': kind}).json()['comments']
    assert next(c for c in feed if c['comment_id'] == cid)['home_thread_id'] == home
    rows = db.query('SELECT * FROM notifications WHERE recipient = ? AND actor = ? AND ' + kind + '_id = ?', (who, who, oid))
    comment_rows = [r for r in rows if json.loads(r['payload']).get('comment_id') == cid]
    assert len(comment_rows) == 1
    payload = json.loads(comment_rows[0]['payload'])
    assert payload['actor_session'] == fp('writer')
    assert payload['actor_home_thread_id'] == home
    assert payload['recipient_session'] == fp('receiver')
    nid = comment_rows[0]['id']
    # Registry restart preserves both origin and the pending target.
    db.init_db()
    for sid, expected in [('receiver', True), ('writer', False), ('unrelated', False)]:
        got = client.get('/api/v1/notifications', params={'for_conversation': sid}).json()['notifications']
        assert (nid in [r['id'] for r in got]) == expected
    own = post('receiver own write', 'receiver')
    assert not db.query("SELECT id FROM notifications WHERE recipient = ? AND actor = ? "
                        "AND " + kind + "_id = ? AND json_extract(payload, '$.comment_id') = ?", (who, who, oid, own))
    # Rebinding after shutdown does not transfer a pending sibling message.
    db.execute("UPDATE threads SET conversation_id = 'replacement' WHERE identity = ? AND thread_id = ?", (who, tid))
    got = client.get('/api/v1/notifications', params={'for_conversation': 'replacement'}).json()['notifications']
    assert nid not in [r['id'] for r in got]
    assert next(c for c in client.get(url).json()['comments'] if c['id'] == cid)['home_thread_id'] == home


def test_unknown_history_and_migration(client):
    t = client.post('/api/v1/tickets', json={'summary': 'legacy'}).json()['id']
    client.post(f'/api/v1/tickets/{t}/comments', json={'body': 'no session'})
    row = client.get(f'/api/v1/tickets/{t}').json()['comments'][0]
    assert row['session_fp'] is None and row['home_thread_id'] is None
    # Emulate pre-upgrade tables in this throwaway DB, then run the real migration.
    for table in ('task_comments', 'doc_comments'):
        db.execute(f'ALTER TABLE {table} DROP COLUMN session_fp')
        db.execute(f'ALTER TABLE {table} DROP COLUMN home_thread_id')
    db.init_db()
    db.init_db()
    for table in ('task_comments', 'ticket_comments', 'doc_comments'):
        assert {'session_fp', 'home_thread_id'} <= {r['name'] for r in db.query(f'PRAGMA table_info({table})')}


def test_birth_origin_does_not_follow_binding(client):
    a = client.post('/api/v1/tickets', json={'summary': 'birth'}).json()['id']
    b = client.post('/api/v1/tickets', json={'summary': 'elsewhere'}, headers={'X-Session-Id': 'other'}).json()['id']
    who = 'admin'
    first = main._upsert_thread('ticket', a, who, 'holder-origin', holder='stream')
    main._upsert_thread('ticket', b, who, 'holder-origin', holder='stream')
    assert main._writer_provenance(who, 'holder-origin')['home_thread_id'] == first['thread_id']


def test_legacy_ambiguous_birth_stays_unknown_on_heartbeat(client):
    a = client.post('/api/v1/tickets', json={'summary': 'one'}).json()['id']
    b = client.post('/api/v1/tickets', json={'summary': 'two'},
                    headers={'X-Session-Id': 'other'}).json()['id']
    main._upsert_thread('ticket', a, 'admin', 'legacy', holder='stream')
    main._upsert_thread('ticket', b, 'admin', 'legacy', holder='stream')
    db.execute("DELETE FROM conversation_origins WHERE conversation_id = 'legacy'")
    assert main._writer_provenance('admin', 'legacy')['home_thread_id'] is None
    main._upsert_thread('ticket', b, 'admin', 'legacy', holder='stream')
    assert main._writer_provenance('admin', 'legacy')['home_thread_id'] is None
