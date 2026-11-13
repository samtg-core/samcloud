"""The inbox feed (#1082): one card per ticket, task or doc, read and star per card."""
import db
from test_registry import client, AuthedClient  # noqa: F401  (fixture)


def user(c, name, groups):
    for g in groups:
        if not db.query_one('SELECT 1 FROM groups WHERE id=?', (g,)):
            assert c.post('/api/v1/groups', json={'id': g, 'name': g}).status_code == 200
    r = c.post('/api/v1/users', json={'username': name, 'password': 'test-pw', 'role': 'user',
                                      'scopes': ['group:' + g for g in groups], 'group_roles': groups})
    assert r.status_code == 200, r.text
    a = c.post('/api/v1/auth/login', json={'username': name, 'password': 'test-pw'})
    return AuthedClient(c._client, a.json()['token'])


def feed(who, **q):
    r = who.get('/api/v1/inbox/feed', params=q)
    assert r.status_code == 200, r.text
    return r.json()


def cards(body):
    return [(i['kind'], i['id']) for i in body['items']]


def age(kind, oid, minutes):
    """Move an object's activity into the past, so a later event is newer."""
    ts = f"-{minutes} minutes"
    db.execute(f"UPDATE {kind}s SET created_at = datetime('now', ?), updated_at = datetime('now', ?) "
               "WHERE id = ?", (ts, ts, oid))


def start_feed(who, minutes=60):
    """Open the feed once and put its epoch in the past."""
    feed(who)
    db.execute("UPDATE inbox_state SET epoch = datetime('now', ?)", (f"-{minutes} minutes",))


def test_default_is_what_is_addressed_to_you_across_tickets_tasks_and_docs(client):
    sam = user(client, 'sam', {'home': 'editor'})
    start_feed(sam)
    d = client.post('/api/v1/docs', json={'title': 'plan', 'body': 'b', 'group_scope': 'home'}).json()
    client.post(f"/api/v1/docs/{d['id']}/comments", json={'body': 'what do you think @sam'})
    p = client.post('/api/v1/projects', json={'name': 'Home', 'group_scope': 'home'}).json()
    k = client.post(f"/api/v1/projects/{p['slug']}/tasks", json={'title': 'fix tap', 'assignee': 'sam'}).json()
    mine = sam.post('/api/v1/tickets', json={'summary': 'my ticket', 'group_scope': 'home'}).json()
    other = client.post('/api/v1/tickets', json={'summary': 'not about sam', 'group_scope': 'home'}).json()

    body = feed(sam)
    assert set(cards(body)) == {('doc', d['id']), ('task', k['id']), ('ticket', mine['id'])}
    doc = next(i for i in body['items'] if i['kind'] == 'doc')
    assert doc['mention'] and doc['latest']['what'] == 'commented' and doc['latest']['by'] == 'admin'
    # everything visible is one filter away, never the default
    assert ('ticket', other['id']) in cards(feed(sam, filter='all'))
    assert body['counts']['mine'] == 3 and body['counts']['all'] == 4


def test_one_busy_thread_is_one_card_and_cannot_crowd_out_the_rest(client):
    sam = user(client, 'sam', {'home': 'editor'})
    busy = client.post('/api/v1/tickets', json={'summary': 'busy', 'group_scope': 'home',
                                                 'assignee': 'sam'}).json()
    quiet = [client.post('/api/v1/tickets', json={'summary': f'q{n}', 'group_scope': 'home',
                                                   'assignee': 'sam'}).json() for n in range(3)]
    for q in quiet:
        age('ticket', q['id'], 30)
    for n in range(40):
        client.post(f"/api/v1/tickets/{busy['id']}/comments", json={'body': f'update {n}'})
    body = feed(sam, limit=4)
    assert cards(body) == [('ticket', busy['id'])] + [('ticket', q['id']) for q in reversed(quiet)]
    assert body['items'][0]['comments'] == 40


