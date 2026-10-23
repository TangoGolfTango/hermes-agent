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

pytest_plugins = ('tests.gateway.test_session_bot_retry',)


class ScriptedAuthority:
    """Script the canonical RPC boundary; keep the actual admission receipt format and writer."""

    def __call__(self, home, params):
        from gateway.session_bot_mailbox import _write_receipt

        existing = live.read_delivery_result(home, params['id'])
        if existing is not None:
            assert existing['message'] == params['message']
            return existing
        receipt = dict(delivery_id=params['id'], admission_id='adm-' + params['id'],
                       profile_home=str(Path(home).resolve()), session_id=params['session_id'],
                       principal_id='fixture', message=params['message'], status='queued', reply='',
                       notification_category=params.get('notification_category', 'result'))
        _write_receipt(home, receipt)
        return receipt

    def update(self, home, key, *, status, reply='', error=''):
        from gateway.session_bot_mailbox import _write_receipt

        receipt = live.read_delivery_result(home, key)
        assert receipt['admission_id'] and 'owner' not in receipt
        receipt.update(status=status, reply=reply, error=error)
        _write_receipt(home, receipt)
        return receipt


@pytest.fixture
def authority(monkeypatch):
    authority = ScriptedAuthority()
    monkeypatch.setattr(live, 'authority_delivery', authority)
    return authority


@pytest.mark.asyncio
async def test_canonical_notifier_admits_once_and_waits_for_the_authority_to_settle(bot, monkeypatch, caplog):
    from gateway.session_bot import deliver
    from gateway.session_contract import SessionRef
    from hermes_state_runtime import list_session_admissions
    from tests.gateway.test_session_bot_retry import _settle_quietly
    from tests.waiting import async_wait_until

    loop = asyncio.get_running_loop()
    schedule = bot.authority._schedule
    monkeypatch.setattr(bot.authority, '_schedule', lambda ref: None)
    monkeypatch.setattr('hermes_cli.profiles.get_profile_dir', lambda profile: bot.home)
    owner = dict(profile_home=str(bot.home), session_id=bot.chat, lease_id='fixture', live_session_id=bot.chat)
    monkeypatch.setattr(live, 'find_canonical_live_owner', lambda home: owner)

    def admit(home, params):
        return asyncio.run_coroutine_threadsafe(deliver(bot.connection, params), loop).result(10)

    monkeypatch.setattr(live, 'authority_delivery', admit)
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.adapters = {}
    runner._profile_adapters = {}
    runner._kanban_dispatcher_lock_handle = None
    runner._kanban_notifier_profile = 'default'
    runner.config = SimpleNamespace(multiplex_profiles=False)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title='Canonical notification', assignee='default')
        kbn.add_notify_sub(conn, task_id=tid, platform='bot-chat', chat_id='default', notifier_profile='default')
        kb.block_task(conn, tid, reason='Worker fixture failure')

    async def tick():
        batches = _notifier_collect(runner, kb, notifier_profile='default', gc_due=False, gc_retention_days=30)
        for batch in batches:
            await _KanbanNotification(runner, batch, platform_cls=Platform, sub_fail_counts={}).deliver()
        return batches

    assert await tick()
    rows = list_session_admissions(bot.authority.db, session_id=bot.chat, pending_only=False)
    assert rows
    tickets = list((bot.home / 'runtime' / 'bot_live_delivery').glob('*.json'))
    assert tickets
    for path in tickets:
        record = live.read_delivery_result(bot.home, path.stem)
        assert record['status'] == 'queued' and record['admission_id'] and 'owner' not in record
        assert record['notification_category'] == 'diagnostic'
    monkeypatch.setattr(live, 'find_canonical_live_owner', lambda home: {**owner, 'lease_id': 'replacement'})
    assert await tick()
    assert len(list_session_admissions(bot.authority.db, session_id=bot.chat, pending_only=False)) == len(rows)
    assert 'unverified' not in caplog.text
    monkeypatch.setattr(bot.authority, '_schedule', schedule)
    schedule(SessionRef(bot.authority.profile_id, bot.chat))
    await async_wait_until(
        lambda: all(row['status'] == 'terminal' for row in list_session_admissions(
            bot.authority.db, session_id=bot.chat, pending_only=False)),
        timeout=10, what='canonical Kanban notification to settle',
    )
    await _settle_quietly(bot)
    assert await tick()
    assert await tick() == []
    assert len(list_session_admissions(bot.authority.db, session_id=bot.chat, pending_only=False)) == len(rows)


