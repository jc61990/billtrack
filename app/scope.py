"""Who the current work is for. Finance queries call owner(), which FAILS CLOSED: if no user is in scope (an admin,
a script without a user, a forgotten code path) it raises instead of returning anyone's data."""
import contextvars

_owner: contextvars.ContextVar = contextvars.ContextVar("bt_owner", default=None)
_week: contextvars.ContextVar = contextvars.ContextVar("bt_week_start", default=None)


class NoOwner(Exception):
    pass


def owner() -> int:
    o = _owner.get()
    if not o:
        raise NoOwner("No user in scope for finance data")
    return o


def set_owner(uid: int | None, week_start: int | None = None):
    return _owner.set(uid), _week.set(week_start)


def reset(tokens):
    _owner.reset(tokens[0])
    _week.reset(tokens[1])


def week_start_override():
    return _week.get()


class as_user:
    """with scope.as_user(uid, week_start): ...  (scheduler / CLI)"""
    def __init__(self, uid, week_start=None):
        self.uid, self.ws = uid, week_start

    def __enter__(self):
        self.t = set_owner(self.uid, self.ws)
        return self

    def __exit__(self, *a):
        reset(self.t)
