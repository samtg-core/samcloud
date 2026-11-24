"""Doc 752: a service acts as itself for its own compute, and with its caller's
access for anything it writes.

A. A service token sees and leases the resources of its own service's device,
   as that service, and nothing else.
B. A caller mints a grant naming one service and one group it edits; the
   service uploads with its own token plus the grant; the file is the caller's.
"""
import io
from datetime import datetime, timedelta, timezone

import db
import enrollment
from test_registry import client, AuthedClient, _register_device, _register_service
from test_auth_boundaries import user


def _setup(client):
    """caller: editor of projects, viewer of services. other: editor of work.
    box (in services) runs box/render and box/other; far (in work) runs nothing."""
    caller, _ = user(client, 'caller', {'projects': 'editor', 'services': 'viewer'})
    other, _ = user(client, 'other', {'work': 'editor'})
    for d, g in (('box', 'services'), ('far', 'work')):
        assert _register_device(client, d).status_code == 200
        assert client.post(f'/api/v1/groups/{g}/devices', json={'device_id': d}).status_code == 200
        r = client.post('/api/v1/resources', json={
            'device_id': d, 'name': 'gpu', 'type': 'gpu', 'provider': 'metal',
            'specs': {'unified_memory_mb': 1000}, 'allocation_mode': 'shared'})
        assert r.status_code == 200, r.text
    for name, port in (('render', 8798), ('other', 8799)):
        assert _register_service(client, name, 'box', port).status_code == 200
    svc = AuthedClient(client._client, enrollment.generate_service_token('box/render'))
    svc2 = AuthedClient(client._client, enrollment.generate_service_token('box/other'))
    return caller, other, svc, svc2


def _upload(c, **form):
    return c.post('/api/v1/files', files={'file': ('sheet.png', io.BytesIO(b'\x89PNG..'), 'image/png')},
                  data=form)


class TestServiceLeasesItsOwnDevice:
    def test_a_service_token_leases_its_own_device_as_itself(self, client):
        _, _, svc, _ = _setup(client)
        assert svc.get('/api/v1/resources/box/gpu').status_code == 200
        assert 'box/gpu' in [r['id'] for r in svc.get('/api/v1/resources').json()]
        r = svc.post('/api/v1/resources/box/gpu/leases', json={'memory_mb': 100, 'ttl_seconds': 60})
        assert r.status_code == 200, r.text
        lease = svc.get(f"/api/v1/leases/{r.json()['lease_id']}").json()
        assert lease['agent_id'] == 'box/render' and lease['service_id'] == 'box/render'
        assert svc.post(f"/api/v1/leases/{lease['id']}/renew", json={'ttl_seconds': 120}).status_code == 200
        assert svc.delete(f"/api/v1/leases/{lease['id']}").status_code == 200

    def test_it_sees_no_other_device_and_leases_as_no_other_service(self, client):
        _, _, svc, _ = _setup(client)
        assert svc.get('/api/v1/resources/far/gpu').status_code == 404
        assert svc.post('/api/v1/resources/far/gpu/leases', json={'memory_mb': 1}).status_code == 404
        assert 'far/gpu' not in [r['id'] for r in svc.get('/api/v1/resources').json()]
        r = svc.post('/api/v1/resources/box/gpu/leases', json={'memory_mb': 1, 'service_id': 'box/other'})
        assert r.status_code == 403

    def test_a_seat_still_leases_as_before(self, client):
        _, other, _, _ = _setup(client)
        r = other.post('/api/v1/resources/far/gpu/leases', json={'memory_mb': 1, 'ttl_seconds': 60})
        assert r.status_code == 200, r.text
        assert other.get('/api/v1/resources/box/gpu').status_code == 404


class TestCallerGrants:
    def test_the_service_uploads_on_the_grant_and_the_file_is_the_callers(self, client):
        caller, other, svc, _ = _setup(client)
        r = caller.post('/api/v1/grants', json={'service_id': 'box/render', 'group_scope': 'projects'})
        assert r.status_code == 200, r.text
        grant = r.json()['grant']
        assert grant.startswith('sc_grant_') and r.json()['group_scope'] == 'projects'
        # Without the grant the service cannot reach the caller's group at all.
        assert _upload(svc, group_scope='projects').status_code == 403
        up = _upload(svc, grant=grant)
        assert up.status_code == 200, up.text
        f = up.json()
        assert (f['group_scope'], f['owner'], f['uploaded_by']) == ('projects', 'caller', 'box/render')
        # The caller owns it: reads, re-scopes, deletes. An outsider cannot read it.
        assert caller.get(f"/api/v1/files/{f['id']}").status_code == 200
        assert other.get(f"/api/v1/files/{f['id']}").status_code == 404
        assert caller.patch(f"/api/v1/files/{f['id']}", json={'group_scope': 'projects'}).status_code == 200
        assert caller.delete(f"/api/v1/files/{f['id']}").status_code == 200
        listed = caller.get('/api/v1/grants').json()['grants']
        assert listed[0]['uses'] == 1 and 'grant' not in listed[0] and 'token_hash' not in listed[0]

    def test_a_grant_is_nothing_without_the_named_services_token(self, client):
        caller, _, svc, svc2 = _setup(client)
        grant = caller.post('/api/v1/grants', json={'service_id': 'box/render', 'group_scope': 'projects'}).json()['grant']
        assert _upload(svc2, grant=grant).status_code == 403       # another service
        assert _upload(caller, grant=grant).status_code == 403     # the caller's own bearer
        assert _upload(svc, grant='sc_grant_made-up').status_code == 403
        assert _upload(svc, grant=grant, group_scope='work').status_code == 422

    def test_expired_revoked_or_demoted_grants_refuse(self, client):
        caller, _, svc, _ = _setup(client)
        mint = lambda: caller.post('/api/v1/grants', json={'service_id': 'box/render', 'group_scope': 'projects'}).json()
        g = mint()
        past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        db.execute("UPDATE service_grants SET expires_at = ? WHERE id = ?", (past, g['id']))
        assert _upload(svc, grant=g['grant']).status_code == 403
        g = mint()
        assert caller.delete(f"/api/v1/grants/{g['id']}").status_code == 200
        assert _upload(svc, grant=g['grant']).status_code == 403
        g = mint()
        uid = db.query_one("SELECT id FROM users WHERE username = 'caller'")['id']
        assert client.patch(f'/api/v1/groups/projects/members/{uid}', json={'role': 'viewer'}).status_code == 200
        assert _upload(svc, grant=g['grant']).status_code == 403

    def test_who_may_mint_and_revoke(self, client):
        caller, other, svc, _ = _setup(client)
        assert svc.post('/api/v1/grants', json={'service_id': 'box/render'}).status_code == 403
        assert caller.post('/api/v1/grants', json={'service_id': 'box/render', 'group_scope': 'services'}).status_code == 403
        assert other.post('/api/v1/grants', json={'service_id': 'box/render'}).status_code == 404
        assert caller.post('/api/v1/grants', json={'service_id': 'box/render', 'ttl_s': 10 ** 7}).status_code == 422
        g = caller.post('/api/v1/grants', json={'service_id': 'box/render'}).json()
        assert g['group_scope'] == 'projects'   # the caller's default group
        assert other.delete(f"/api/v1/grants/{g['id']}").status_code == 404
        assert other.get('/api/v1/grants').json()['grants'] == []