@pytest.mark.parametrize('status,final', [('queued', True), ('claimed', False)])
def test_unadmitted_legacy_receipt_cancels_only_before_a_claim(tmp_path, monkeypatch, status, final):
    from gateway.kanban_bot_chat import _receipt_has_final_disposition

    key = 'a' * 64
    owner = dict(profile_home=str(tmp_path), session_id='canonical', lease_id='old', live_session_id='old')
    receipt = dict(id=key, delivery_id=key, owner=owner, **owner, status=status,
                   message='Legacy notification', created_at=1, sequence=1)
    with live._locked(tmp_path) as root:
        live._write(root / f'{key}.json', receipt)
    monkeypatch.setattr(live, 'find_canonical_live_owner', lambda home: {**owner, 'lease_id': 'replacement'})
    assert _receipt_has_final_disposition(tmp_path, receipt, 'task', 1, key) is final
    assert live.read_delivery_result(tmp_path, key)['status'] == ('cancelled' if final else 'claimed')


def test_worker_crash_notifies_bot_chat_once_after_settled_receipt(tmp_path, monkeypatch, authority, caplog):
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
    assert 'unverified' not in caplog.text
    tickets = list((home / 'runtime' / 'bot_live_delivery').glob('*.json'))
    assert tickets
    for path in tickets:
        receipt = live.read_delivery_result(home, path.stem)
        assert receipt['status'] == 'queued'
        assert tid in receipt['message'] and 'Kanban' in receipt['message']
        assert 'Cronjob' not in receipt['message']
        assert receipt['notification_category'] == 'diagnostic'
        authority.update(home, path.stem, status='settled', reply='Observed worker failure')
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


def test_queued_authority_admission_survives_owner_change_without_replaying(tmp_path, monkeypatch, authority):
    home, owner, _tid, key, tick = _pending_notification(tmp_path, monkeypatch)
    monkeypatch.setattr(live, 'find_canonical_live_owner', lambda h: {**owner, 'lease_id': 'replacement'})
    tick()
    receipt = live.read_delivery_result(home, key)
    assert receipt['status'] == 'queued'
    assert receipt['admission_id'] and 'owner' not in receipt
    authority.update(home, key, status='settled', reply='Canonical authority completed the admission')
    tick()
    assert tick() == []
    assert len(list((home / 'runtime' / 'bot_live_delivery').glob('*.json'))) == 1


@pytest.mark.parametrize("status", ["failed", "cancelled", "ambiguous"])
def test_terminal_failed_receipt_has_observable_final_disposition(tmp_path, monkeypatch, caplog, status, authority):
    home, _owner, _tid, key, tick = _pending_notification(tmp_path, monkeypatch)
    before = authority.update(home, key, status=status, error='Fixture consumer failure')
    tick()
    assert tick() == [], 'terminal failure must not spin forever or execute again'
    assert live.read_delivery_result(home, key) == before
    assert key in caplog.text and status in caplog.text and 'Fixture consumer failure' in caplog.text
    assert len(list((home / 'runtime' / 'bot_live_delivery').glob('*.json'))) == 1


def test_task_mutation_preserves_admitted_payload_and_receipt(tmp_path, monkeypatch, authority):
    home, _owner, tid, key, tick = _pending_notification(tmp_path, monkeypatch)
    before = live.read_delivery_result(home, key)
    with kbc.connect() as conn:
        assert kb.edit_task(conn, tid, title='Edited task title')
        assert kb.assign_task(conn, tid, 'different-worker')
    tick()
    assert live.read_delivery_result(home, key) == before
    authority.update(home, key, status='settled', reply='Original admitted payload observed')
    tick()
    assert tick() == [], 'mutable title/assignee must not wedge an already settled event'
    assert live.read_delivery_result(home, key)['message'] == before['message']
    assert len(list((home / 'runtime' / 'bot_live_delivery').glob('*.json'))) == 1


def test_claimed_receipt_is_not_cancelled_or_replayed_after_owner_change(tmp_path, monkeypatch, authority):
    home, owner, _tid, key, tick = _pending_notification(tmp_path, monkeypatch)
    before = authority.update(home, key, status='claimed')
    monkeypatch.setattr(live, 'find_canonical_live_owner', lambda h: {**owner, 'lease_id': 'replacement'})
    assert len(tick()) == 1
    assert len(tick()) == 1
    assert live.read_delivery_result(home, key) == before
    assert len(list((home / 'runtime' / 'bot_live_delivery').glob('*.json'))) == 1


def test_absent_owner_retains_queued_delivery_until_pin_can_be_resolved(tmp_path, monkeypatch, authority):
    home, owner, _tid, key, tick = _pending_notification(tmp_path, monkeypatch)
    before = live.read_delivery_result(home, key)
    monkeypatch.setattr(live, 'find_canonical_live_owner', lambda h: None)
    assert len(tick()) == 1
    assert live.read_delivery_result(home, key) == before
    monkeypatch.setattr(live, 'find_canonical_live_owner', lambda h: owner)
    authority.update(home, key, status='settled', reply='Owner remained valid')
    tick()
    assert tick() == []
