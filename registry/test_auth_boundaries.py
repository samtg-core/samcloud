"""Real HTTP regressions for invite, device identity, enrollment and routing."""
import pytest
from unittest.mock import Mock
import db, enrollment, main, scope
from test_registry import client, AuthedClient, _register_device, _register_service


def user(c, name, roles):
    for g in roles:
        if not db.query_one('SELECT 1 FROM groups WHERE id=?', (g,)):
            assert c.post('/api/v1/groups', json={'id':g, 'name':g}).status_code == 200
    r=c.post('/api/v1/users', json={'username':name,'password':'test-pw','role':'user',
        'scopes':['group:'+g for g in roles], 'group_roles':roles})
    assert r.status_code == 200, r.text
    a=c.post('/api/v1/auth/login',json={'username':name,'password':'test-pw'})
    return AuthedClient(c._client,a.json()['token']),r.json()['user_id']


def counts():
    return {t:db.query_one('SELECT count(*) AS n FROM '+t)['n'] for t in
            ('users','tokens','group_members','devices','group_devices','ports')}


@pytest.mark.parametrize('extra,status',[
    ({'role':'admin'},403), ({'role':'invented'},400),
    ({'group_roles':{'work':'owner'}},403), ({'group_roles':{'work':'bad'}},400),
    ({'group_roles':{'elsewhere':'viewer'}},400),
    ({'scopes':['*']},403), ({'scopes':['admin']},403),
    ({'scopes':['group:elsewhere']},403)])
def test_rejected_invite_writes_nothing(client,extra,status):
    editor,_=user(client,'editor',{'work':'editor'}); before=counts()
    r=editor.post('/api/v1/users/invite',json={'username':'newcomer','role':'agent',
        'scopes':['group:work'],**extra})
    assert r.status_code == status,r.text
    assert counts() == before


@pytest.mark.parametrize('role',['editor','owner'])
def test_inviter_can_grant_own_group_role(client,role):
    editor,_=user(client,'editor',{'work':role})
    r=editor.post('/api/v1/users/invite',json={'username':'newcomer','role':'user',
        'scopes':['group:work'],'group_roles':{'work':role}})
    assert r.status_code == 200,r.text
    a=client.post('/api/v1/users/activate',json={'token':r.json()['token'],'password':'pw'})
    auth=main.get_auth('Bearer '+a.json()['token'])
    assert scope.role_in(auth,'work') == role and not auth.is_admin()


def test_admin_grants_still_work_and_unknown_group_leaves_no_user(client):
    before=counts()
    r=client.post('/api/v1/users/invite',json={'username':'bad','scopes':['group:absent']})
    assert r.status_code == 400 and counts() == before
    r=client.post('/api/v1/users/invite',json={'username':'operator','role':'admin','scopes':['*']})
    assert r.status_code == 200
    a=client.post('/api/v1/users/activate',json={'token':r.json()['token'],'password':'pw'})
    assert main.get_auth('Bearer '+a.json()['token']).is_admin()


@pytest.mark.parametrize('groups',[None,[],['work']])
def test_viewer_cannot_invite_or_enroll_even_with_empty_grants(client,groups):
    viewer,_=user(client,'viewer',{'work':'viewer'}); before=counts()
    assert viewer.post('/api/v1/users/invite',json={'username':'new','scopes':[]}).status_code == 403
    assert viewer.post('/api/v1/enroll',json={'device_id':'new-box','group_ids':groups}).status_code == 403
    assert counts() == before


@pytest.mark.parametrize('groups',[['private'],['work','private'],['absent']])
def test_enrollment_checks_all_groups_before_writes(client,groups):
    editor,_=user(client,'editor',{'work':'editor','private':'viewer'}); before=counts()
    r=editor.post('/api/v1/enroll',json={'device_id':'new-box','access_target':'reverse-tunnel','group_ids':groups})
    assert r.status_code == 403 and counts() == before


def test_enrollment_default_skips_read_only_groups_and_admin_can_choose_any(client):
    editor,_=user(client,'editor',{'work':'editor','private':'viewer'})
    r=editor.post('/api/v1/enroll',json={'device_id':'new-box','access_route':'browser-only'})
    assert r.status_code == 200 and r.json()['groups'] == ['work']
    r=client.post('/api/v1/enroll',json={'device_id':'other-box','group_ids':['private']})
    assert r.status_code == 200 and r.json()['groups'] == ['private']
    before=counts()
    assert client.post('/api/v1/enroll',json={'device_id':'bad','group_ids':['absent']}).status_code == 400
    assert counts() == before


