"""
SSE 事件匯流排 — 全域單例，供廚房看板即時更新使用。
"""
import threading
import json
import time

_lock = threading.Lock()
_listeners: list = []
_version: int = 0


def emit(event_type: str, payload: dict = None):
    global _version
    with _lock:
        _version += 1
        event = {
            "id": _version,
            "type": event_type,
            "payload": payload or {},
            "createdAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        dead = []
        for q in _listeners:
            try:
                q.put_nowait(event)
            except Exception:
                dead.append(q)
        for q in dead:
            _listeners.remove(q)


def subscribe():
    """回傳一個 queue.Queue，呼叫端從中讀取事件。"""
    import queue
    q = queue.Queue(maxsize=64)
    with _lock:
        _listeners.append(q)
    return q


def unsubscribe(q):
    with _lock:
        if q in _listeners:
            _listeners.remove(q)


def current_version() -> int:
    return _version
