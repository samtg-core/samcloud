"""One-off contributions, shared-context framing and literal wake configuration."""
import ast
import json
import os
from pathlib import Path
import subprocess
import pytest

from test_registry import client
from test_delivery_context import agent, inbox

REPO = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize('kind', ['ticket', 'doc', 'task'])
def test_comment_alone_does_not_enrol_but_direct_ask_still_reaches(client, kind):
    reader = agent(client, 'reader')
    if kind == 'task':
        project = client.post('/api/v1/projects', json={'name': 'work'}).json()['slug']
        made = client.post(f'/api/v1/projects/{project}/tasks', json={'title': 'work'}).json()
    else:
        made = client.post(f'/api/v1/{kind}s', json=(
            {'summary': 'work'} if kind == 'ticket' else {'title': 'work', 'body': 'brief'})).json()
    url = f"/api/v1/{kind}s/{made['id']}"
    posted = reader.post(url + '/comments', json={'body': 'one contribution'},
                         headers={'X-Session-Id': 'visiting-context'})
    assert posted.status_code == 200
    if kind != 'task':
        assert posted.json()['subscribed'] is False
        assert 'reader' not in {w['identity'] for w in client.get(url + '/watchers').json()['watchers']}
    assert client.get(url).json()['comments'][-1]['session_fp']
    # The owner still hears the contribution; the visiting writer hears no later chatter.
    assert any(r['payload'].get('body') == 'one contribution' for r in inbox(client))
    client.post(url + '/comments', json={'body': 'unaddressed followup'})
    assert inbox(reader) == []
    client.post(url + '/comments', json={'body': '@reader a specific question'})
    assert len(inbox(reader)) == 1
    assert inbox(reader)[0]['event_type'] == kind + '.mentioned'


def functions(*names):
    tree = ast.parse((REPO / 'registry/helpers/samcloud-rail.py').read_text())
    module = ast.Module(body=[n for n in tree.body if isinstance(n, ast.FunctionDef)
                             and n.name in names], type_ignores=[])
    namespace = {'Path': Path}
    exec(compile(module, '<rail-functions>', 'exec'), namespace)
    return namespace


def test_wake_env_inline_comment_is_not_part_of_park_value(tmp_path):
    f = tmp_path / 'wake.env'
    f.write_text('RAIL_DRY=1 # maintenance\nQUOTED="1" # maintenance\n'
                 "SINGLE='a # literal' # note\nURL=https://example.test/#fragment\n"
                 "CMD=hermes -z '{prompt}' --yolo # note\n"
                 'BAD=$(touch nope)\nBAD2=`touch nope`\nBROKEN="unterminated\n')
    parsed = functions('_read_env_file')['_read_env_file'](f)
    assert parsed == {'RAIL_DRY': '1', 'QUOTED': '1', 'SINGLE': 'a # literal',
                      'URL': 'https://example.test/#fragment',
                      'CMD': "hermes -z '{prompt}' --yolo"}


def test_old_root_does_not_name_current_work():
    ns = functions('thread_ref', '_row_ref', '_thread_title', '_root_block')
    ns['_thread_info'] = lambda key: {'root': 'task:1', 'title': 'old app release'}
    rows = [{'id': 8, 'task_id': 2, 'payload': {'title': 'revive immersive project',
             'root': {'kind': 'task', 'id': 1, 'title': 'old app release', 'read': 'GET /tasks/1'}}}]
    assert ns['thread_ref']('thread-99') == 'thread 99'
    title = ns['_thread_title']('thread-99', rows)
    assert 'task #2' in title and 'revive immersive project' in title
    assert 'old app release' not in title
    assert 'background, not an assignment' in ns['_root_block'](rows)
    untitled = {'id': 9, 'doc_id': 3, 'payload': {'body': 'a new topic'}}
    assert ns['_thread_title']('thread-99', rows + [untitled]) == 'thread 99'


def test_say_displays_server_notes_without_another_post(tmp_path):
    fake = tmp_path / 'curl'
    reply = json.dumps({'comment_id': 12, 'notes': ['outsider was not notified'],
                        'thread_notice': 'shared context can continue'}) + '\n201\n'
    fake.write_text('#!/usr/bin/env python3\nimport sys\nsys.stdin.read()\nprint(' + repr(reply) + ', end="")\n')
    fake.chmod(0o700)
    hdr = tmp_path / 'token.hdr'
    hdr.write_text('Authorization: Bearer fixture\n')
    env = dict(os.environ, PATH=str(tmp_path) + os.pathsep + os.environ['PATH'],
               SAMCLOUD_API='http://127.0.0.1:9/api/v1', SAMCLOUD_AUTH_HDR=str(hdr))
    result = subprocess.run(['bash', str(REPO / 'registry/helpers/samcloud-say'), 'ticket', '1'],
                            input='one message', text=True, capture_output=True, env=env, timeout=10)
    assert result.returncode == 0, result.stderr
    assert 'posted comment 12' in result.stdout
    assert 'outsider was not notified' in result.stdout
    assert 'shared context can continue' in result.stdout
