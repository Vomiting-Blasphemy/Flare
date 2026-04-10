"""Small pure-Python D-Bus helpers built on top of ``jeepney``.

We deliberately avoid ``dbus-python`` (C extension, PyInstaller hook mess)
and ``dasbus`` (pulls in PyGObject, which is *not* pip-installable without
GObject Introspection dev headers, so bundling it in a PyInstaller binary
is impractical). ``jeepney`` is pure Python and bundles cleanly.

This module provides two things:

1. ``call()`` — a one-shot method-call helper that returns the reply body
   (``tuple``) or raises. Errors are translated into ``DBusError``.

2. ``DBusSignalListener`` — a ``QObject`` that owns a background thread
   running ``conn.recv_messages()``. Matched signals are dispatched to
   callbacks on the Qt main thread via a ``pyqtSignal`` with
   ``QueuedConnection`` (so callbacks execute where Qt objects are safe
   to touch).

Both are written so that any failure is logged and returns a tentative
value rather than raising — the rest of FlareReminder degrades gracefully
if D-Bus is unavailable.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from typing import Any, Callable, Optional

from PyQt6.QtCore import QObject, Qt, pyqtSignal

try:
    from jeepney import DBusAddress, HeaderFields, MessageType, new_method_call
    from jeepney.bus_messages import MatchRule, message_bus
    from jeepney.io.blocking import DBusConnection, open_dbus_connection
    _JEEPNEY_OK = True
except ImportError:  # pragma: no cover
    _JEEPNEY_OK = False

log = logging.getLogger(__name__)


class DBusError(Exception):
    """Raised when a D-Bus method call returns an error message."""


def jeepney_available() -> bool:
    return _JEEPNEY_OK


# ---- one-shot method calls ----------------------------------------------


def open_bus(kind: str = "SESSION") -> Optional["DBusConnection"]:
    """Return an open jeepney connection or None on failure.

    ``kind`` is ``"SESSION"`` or ``"SYSTEM"``.
    """
    if not _JEEPNEY_OK:
        return None
    try:
        return open_dbus_connection(kind)
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not open %s bus: %s", kind, exc)
        return None


def call(
    conn: "DBusConnection",
    service: str,
    path: str,
    interface: str,
    method: str,
    signature: str = "",
    body: tuple = (),
    *,
    timeout: float = 5.0,
) -> tuple:
    """Perform a blocking D-Bus method call.

    Returns the reply body as a tuple. Raises ``DBusError`` on any error.
    """
    if not _JEEPNEY_OK:
        raise DBusError("jeepney not available")
    addr = DBusAddress(path, bus_name=service, interface=interface)
    msg = new_method_call(addr, method, signature, body)
    try:
        reply = conn.send_and_get_reply(msg, timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        raise DBusError(f"{interface}.{method} send failed: {exc}") from exc
    if reply.header.message_type == MessageType.error:
        err_name = reply.header.fields.get(HeaderFields.error_name, "unknown-error")
        err_text = ""
        if reply.body:
            err_text = str(reply.body[0])
        raise DBusError(f"{interface}.{method} -> {err_name}: {err_text}")
    return tuple(reply.body) if reply.body else ()


# ---- signal listener -----------------------------------------------------


class _Subscription:
    __slots__ = ("rule", "callback", "queue", "handle")

    def __init__(self, rule: "MatchRule", callback: Callable[..., None]) -> None:
        self.rule = rule
        self.callback = callback
        self.queue: deque = deque()
        self.handle = None


class DBusSignalListener(QObject):
    """Owns a D-Bus connection plus a background thread that waits on
    incoming signals and dispatches them to callbacks on the Qt main thread.

    Usage::

        listener = DBusSignalListener("SESSION")
        if listener.start():
            listener.subscribe(
                path="/org/freedesktop/ScreenSaver",
                interface="org.freedesktop.ScreenSaver",
                member="ActiveChanged",
                callback=on_screen_saver,
            )

    All callbacks run on the Qt main thread; inside them it is safe to
    touch any QObject.
    """

    # Single Qt signal we use to shuttle dispatches back to the main thread.
    _dispatch = pyqtSignal(object, tuple)

    def __init__(self, bus_kind: str = "SESSION", parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._bus_kind = bus_kind
        self._conn: Optional["DBusConnection"] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._subs: list[_Subscription] = []
        self._dispatch.connect(self._on_dispatch, Qt.ConnectionType.QueuedConnection)

    # ---- public ----------------------------------------------------------

    def start(self) -> bool:
        """Open the bus and start the worker thread. Returns success."""
        if not _JEEPNEY_OK:
            log.warning("jeepney not available; D-Bus signals disabled")
            return False
        self._conn = open_bus(self._bus_kind)
        if self._conn is None:
            return False
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name=f"dbus-{self._bus_kind.lower()}",
        )
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:  # noqa: BLE001
                pass
            self._conn = None

    def conn(self) -> Optional["DBusConnection"]:
        return self._conn

    def subscribe(
        self,
        *,
        path: Optional[str] = None,
        interface: Optional[str] = None,
        member: Optional[str] = None,
        sender: Optional[str] = None,
        callback: Callable[..., None],
    ) -> bool:
        """Install a signal filter. ``callback`` is invoked with the signal
        body (expanded as positional args) on the Qt main thread.
        """
        if self._conn is None:
            return False
        if path is not None:
            rule = MatchRule(
                type="signal",
                interface=interface,
                member=member,
                path=path,
                sender=sender,
            )
        else:
            rule = MatchRule(
                type="signal",
                interface=interface,
                member=member,
                sender=sender,
            )
        try:
            call(
                self._conn,
                "org.freedesktop.DBus",
                "/org/freedesktop/DBus",
                "org.freedesktop.DBus",
                "AddMatch",
                "s",
                (rule.serialise(),),
            )
        except DBusError as exc:
            log.warning(
                "AddMatch for %s.%s failed: %s", interface, member, exc
            )
            return False
        sub = _Subscription(rule, callback)
        # Register a local filter. Jeepney's filter() is a context manager —
        # we manually enter it and hold the cleanup handle for stop().
        cm = self._conn.filter(rule, queue=sub.queue)
        sub.handle = cm
        try:
            cm.__enter__()
        except Exception as exc:  # noqa: BLE001
            log.warning("filter() enter failed: %s", exc)
            return False
        self._subs.append(sub)
        log.debug(
            "D-Bus signal subscribed: iface=%s member=%s path=%s",
            interface, member, path,
        )
        return True

    # ---- internals -------------------------------------------------------

    def _run(self) -> None:
        """Worker-thread loop: drive recv_messages and dispatch hits."""
        assert self._conn is not None
        while not self._stop.is_set():
            try:
                self._conn.recv_messages(timeout=1.0)
            except TimeoutError:
                # recv_messages raises when no message within timeout
                pass
            except Exception as exc:  # noqa: BLE001
                if not self._stop.is_set():
                    log.warning("D-Bus recv loop error: %s", exc)
                    return
                return
            # Drain all subscription queues.
            for sub in self._subs:
                while sub.queue:
                    msg = sub.queue.popleft()
                    body: tuple = tuple(msg.body) if msg.body else ()
                    # Cross-thread dispatch: main thread will run _on_dispatch.
                    self._dispatch.emit(sub.callback, body)

    def _on_dispatch(self, callback: Callable[..., None], body: tuple) -> None:
        try:
            callback(*body)
        except Exception:  # noqa: BLE001
            log.exception("D-Bus signal callback failed")
