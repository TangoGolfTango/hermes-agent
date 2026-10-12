"""Kanban worker failures reach canonical Bot Chat through durable receipts (#69)."""
import argparse
import asyncio
import os
import subprocess
import sys

import pytest
from pathlib import Path
from types import SimpleNamespace

from gateway.config import Platform
from gateway.kanban_watchers_notifier import _KanbanNotification, _notifier_collect
from gateway.run import GatewayRunner
from hermes_cli import kanban, kanban_db as kb, kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd, kanban_db_notify as kbn
from tools import bot_live_delivery as live


def test_worker_crash_notifies_bot_chat_once_after_settled_receipt(tmp_path, monkeypatch):
    home = tmp_path / '.hermes'
    home.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setattr(kb, '_resolve_crash_grace_seconds', lambda: 0)
    owner = dict(profile_home=str(home), session_id='canonical', lease_id='lease', live_session_id='live')
    monkeypatch.setattr(live, 'find_canonical_live_owner', lambda h: owner)
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.adapters = {}
    runner._profile_adapters = {}
    runner._kanban_dispatcher_lock_handle = None
    runner._kanban_notifier_profile = 'default'
    runner.config = SimpleNamespace(multiplex_profiles=False)
    parser = argparse.ArgumentParser()
    kanban.build_parser(parser.add_subparsers(dest='command'))
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title='Disposable worker crash', assignee='default')
    args = parser.parse_args(['kanban', 'notify-subscribe', tid, '--platform', 'bot-chat',
                              '--chat-id', 'default', '--notifier-profile', 'default'])
    assert kanban.kanban_command(args) == 0

    def spawn(task, workspace, **kwargs):
        env = {k: v for k, v in os.environ.items() if not k.startswith('HERDR_')}
        child = subprocess.Popen([sys.executable, '-c', 'raise SystemExit(1)'], env=env)
        child.wait()
        return child.pid

    with kbc.connect() as conn:
        assert kbd.dispatch_once(conn, spawn_fn=spawn, max_spawn=1).spawned
        result = kbd.dispatch_once(conn, max_spawn=0, failure_limit=1)
        assert result.crashed == [tid]

    def collect():
        return _notifier_collect(runner, kb, notifier_profile='default', gc_due=False, gc_retention_days=30)

    batches = collect()
    assert len(batches) == 1, 'bot-chat worker failure was never claimed'
    asyncio.run(_KanbanNotification(runner, batches[0], platform_cls=Platform, sub_fail_counts={}).deliver())
    # Admission is not completion: the cursor must remain retryable.
    retry = collect()
    assert len(retry) == 1
    asyncio.run(_KanbanNotification(runner, retry[0], platform_cls=Platform, sub_fail_counts={}).deliver())
    tickets = list((home / 'runtime' / 'bot_live_delivery').glob('*.json'))
    assert tickets
    for path in tickets:
        receipt = live.read_delivery_result(home, path.stem)
        assert receipt['status'] == 'queued'
        assert tid in receipt['message'] and 'Kanban' in receipt['message']
        assert 'Cronjob' not in receipt['message']
        assert receipt['notification_category'] == 'diagnostic'
        assert live.claim_pending_delivery(home, owner)
        live.complete_delivery(home, path.stem, status='settled', reply='Observed worker failure')
    # A new owner must never receive a replay of an already admitted event.
    monkeypatch.setattr(live, 'find_canonical_live_owner', lambda h: {**owner, 'lease_id': 'new-owner'})
    batches = collect()
    asyncio.run(_KanbanNotification(runner, batches[0], platform_cls=Platform, sub_fail_counts={}).deliver())
    assert collect() == []
    assert len(list((home / 'runtime' / 'bot_live_delivery').glob('*.json'))) == len(tickets)


def test_bot_chat_subscription_cannot_target_a_different_profile(tmp_path, monkeypatch):
    home = tmp_path / '.hermes'
    home.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.adapters = {}
    runner._profile_adapters = {}
    runner._kanban_dispatcher_lock_handle = None
    runner._kanban_notifier_profile = 'default'
    runner.config = SimpleNamespace(multiplex_profiles=False)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title='Wrong owner', assignee='default')
        kbn.add_notify_sub(conn, task_id=tid, platform='bot-chat', chat_id='other', notifier_profile='default')
        kb.block_task(conn, tid, reason='fixture')
        before = kbn.list_notify_subs(conn, tid)[0]['last_event_id']
    assert _notifier_collect(runner, kb, notifier_profile='default', gc_due=False, gc_retention_days=30) == []
    with kbc.connect() as conn:
        assert kbn.list_notify_subs(conn, tid)[0]['last_event_id'] == before
    assert not (home / 'runtime' / 'bot_live_delivery').exists()


