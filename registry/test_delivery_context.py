"""RFCs 385/386: audience selection and context, through the real read/write routes."""
import asyncio
import json

import db
import main
import pytest
from test_registry import client, AuthedClient, _invite_and_activate


def agent(client, name):
    token, _ = _invite_and_activate(client, client._client, name, [])
    return AuthedClient(client._client, token)


def inbox(client, **params):
    return client.get('/api/v1/notifications', params=params).json()['notifications']


@pytest.mark.parametrize('kind', ['ticket', 'doc'])
def test_participation_is_not_update_subscription(client, kind):
    reader = agent(client, 'reader')
    made = client.post(f'/api/v1/{kind}s', json=(
        {'summary': 'work'} if kind == 'ticket' else {'title': 'record', 'body': 'v1'})).json()
    oid = made['id']
    url = f'/api/v1/{kind}s/{oid}'
    assert inbox(reader) == []  # group access does not announce a new doc
    reader.post(url + '/comments', json={'body': 'participating'})
    client.post(url + '/comments', json={'body': 'actual conversation'})
    assert inbox(reader) == []  # a one-off comment is not a subscription
    reader.post('/api/v1/notifications/ack', json={'all': True})
    def change():
        return (client.patch(url, json={'priority': 'high'}) if kind == 'ticket'
                else client.post(url + '/append', json={'block': 'next', 'note': 'added next'}))
    assert change().status_code == 200
    assert inbox(reader) == []
    assert reader.post(url + '/subscribe').status_code == 200
    assert change().status_code == 200
    rows = inbox(reader)
    assert len(rows) == 1
    assert rows[0]['event_type'] == ('ticket.updated' if kind == 'ticket' else 'doc.revised')
    assert rows[0]['read_object'].startswith('GET ' + url)
    assert 'body' not in rows[0]['payload']
    # Later participation does not downgrade the explicit opt-in.
    reader.post(url + '/comments', json={'body': 'still participating'})
    assert reader.get(url + '/watchers').json()['watchers'][-1]['updates'] == 1
    reader.delete(url + '/subscribe')
    reader.post('/api/v1/notifications/ack', json={'all': True})
    reader.post(url + '/comments', json={'body': 'one-off after opting out'})
    assert change().status_code == 200
    assert inbox(reader) == []


def test_doc_owner_is_responsible_and_transfer_is_addressed(client):
    owner, successor = agent(client, 'owner'), agent(client, 'successor')
    made = client.post('/api/v1/docs', json={
        'title': 'owned', 'body': 'brief', 'owner': 'owner'}).json()
    url = f"/api/v1/docs/{made['id']}"
    assert made['owner'] == 'owner'
    assert inbox(owner)[0]['event_type'] == 'doc.assigned'
    assert inbox(successor) == []
    owner.post('/api/v1/notifications/ack', json={'all': True})
    client.post(url + '/append', json={'block': 'new brief'})
    assert [r['event_type'] for r in inbox(owner)] == ['doc.revised']
    assert owner.patch(url, json={'owner': 'successor'}).status_code == 200
    assert client.get(url).json()['source'] == 'admin'
    assert client.get(url).json()['owner'] == 'successor'
    assert [r['event_type'] for r in inbox(successor)] == ['doc.assigned']
    assert owner.patch(url, json={'owner': 'owner'}).status_code == 403
    assert client.patch(url, json={'owner': ''}).status_code == 422
    assert client.patch(url, json={'owner': 'nonexistent'}).status_code == 422


def test_owner_must_have_group_access(client):
    agent(client, 'outsider')
    client.post('/api/v1/groups', json={'id': 'private', 'name': 'Private'})
    r = client.post('/api/v1/docs', json={
        'title': 'private', 'body': 'secret', 'group_scope': 'private', 'owner': 'outsider'})
    assert r.status_code == 422


def test_link_is_not_revision_subscription(client):
    reader = agent(client, 'reader')
    ticket = client.post('/api/v1/tickets', json={'summary': 'linked'}).json()['id']
    reader.post(f'/api/v1/tickets/{ticket}/subscribe')
    doc = client.post('/api/v1/docs', json={'title': 'deliverable', 'body': 'v1'}).json()['id']
    client.post(f'/api/v1/docs/{doc}/links', json={'target_type': 'ticket', 'target_id': str(ticket)})
    reader.post('/api/v1/notifications/ack', json={'all': True})
    client.post(f'/api/v1/docs/{doc}/append', json={'block': 'v2'})
    assert inbox(reader) == []


