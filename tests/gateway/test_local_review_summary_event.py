"""A local viewer sees the background review's summary as a ``review.summary`` event.

LocalSessionAdapter.send publishes nothing (TurnRunner owns the canonical stream), so the
"💾 Self-improvement review: …" line handed to the status adapter reached no Desktop/TUI
viewer once every local turn ran through the session authority. The in-process tui_gateway
emitted it as ``review.summary``; the authority route must publish the same frame.
"""
from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import patch

from gateway.config import Platform
from gateway.run_turn_runner import TurnRunner


def _runner(platform, published):
    turn = object.__new__(TurnRunner)
    adapter_sends = []

    async def send(chat_id, text, metadata=None):
        adapter_sends.append(text)

    turn._ctx = SimpleNamespace(
        source=SimpleNamespace(platform=platform),
        _status_adapter=SimpleNamespace(send=send),
        _status_chat_id='chat',
        _status_thread_metadata=None,
        _run_still_current=lambda: True,
    )
    live = SimpleNamespace(event_stream=SimpleNamespace(
        execution={},
        publish=lambda sid, payload, event_type: published.append((sid, event_type, payload)) or True))
    authority = SimpleNamespace(sessions={'s': live})
    turn._approval_owner = (authority, 's', 3)
    turn._schedule = lambda coro, _msg: coro.close()
    return turn, adapter_sends


def _callbacks(turn):
    # _make_bg_review_callbacks imports gateway.run, whose module init reads the
    # install manifest; stub it when already loaded tests (or a real install)
    # would otherwise touch the real hermes home.
    stub = sys.modules.get('gateway.run') or SimpleNamespace(
        _interim_metadata=lambda md: md, _non_conversational_metadata=lambda md, platform=None: {})
    with patch.dict(sys.modules, {'gateway.run': stub}):
        return turn._make_bg_review_callbacks()


def test_local_review_summary_follows_the_turn_settlement_and_is_never_fenced():
    import threading
    published = []
    turn, adapter_sends = _runner(Platform.LOCAL, published)
    stream = turn._approval_owner[0].sessions['s'].event_stream
    stream.execution = {'execution_generation': 3}  # the drain is still settling turn 3
    turn._ctx._run_still_current = lambda: False  # and the review fork outlives the turn
    send, _release = _callbacks(turn)
    worker = threading.Thread(target=send, args=('💾 Self-improvement review: memory updated',))
    worker.start()
    worker.join(0.3)
    # Not before the turn's message.complete: the drain clears the stamp only after it.
    assert worker.is_alive() and published == []
    stream.execution = {}
    worker.join(5)
    assert published == [('s', 'review.summary', {'text': '💾 Self-improvement review: memory updated'})]
    assert adapter_sends == []


def test_platform_review_summary_still_waits_for_the_post_delivery_release():
    published = []
    turn, _ = _runner(Platform.TELEGRAM, published)
    sent = []
    turn._send_status_text = lambda text, metadata, log_message: sent.append(text)
    send, release = _callbacks(turn)
    send('💾 Memory updated')
    assert sent == []
    release()
    assert sent == ['💾 Memory updated'] and published == []


def test_platform_review_summary_survives_a_superseding_turn():
    """A /refine fork finishing after a newer turn took the session must still
    report: the summary is post-turn bookkeeping, not fenced turn output. The
    old _status_live() gate silently dropped it (seen 2026-11-20: review ran
    6 minutes, wrote a memory entry, and no summary ever reached the chat)."""
    published = []
    turn, _ = _runner(Platform.DISCORD, published)
    sent = []
    turn._send_status_text = lambda text, metadata, log_message: sent.append(text)
    turn._ctx._run_still_current = lambda: False  # a newer turn owns the session
    send, release = _callbacks(turn)
    release()  # the original turn's response already landed
    send('💾 Self-improvement review: memory updated')
    assert sent == ['💾 Self-improvement review: memory updated']