@pytest.mark.parametrize('name,status',[('victim',409),('user:3',422),('host/service',422)])
def test_device_collisions_rejected_before_writes(client,name,status):
    editor,_=user(client,'editor',{'work':'editor'}); user(client,'victim',{'private':'owner'})
    before=counts()
    assert editor.post('/api/v1/enroll',json={'device_id':name,'access_target':'reverse-tunnel'}).status_code == status
    assert client.post('/api/v1/devices',json={'id':name}).status_code == status
    assert counts() == before


def test_device_first_cannot_become_user_through_either_creation_route(client):
    assert _register_device(client,'existing-box').status_code == 200
    for path,body in [('/users',{'username':'existing-box','password':'pw'}),
                      ('/users/invite',{'username':'existing-box','scopes':[]})]:
        before=counts()
        assert client.post('/api/v1'+path,json=body).status_code == 409
        assert counts() == before


def test_old_bare_device_cannot_impersonate_user_or_read_inbox(client):
    victim,_=user(client,'victim',{'private':'owner'})
    doc=victim.post('/api/v1/docs',json={'title':'secret','body':'private','group_scope':'private'}).json()['id']
    db.execute("INSERT INTO devices(id,name,context) VALUES ('victim','old device','samcloud')")
    device=AuthedClient(client._client,enrollment.generate_device_token('victim'))
    assert device.get('/api/v1/docs/'+str(doc)).status_code == 401
    assert device.get('/api/v1/notifications').status_code == 401
    assert scope.memberships(main.AuthContext('device','victim',[])) == {}
    legacy=AuthedClient(client._client,enrollment.generate_agent_token('victim',scopes=[]))
    assert legacy.get('/api/v1/docs/'+str(doc)).status_code == 200
    assert victim.get('/api/v1/docs/'+str(doc)).status_code == 200


def test_old_reserved_device_refused_but_password_session_and_normal_tokens_work(client):
    victim,uid=user(client,'victim',{'private':'owner'}); name='user:'+str(uid)
    db.execute('INSERT INTO devices(id,name,context) VALUES (?,?,?)',(name,'old device','samcloud'))
    stolen=AuthedClient(client._client,enrollment.generate_device_token(name))
    assert stolen.get('/api/v1/auth/verify').status_code == 401
    assert victim.get('/api/v1/auth/verify').status_code == 200
    assert victim.get('/api/v1/auth/verify').json()['groups'] == {'private':'owner'}
    assert _register_device(client,'ordinary-box').status_code == 200
    normal=AuthedClient(client._client,enrollment.generate_device_token('ordinary-box'))
    assert normal.get('/api/v1/auth/verify').status_code == 200
    assert client.get('/api/v1/auth/verify').status_code == 200


def test_route_activation_is_scoped_before_any_external_effect(client,monkeypatch):
    editor,_=user(client,'editor',{'work':'editor'}); viewer,_=user(client,'viewer',{'work':'viewer'})
    outsider,_=user(client,'outsider',{'elsewhere':'editor'})
    assert _register_device(client,'box').status_code == 200
    assert client.post('/api/v1/groups/work/devices',json={'device_id':'box'}).status_code == 200
    assert _register_service(client,'web','box',8080,slice_port=18001,subdomain='app',domain='example.org').status_code == 200
    forward,caddy,dns=Mock(return_value=(True,'ok')),Mock(return_value=(True,'ok')),Mock(return_value={'success':True})
    monkeypatch.setattr(main.forward,'start_forward',forward)
    monkeypatch.setattr(main.caddy,'append_route',caddy); monkeypatch.setattr(main,'_dns_record',dns)
    for caller,status in [(outsider,404),(viewer,403)]:
        assert caller.post('/api/v1/routes/app/activate?domain=example.org').status_code == status
        assert not forward.called and not caddy.called and not dns.called
    assert editor.post('/api/v1/routes/app/activate?domain=example.org').status_code == 200
    service=AuthedClient(client._client,enrollment.generate_service_token('box/web'))
    assert service.post('/api/v1/routes/app/activate?domain=example.org').status_code == 200
    assert client.post('/api/v1/routes',json={'subdomain':'bare','domain':'example.org','target':'localhost:99'}).status_code == 200
    assert editor.post('/api/v1/routes/bare/activate?domain=example.org').status_code == 403
    assert client.post('/api/v1/routes/bare/activate?domain=example.org').status_code == 200


