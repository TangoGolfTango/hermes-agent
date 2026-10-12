"""Receipt-backed Kanban delivery to the subscription owner's canonical Bot Chat."""
from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def subscription_home(runner, sub):
    """An explicit Bot Chat target may only name its subscription owner."""
    profile = sub.get('notifier_profile')
    if not profile or sub.get('chat_id') != profile or sub.get('thread_id'):
        return None
    current = getattr(runner, '_kanban_notifier_profile', None)
    if profile != current:
        if not getattr(runner.config, 'multiplex_profiles', False):
            return None
        from gateway.run import _multiplex_profile_homes
        homes = dict(_multiplex_profile_homes(runner.config))
        return homes.get(profile)
    from hermes_cli.profiles import get_profile_dir
    return get_profile_dir(profile)


def _receipt_has_final_disposition(home, receipt, task_id, event_id, key):
    from tools.bot_live_delivery import owner_holds_delivery, cancel_queued_delivery, find_canonical_live_owner

    if receipt['status'] == 'queued':
        current = find_canonical_live_owner(home)
        # Absence is not proof of replacement. Same-lease compression keeps
        # the original pin viable; only a positively observed new writer can cancel it.
        replaced = current is not None and any(
            current[name] != receipt['owner'][name] for name in ('lease_id', 'live_session_id')
        )
        if replaced and not owner_holds_delivery(home, receipt):
            receipt = cancel_queued_delivery(
                home, key, error='Bot Chat owner changed before claim', reason='owner_changed',
            )
    status = receipt['status']
    if status in {'failed', 'cancelled', 'ambiguous'}:
        from agent.redact import redact_sensitive_text
        detail = redact_sensitive_text(
            str(receipt.get('reason') or '') + ' ' + str(receipt.get('error') or ''),
            force=True, redact_url_credentials=True,
        )
        logger.warning('kanban notifier: terminal delivery failure task=%s event=%s receipt=%s status=%s detail=%s; '
                       'receipt retained, no replay', task_id, event_id, key, status, detail)
        return True
    return status == 'settled'


def deliver_batch(notification):
    """Return true when all events have final dispositions, including reported failures.

    Only a settled receipt means successful delivery. Queued/claimed/unknown
    outcomes retain the cursor; admitted events are never executed again.
    """
    from agent.redact import redact_sensitive_text
    from hermes_cli import kanban_db as kb
    from tools.bot_live_delivery import (
        deliver_to_live_owner, find_canonical_live_owner, read_delivery_result,
    )
    from gateway.kanban_watchers_notifier import diagnostic_event
    from gateway.run import _profile_runtime_scope

    home = subscription_home(notification.runner, notification.sub)
    if home is None:
        return False
    home = Path(home).resolve()
    with kb.pin_first_board_resolution():
        board_path = str(kb.kanban_db_path(board=notification.board_slug).resolve())

    with _profile_runtime_scope(home):
        disposed = True
        for event in notification.d['events']:
            category = 'diagnostic' if diagnostic_event(event) else 'result'
            key = hashlib.sha256(json.dumps(
                [board_path, notification.task_id, event.id, str(home)],
                separators=(',', ':'),
            ).encode()).hexdigest()
            receipt = read_delivery_result(home, key)
            if receipt is None:
                text = notification.format_event(event)
                if text is None:
                    continue
                owner = find_canonical_live_owner(home)
                if owner is None:
                    disposed = False
                    continue
                message = redact_sensitive_text(
                    '[Kanban task event — automatic worker notification, not the user.]\n\n' + text,
                    force=True, redact_url_credentials=True,
                )
                receipt = deliver_to_live_owner(home, owner, message, delivery_id=key,
                                                notification_category=category)
            # The admitted payload is authoritative. Mutable title/assignee/result
            # must never change it on a retry of the same immutable event identity.
            if receipt.get('notification_category', 'result') != category:
                raise ValueError('Kanban delivery receipt belongs to a different category')
            logger.info('kanban notifier: task=%s event=%s bot-chat:%s receipt=%s status=%s',
                        notification.task_id, event.id, notification.sub['chat_id'], key, receipt['status'])
            final = _receipt_has_final_disposition(home, receipt, notification.task_id, event.id, key)
            disposed = disposed and final
        return disposed


async def deliver(notification):
    from gateway.kanban_watchers_common import _to_thread_process_service

    try:
        disposed = await _to_thread_process_service(deliver_batch, notification)
    except Exception:
        logger.exception('kanban notifier: Bot Chat delivery for %s unverified', notification.task_id)
        disposed = False
    if not disposed:
        await notification.rewind()
        return
    await notification.advance()
    if notification.task and notification.task.status == 'archived':
        await notification.unsub()