def test_task_changes_reach_assignee_not_commenters(client):
    owner, reader = agent(client, 'owner'), agent(client, 'reader')
    project = client.post('/api/v1/projects', json={'name': 'tasks'}).json()['slug']
    task = client.post(f'/api/v1/projects/{project}/tasks', json={
        'title': 'task', 'assignee': 'owner'}).json()['id']
    url = f'/api/v1/tasks/{task}'
    reader.post(url + '/comments', json={'body': 'one comment'})
    for c in (owner, reader):
        c.post('/api/v1/notifications/ack', json={'all': True})
    client.patch(url, json={'detail': 'new brief'})
    assert len(inbox(owner)) == 1
    assert inbox(owner)[0]['payload']['changed'] == ['detail']
    assert inbox(reader) == []
    reader.post(f'/api/v1/projects/{project}/subscribe')
    client.patch(url, json={'detail': 'another brief'})
    assert len(inbox(reader)) == 1


def test_records_are_references_comments_and_direct_asks_are_text(client):
    reader = agent(client, 'reader')
    body = 'RECORD CONTENT ' * 900
    made = client.post('/api/v1/tickets', json={
        'summary': 'Work', 'body': body, 'assignee': 'reader'}).json()
    row = inbox(reader)[0]
    assert 'body' not in row['payload'] and body not in json.dumps(row)
    assert row['read_object'] == f"GET /api/v1/tickets/{made['id']}"
    comment = '@reader Please check exactly these criteria: ' + 'criterion ' * 200
    client.post(f"/api/v1/tickets/{made['id']}/comments", json={'body': comment})
    row = inbox(reader)[0]
    assert row['payload']['body'] == comment
    assert 'body' not in row['payload']['root']
    # A mention in a record stays directed, but requires reading that record.
    client.post('/api/v1/docs', json={'title': 'Directed record', 'body': '@reader ' + body})
    row = inbox(reader)[0]
    assert row['event_type'] == 'doc.mentioned'
    assert row['payload']['read_required'] is True
    assert 'body' not in row['payload']
    assert row['read_object'].endswith('?rev=1')


def test_owner_mention_is_one_directed_event_and_revision_is_pinned(client):
    owner = agent(client, 'owner')
    doc = client.post('/api/v1/docs', json={
        'title': 'brief', 'body': '@owner read this', 'owner': 'owner'}).json()['id']
    rows = inbox(owner)
    assert len(rows) == 1 and rows[0]['event_type'] == 'doc.mentioned'
    assert rows[0]['read_object'] == f'GET /api/v1/docs/{doc}?rev=1'
    client.post(f'/api/v1/docs/{doc}/append', json={'block': '@owner new criteria'})
    mentions = [r for r in inbox(owner) if r['event_type'] == 'doc.mentioned']
    assert mentions[0]['read_object'].endswith('?rev=2')


def test_siblings_group_exclude_self_and_do_not_claim_health(client):
    def create(sid, title):
        return client.post('/api/v1/tickets', json={'summary': title},
                           headers={'X-Session-Id': sid}).json()
    own, other, third = create('receiver', 'one'), create('sibling', 'two'), create('third', 'three')
    # One conversation holds two threads; the unit in the hint stays the conversation.
    db.execute("UPDATE threads SET conversation_id = 'sibling' WHERE conversation_id = 'third'")
    db.execute("UPDATE threads SET pid = 123 WHERE object_id = ?", (other['id'],))
    db.execute("INSERT INTO notifications (recipient, ticket_id, event_type, actor, payload) "
               "VALUES ('admin', ?, 'ticket.commented', 'peer', ?)",
               (own['id'], json.dumps({'body': 'hello'})))
    row = inbox(client, for_conversation='receiver')[0]
    assert len(row['siblings']) == 1
    assert row['siblings'][0]['conversation'] == main._session_fp('sibling')
    assert len(row['siblings'][0]['threads']) == 2
    assert 'process recorded' in row['siblings_text']
    assert 'no process recorded' in row['siblings_text']
    assert 'live' not in row['siblings_text']
    assert 'receiver' not in row['siblings_text']
    watched = asyncio.run(main._watch_loop(1, None, None, 0, 'receiver', False, 'admin', None))
    assert watched['notifications'][0]['siblings'] == row['siblings']
    db.execute("UPDATE threads SET state = 'closed' WHERE conversation_id = 'sibling'")
    assert inbox(client, for_conversation='receiver')[0]['siblings'] == []


def test_hidden_sibling_has_no_object_metadata(client):
    reader = agent(client, 'reader')
    doc = client.post('/api/v1/docs', json={'title': 'secret title', 'body': 'secret'}).json()
    main._upsert_thread('doc', doc['id'], 'reader', 'hidden-sibling', holder='stream')
    client.post('/api/v1/groups', json={'id': 'private', 'name': 'Private'})
    client.patch(f"/api/v1/docs/{doc['id']}", json={'group_scope': 'private'})
    db.execute("INSERT INTO notifications (recipient, event_type, actor, payload) "
               "VALUES ('reader', 'wake.probe', 'admin', '{}')")
    row = inbox(reader)[0]
    assert row['siblings'][0]['threads'][0]['hidden']
    assert 'secret' not in json.dumps(row)
    assert '(hidden)' in row['siblings_text']