def test_account_events_are_private_to_account_and_admin(client):
    from events import Event
    one,uid=user(client,'one',{'work':'editor'}); two,_=user(client,'two',{'other':'editor'})
    event=Event('user.activated','user:'+str(uid),{'username':'one','role':'user','scopes':['group:work']})
    assert main._can_see_event(main.get_auth(one._headers['Authorization']),event)
    assert not main._can_see_event(main.get_auth(two._headers['Authorization']),event)
    assert main._can_see_event(main.get_auth(client._headers['Authorization']),event)


@pytest.mark.parametrize('kind',['resource.deleted','lease.granted','lease.expired'])
def test_compute_events_do_not_become_global_when_unscoped(client,kind):
    from events import Event
    viewer,_=user(client,'viewer',{'work':'viewer'}); outsider,_=user(client,'outsider',{'other':'editor'})
    assert _register_device(client,'box').status_code == 200
    assert client.post('/api/v1/groups/work/devices',json={'device_id':'box'}).status_code == 200
    # Tombstone: the row has already gone, but its device scope is preserved.
    event=Event(kind,'box/gpu',{'resource_id':'box/gpu'},scope='device:box')
    assert main._can_see_event(main.get_auth(viewer._headers['Authorization']),event)
    assert not main._can_see_event(main.get_auth(outsider._headers['Authorization']),event)
    # A legacy event lacking an anchor is refused, not broadcast.
    event.scope=''
    assert not main._can_see_event(main.get_auth(outsider._headers['Authorization']),event)


def test_normal_editor_enrollment_activation_keeps_own_group_only(client):
    editor,_=user(client,'editor',{'work':'editor'})
    private,_=user(client,'private-user',{'private':'owner'})
    own=editor.post('/api/v1/docs',json={'title':'own','body':'work','group_scope':'work'}).json()['id']
    secret=private.post('/api/v1/docs',json={'title':'secret','body':'secret','group_scope':'private'}).json()['id']
    r=editor.post('/api/v1/enroll',json={'device_id':'new-box','access_target':'reverse-tunnel'})
    assert r.status_code == 200,r.text
    a=client.post('/api/v1/devices/activate',json={'token':r.json()['token'],'ssh_pubkey':'ssh-ed25519 AAAAclientkey'})
    assert a.status_code == 200,a.text
    device=AuthedClient(client._client,a.json()['device_token'])
    assert device.get('/api/v1/docs/'+str(own)).status_code == 200
    assert device.get('/api/v1/docs/'+str(secret)).status_code == 404


def test_existing_ambiguous_device_cannot_reactivate_and_replace_user_token(client):
    _,uid=user(client,'victim',{'private':'owner'}); name='user:'+str(uid)
    human=enrollment.generate_device_token(name,scopes=['group:private'])
    saved=db.query_one('SELECT token_hash FROM tokens WHERE id=?',('device:'+name,))['token_hash']
    db.execute('INSERT INTO devices(id,name,context) VALUES (?,?,?)',(name,'old device','samcloud'))
    invite=enrollment.generate_enrollment_token(name,'samcloud')
    r=client.post('/api/v1/devices/activate',json={'token':invite,'ssh_pubkey':'ssh-ed25519 AAAAclientkey'})
    assert r.status_code == 422
    assert db.query_one('SELECT token_hash FROM tokens WHERE id=?',('device:'+name,))['token_hash'] == saved


def test_real_resource_deletion_preserves_scope_for_event_visibility(client,monkeypatch):
    viewer,_=user(client,'viewer',{'work':'viewer'}); outsider,_=user(client,'outsider',{'elsewhere':'viewer'})
    assert _register_device(client,'box').status_code == 200
    assert client.post('/api/v1/groups/work/devices',json={'device_id':'box'}).status_code == 200
    made=client.post('/api/v1/resources',json={'name':'gpu','device_id':'box','type':'gpu','provider':'example'})
    assert made.status_code == 200,made.text
    captured=[]
    from events import Event
    monkeypatch.setattr(main.event_bus,'emit',lambda kind,target,data,scope='':captured.append(Event(kind,target,data,scope)))
    assert client.delete('/api/v1/resources/box/gpu').status_code == 200
    event=next(e for e in captured if e.type=='resource.deleted')
    assert event.scope == 'device:box'
    assert main._can_see_event(main.get_auth(viewer._headers['Authorization']),event)
    assert not main._can_see_event(main.get_auth(outsider._headers['Authorization']),event)