def _pending_notification(tmp_path, monkeypatch):
    home = tmp_path / '.hermes'
    home.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    owner = dict(profile_home=str(home), session_id='canonical', lease_id='lease', live_session_id='live')
    monkeypatch.setattr(live, 'find_canonical_live_owner', lambda h: owner)
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.adapters = {}
    runner._profile_adapters = {}
    runner._kanban_dispatcher_lock_handle = None
    runner._kanban_notifier_profile = 'default'
    runner.config = SimpleNamespace(multiplex_profiles=False)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title='Original task title', assignee='default')
        kbn.add_notify_sub(conn, task_id=tid, platform='bot-chat', chat_id='default', notifier_profile='default')
        kb.block_task(conn, tid, reason='Worker fixture failure')

    def tick():
        batches = _notifier_collect(runner, kb, notifier_profile='default', gc_due=False, gc_retention_days=30)
        for batch in batches:
            asyncio.run(_KanbanNotification(runner, batch, platform_cls=Platform, sub_fail_counts={}).deliver())
        return batches

    tick()
    ticket, = (home / 'runtime' / 'bot_live_delivery').glob('*.json')
    return home, owner, tid, ticket.stem, tick


def test_queued_owner_change_cancels_without_replaying(tmp_path, monkeypatch, caplog):
    home, owner, _tid, key, tick = _pending_notification(tmp_path, monkeypatch)
    monkeypatch.setattr(live, 'find_canonical_live_owner', lambda h: {**owner, 'lease_id': 'replacement'})
    tick()
    receipt = live.read_delivery_result(home, key)
    assert receipt['status'] == 'cancelled'
    assert receipt['reason'] == 'owner_changed'
    assert key in caplog.text and 'cancelled' in caplog.text
    assert tick() == []
    assert len(list((home / 'runtime' / 'bot_live_delivery').glob('*.json'))) == 1


@pytest.mark.parametrize("status", ["failed", "cancelled", "ambiguous"])
def test_terminal_failed_receipt_has_observable_final_disposition(tmp_path, monkeypatch, caplog, status):
    home, owner, _tid, key, tick = _pending_notification(tmp_path, monkeypatch)
    assert live.claim_pending_delivery(home, owner)
    before = live.complete_delivery(home, key, status=status, error='Fixture consumer failure')
    tick()
    assert tick() == [], 'terminal failure must not spin forever or execute again'
    assert live.read_delivery_result(home, key) == before
    assert key in caplog.text and status in caplog.text and 'Fixture consumer failure' in caplog.text
    assert len(list((home / 'runtime' / 'bot_live_delivery').glob('*.json'))) == 1


def test_task_mutation_preserves_admitted_payload_and_receipt(tmp_path, monkeypatch):
    home, owner, tid, key, tick = _pending_notification(tmp_path, monkeypatch)
    before = live.read_delivery_result(home, key)
    with kbc.connect() as conn:
        assert kb.edit_task(conn, tid, title='Edited task title')
        assert kb.assign_task(conn, tid, 'different-worker')
    tick()
    assert live.read_delivery_result(home, key) == before
    assert live.claim_pending_delivery(home, owner)
    live.complete_delivery(home, key, status='settled', reply='Original admitted payload observed')
    tick()
    assert tick() == [], 'mutable title/assignee must not wedge an already settled event'
    assert live.read_delivery_result(home, key)['message'] == before['message']
    assert len(list((home / 'runtime' / 'bot_live_delivery').glob('*.json'))) == 1


def test_claimed_receipt_is_not_cancelled_or_replayed_after_owner_change(tmp_path, monkeypatch):
    home, owner, _tid, key, tick = _pending_notification(tmp_path, monkeypatch)
    assert live.claim_pending_delivery(home, owner)
    before = live.read_delivery_result(home, key)
    monkeypatch.setattr(live, 'find_canonical_live_owner', lambda h: {**owner, 'lease_id': 'replacement'})
    assert len(tick()) == 1
    assert len(tick()) == 1
    assert live.read_delivery_result(home, key) == before
    assert len(list((home / 'runtime' / 'bot_live_delivery').glob('*.json'))) == 1


def test_absent_owner_retains_queued_delivery_until_pin_can_be_resolved(tmp_path, monkeypatch):
    home, owner, _tid, key, tick = _pending_notification(tmp_path, monkeypatch)
    before = live.read_delivery_result(home, key)
    monkeypatch.setattr(live, 'find_canonical_live_owner', lambda h: None)
    assert len(tick()) == 1
    assert live.read_delivery_result(home, key) == before
    monkeypatch.setattr(live, 'find_canonical_live_owner', lambda h: owner)
    assert live.claim_pending_delivery(home, owner)
    live.complete_delivery(home, key, status='settled', reply='Owner remained valid')
    tick()
    assert tick() == []