def test_read_keeps_the_card_and_new_activity_from_others_unreads_it(client):
    sam = user(client, 'sam', {'home': 'editor'})
    t = client.post('/api/v1/tickets', json={'summary': 't', 'group_scope': 'home', 'assignee': 'sam'}).json()
    start_feed(sam)
    ref = {'kind': 'ticket', 'id': t['id']}
    assert feed(sam)['items'][0]['unread']
    assert db.query_one("SELECT COUNT(*) AS n FROM notifications WHERE recipient='sam' AND read=0")['n'] > 0

    r = sam.post('/api/v1/inbox/marks', json={'items': [ref], 'read': True})
    assert r.status_code == 200, r.text
    after = feed(sam)
    assert cards(after) == [('ticket', t['id'])] and not after['items'][0]['unread']
    assert after['counts']['unread'] == 0
    # the badge agrees: sam's rows for that ticket are read
    assert db.query_one("SELECT COUNT(*) AS n FROM notifications WHERE recipient='sam' AND read=0")['n'] == 0

    db.execute("UPDATE inbox_marks SET read_at = datetime('now', '-5 minutes')")
    sam.post(f"/api/v1/tickets/{t['id']}/comments", json={'body': 'my own note'})
    assert not feed(sam)['items'][0]['unread']          # your own activity never unreads
    client.post(f"/api/v1/tickets/{t['id']}/comments", json={'body': 'a reply'})
    assert feed(sam)['items'][0]['unread']

    sam.post('/api/v1/inbox/marks', json={'items': [ref], 'read': True})
    sam.post('/api/v1/inbox/marks', json={'items': [ref], 'read': False})
    assert feed(sam)['items'][0]['unread']               # mark unread


def test_first_visit_counts_what_was_there_as_read(client):
    sam = user(client, 'sam', {'home': 'editor'})
    t = client.post('/api/v1/tickets', json={'summary': 'old', 'group_scope': 'home', 'assignee': 'sam'}).json()
    age('ticket', t['id'], 10)
    body = feed(sam)
    assert cards(body) == [('ticket', t['id'])] and not body['items'][0]['unread']


def test_star_pins_a_card_past_the_window_and_read_all_keeps_stars(client):
    sam = user(client, 'sam', {'home': 'editor'})
    t = client.post('/api/v1/tickets', json={'summary': 'keep', 'group_scope': 'home', 'assignee': 'sam'}).json()
    sam.post('/api/v1/inbox/marks', json={'items': [{'kind': 'ticket', 'id': t['id']}], 'starred': True})
    age('ticket', t['id'], 60 * 24 * 40)
    assert cards(feed(sam)) == []                         # out of the week's window
    assert cards(feed(sam, filter='starred')) == [('ticket', t['id'])]
    r = sam.post('/api/v1/inbox/read-all')
    assert r.status_code == 200, r.text
    assert cards(feed(sam, filter='starred')) == [('ticket', t['id'])]


def test_scope_holds_even_for_a_starred_card(client):
    sam = user(client, 'sam', {'home': 'editor'})
    user(client, 'work-peer', {'work': 'editor'})
    hidden = client.post('/api/v1/tickets', json={'summary': 'work only', 'group_scope': 'work'}).json()
    d = client.post('/api/v1/docs', json={'title': 'work doc', 'body': 'b', 'group_scope': 'work'}).json()
    sam.post('/api/v1/inbox/marks', json={'items': [{'kind': 'ticket', 'id': hidden['id']},
                                                    {'kind': 'doc', 'id': d['id']}], 'starred': True})
    for f in ('mine', 'all', 'starred'):
        assert cards(feed(sam, filter=f)) == []
    assert ('ticket', hidden['id']) in cards(feed(client, filter='all'))


def test_the_feed_and_its_marks_notify_no_one(client):
    sam = user(client, 'sam', {'home': 'editor'})
    t = client.post('/api/v1/tickets', json={'summary': 't', 'group_scope': 'home', 'assignee': 'sam'}).json()
    before = db.query_one("SELECT COUNT(*) AS n FROM notifications")['n']
    feed(sam, filter='all')
    sam.post('/api/v1/inbox/marks', json={'items': [{'kind': 'ticket', 'id': t['id']}], 'read': True, 'starred': True})
    sam.post('/api/v1/inbox/read-all')
    assert db.query_one("SELECT COUNT(*) AS n FROM notifications")['n'] == before


def test_bad_input_is_refused(client):
    assert client.get('/api/v1/inbox/feed', params={'kind': 'thread'}).status_code == 400
    assert client.get('/api/v1/inbox/feed', params={'filter': 'handled'}).status_code == 400
    assert client.post('/api/v1/inbox/marks', json={'items': [{'kind': 'ticket', 'id': 1}]}).status_code == 400
    assert client.post('/api/v1/inbox/marks', json={'items': [{'kind': 'x', 'id': 1}], 'read': True}).status_code == 400
    assert client.post('/api/v1/inbox/marks', json={'items': [], 'read': True}).status_code == 422