def test_pending_inbox_is_not_a_park_diagnosis_or_an_assignment(client):
    first = client.post('/api/v1/tickets', json={'summary': 'first'},
                        headers={'X-Session-Id': 'receiver'}).json()
    second = client.post('/api/v1/tickets', json={'summary': 'second'},
                         headers={'X-Session-Id': 'sibling'}).json()
    for ticket, text, cid in ((first, 'current body', 1), (second, 'other private body', 2)):
        db.execute("INSERT INTO notifications (recipient, ticket_id, event_type, actor, payload) "
                   "VALUES ('admin', ?, 'ticket.commented', 'peer', ?)",
                   (ticket['id'], json.dumps({'body': text, 'comment_id': cid})))
    row = inbox(client, for_conversation='receiver')[0]
    pending = row['pending_inbox']
    assert pending['count'] == 1
    assert f"ticket #{second['id']} c2" in pending['references'][0]
    assert 'other private body' not in json.dumps(row)
    assert 'parked' not in row['pending_inbox_text']
    db.execute("UPDATE notifications SET delivered_to = 'sibling' WHERE ticket_id = ?", (second['id'],))
    assert inbox(client, for_conversation='receiver')[0]['pending_inbox_text'] == ''


def test_context_bounds_and_hidden_pending_reference(client):
    reader = agent(client, 'reader')
    doc = client.post('/api/v1/docs', json={'title': 'secret title', 'body': 'secret body'}).json()['id']
    client.post('/api/v1/groups', json={'id': 'private', 'name': 'Private'})
    client.patch(f'/api/v1/docs/{doc}', json={'group_scope': 'private'})
    for n in range(8):
        main._upsert_thread('doc', doc, 'reader', f'sibling-{n}', holder='stream')
        # A real binding is unique per object/identity; distinct objects are
        # needed for simultaneous siblings, not overwrites of the same row.
        if n < 7:
            doc = client.post('/api/v1/docs', json={
                'title': 'secret title', 'body': 'secret body', 'group_scope': 'private'},
                headers={'X-Session-Id': f'creator-{n}'}).json()['id']
    for n in range(8):
        db.execute("INSERT INTO notifications (recipient, doc_id, event_type, actor, payload) "
                   "VALUES ('reader', ?, 'doc.commented', 'peer', ?)",
                   (doc, json.dumps({'body': 'secret body', 'comment_id': n})))
    db.execute("INSERT INTO notifications (recipient, event_type, actor, payload) "
               "VALUES ('reader', 'wake.probe', 'admin', '{}')")
    row = inbox(reader, limit=1)[0]
    assert len(row['siblings']) == 8
    assert '+2 conversations' in row['siblings_text']
    assert row['pending_inbox']['count'] == 8
    assert len(row['pending_inbox']['references']) == 6
    assert all('(hidden)' in ref for ref in row['pending_inbox']['references'])
    assert 'secret' not in json.dumps(row)


def test_legacy_publish_stays_inbox_only_without_false_receipt(client):
    doc = client.post('/api/v1/docs', json={'title': 'old publish', 'body': 'record'},
                      headers={'X-Session-Id': 'receiver'}).json()
    nid = db.execute("INSERT INTO notifications (recipient, doc_id, event_type, actor, payload) "
                     "VALUES ('admin', ?, 'doc.published', 'peer', ?)",
                     (doc['id'], json.dumps({'body': 'old full record'})))
    assert any(r['id'] == nid for r in inbox(client))
    assert not inbox(client, for_conversation='receiver')
    assert db.query_one('SELECT delivered_at FROM notifications WHERE id = ?', (nid,))['delivered_at'] is None
    comment = db.execute("INSERT INTO notifications (recipient, doc_id, event_type, actor, payload) "
                         "VALUES ('admin', ?, 'doc.commented', 'peer', '{}')", (doc['id'],))
    for _ in range(55):
        db.execute("INSERT INTO notifications (recipient, doc_id, event_type, actor, payload) "
                   "VALUES ('admin', ?, 'doc.published', 'peer', '{}')", (doc['id'],))
    rows = inbox(client, delivery=True, undelivered=True, limit=1)
    assert [r['id'] for r in rows] == [comment]  # exclusion precedes pagination


def test_migration_keeps_participants_without_guessing_opt_in(client):
    doc = client.post('/api/v1/docs', json={'title': 'old doc', 'body': 'record'}).json()['id']
    for table in ('ticket_watchers', 'doc_watchers'):
        db.execute(f'ALTER TABLE {table} DROP COLUMN updates')
    db.execute('ALTER TABLE docs DROP COLUMN owner')
    db.init_db()
    assert db.query_one('SELECT owner FROM docs WHERE id = ?', (doc,))['owner'] == 'admin'
    assert db.query_one('SELECT updates FROM doc_watchers WHERE doc_id = ?', (doc,))['updates'] == 0
    client.post(f'/api/v1/docs/{doc}/subscribe')
    db.init_db()
    assert db.query_one('SELECT updates FROM doc_watchers WHERE doc_id = ?', (doc,))['updates'] == 1
