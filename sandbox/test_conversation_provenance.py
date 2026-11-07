#!/usr/bin/env python3
"""#932: pure rail receiver filtering and rendered origin, with no network."""
import hashlib
import importlib.util
from pathlib import Path
from unittest.mock import Mock

root = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('rail_provenance', root / 'registry/helpers/samcloud-rail.py')
rail = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rail)
rail._api = Mock(side_effect=AssertionError('unexpected network'))
rail._whoami = lambda: 'services'
rail._thread_info = lambda key: {'root': 'task:801', 'owners': ['services']}
rail._held_by_surface = lambda key: None
rail._object_open = lambda key: True
rail._resolve_entry = lambda key, reg: reg.get(key)
rail.holder_alive = lambda entry: True
rail._row_owner = lambda row: 'services'
rail._footer = lambda key: 'FOOTER'
rail._thread_title = lambda key, rows: key
fp = lambda s: hashlib.sha256(s.encode()).hexdigest()[:8]
entry = {'sid': 'B', 'key': 'thread-2', 'state': 'open'}
reg = {'thread-2': entry}
sibling = {'id': 1, 'actor': 'services', 'thread_id': 2, 'task_id': 801,
           'event_type': 'task.commented', 'created_at': '2026-10-09 21:00:00',
           'payload': {'body': 'Sibling work', 'actor_session': fp('A'),
                       'actor_home_thread_id': 1, 'recipient_session': fp('B')}}
echo = {**sibling, 'id': 2, 'payload': {**sibling['payload'], 'actor_session': fp('B')}}
unknown = {**sibling, 'id': 3, 'payload': {'body': 'Unknown'}}
peer = {**sibling, 'id': 4, 'actor': 'peer', 'payload': {'body': 'Peer work'}}
assert rail._receiver_rows([sibling, echo, unknown, peer], entry, 'services') == [sibling, peer]
assert rail._receiver_rows([sibling], {'sid': 'A'}, 'services') == []
assert rail._receiver_rows([sibling], {'sid': 'C'}, 'services') == []
assert rail._receiver_rows([sibling], None, 'services') == []
assert rail.deliverable('thread-2', [sibling], reg, 'services')
assert not rail.deliverable('thread-2', [sibling], {}, 'services')
assert not rail.deliverable('thread-2', [echo], reg, 'services')
text = rail.continue_prompt('thread-2', [sibling])
assert text.startswith('You are services on thread 2.')
assert 'services@1 [' + fp('A') + ']' in text
assert 'Sibling work' in text
text = rail.continue_prompt('thread-3', [sibling], home_key='thread-2')
assert text.startswith('You are services on thread 2.')
assert rail.wake_prompt('thread-2', [sibling], False).startswith('You are services on thread 2.')
assert rail._author_label('services', fp('A'), None) == 'services [' + fp('A') + ']'
assert rail._author_label('services') == 'services'
rail._api.assert_not_called()
print('ALL PASSED: conversation provenance, sibling routing, mixed batches and copy')
