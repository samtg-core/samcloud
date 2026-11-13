"""Search across planes (#1082): one query over tickets, tasks and docs."""
import db
from test_registry import client, AuthedClient  # noqa: F401  (fixture)
from test_inbox_feed import user


def search(who, **q):
    r = who.get('/api/v1/search', params=q)
    assert r.status_code == 200, r.text
    return r.json()


def found(body):
    return [(i['kind'], i['id']) for i in body['items']]


def test_one_query_finds_titles_bodies_and_comments_on_every_plane(client):
    assert client.post('/api/v1/groups', json={'id': 'home', 'name': 'home'}).status_code == 200
    t = client.post('/api/v1/tickets', json={'summary': 'Router drops wifi', 'group_scope': 'home'}).json()
    p = client.post('/api/v1/projects', json={'name': 'Net', 'group_scope': 'home'}).json()
    k = client.post(f"/api/v1/projects/{p['slug']}/tasks", json={'title': 'Buy a mesh kit',
                                                                 'detail': 'the old router is dying'}).json()
    d = client.post('/api/v1/docs', json={'title': 'House network', 'body': 'b', 'group_scope': 'home'}).json()
    client.post(f"/api/v1/docs/{d['id']}/comments", json={'body': 'the ROUTER lives in the hall cupboard'})
    body = search(client, q='router')
    assert found(body) == [('ticket', t['id']), ('task', k['id']), ('doc', d['id'])]
    assert [i['match'] for i in body['items']] == ['title', 'body', 'comment']
    assert body['items'][2]['snippet'].startswith('admin: the ROUTER')
    assert body['items'][0]['ref'] == f"[Router drops wifi](/tickets/{t['id']})"
    assert body['counts'] == {'ticket': 1, 'task': 1, 'doc': 1}


def test_every_word_must_match_somewhere_and_an_id_matches_itself(client):
    a = client.post('/api/v1/tickets', json={'summary': 'printer offline'}).json()
    client.post('/api/v1/tickets', json={'summary': 'printer toner'})
    client.post(f"/api/v1/tickets/{a['id']}/comments", json={'body': 'rebooted it'})
    assert found(search(client, q='printer rebooted')) == [('ticket', a['id'])]
    assert found(search(client, q=f"#{a['id']}", kind='ticket'))[0] == ('ticket', a['id'])
    assert search(client, q=f"#{a['id']}", kind='ticket')['items'][0]['match'] == 'id'
    # LIKE wildcards in the query are literal
    assert found(search(client, q='%')) == [] and found(search(client, q='_')) == []


def test_one_filter_vocabulary_across_planes(client):
    me = client.post('/api/v1/tickets', json={'summary': 'mine open', 'assignee': 'admin'}).json()
    done = client.post('/api/v1/tickets', json={'summary': 'mine done', 'assignee': 'admin'}).json()
    client.patch(f"/api/v1/tickets/{done['id']}", json={'status': 'resolved'})
    p = client.post('/api/v1/projects', json={'name': 'P'}).json()
    blocked = client.post(f"/api/v1/projects/{p['slug']}/tasks", json={'title': 'stuck', 'assignee': 'admin'}).json()
    client.patch(f"/api/v1/tasks/{blocked['id']}", json={'status': 'blocked'})
    finished = client.post(f"/api/v1/projects/{p['slug']}/tasks", json={'title': 'over'}).json()
    client.patch(f"/api/v1/tasks/{finished['id']}", json={'status': 'done'})
    active = set(found(search(client, status='active')))
    assert {('ticket', me['id']), ('task', blocked['id'])} <= active
    assert not {('ticket', done['id']), ('task', finished['id'])} & active
    assert found(search(client, kind='task', status='blocked')) == [('task', blocked['id'])]
    assert set(found(search(client, assignee=':me', status='active'))) == {('ticket', me['id']), ('task', blocked['id'])}
    db.execute("UPDATE tickets SET updated_at = datetime('now', '-3 days') WHERE id = ?", (me['id'],))
    assert ('ticket', me['id']) not in found(search(client, updated_hours=24))


def test_scope_holds(client):
    sam = user(client, 'sam', {'home': 'editor'})
    user(client, 'peer', {'work': 'editor'})
    client.post('/api/v1/tickets', json={'summary': 'secret plan', 'group_scope': 'work'})
    d = client.post('/api/v1/docs', json={'title': 'work doc', 'body': 'secret plan', 'group_scope': 'work'}).json()
    p = client.post('/api/v1/projects', json={'name': 'W', 'group_scope': 'work'}).json()
    client.post(f"/api/v1/projects/{p['slug']}/tasks", json={'title': 'secret plan'})
    assert search(sam, q='secret')['total'] == 0
    assert search(client, q='secret')['total'] == 3
    assert found(search(client, q='secret', group_scope='work', kind='doc')) == [('doc', d['id'])]


def test_paging_and_bad_input(client):
    for n in range(5):
        client.post('/api/v1/tickets', json={'summary': f'batch item {n}'})
    first, second = search(client, q='batch', limit=3), search(client, q='batch', limit=3, offset=3)
    assert first['total'] == 5 and len(first['items']) == 3 and len(second['items']) == 2
    assert not set(found(first)) & set(found(second))
    assert client.get('/api/v1/search', params={'kind': 'thread'}).status_code == 400
    assert client.get('/api/v1/search', params={'updated_hours': 0}).status_code == 400
