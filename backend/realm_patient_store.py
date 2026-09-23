"""Clinical patient cache with the same realm boundary as its durable episodes."""
from collections.abc import MutableMapping

import realm


class RealmPatientStore(MutableMapping):
    """Resolve the current realm on every mapping operation, without copying data."""

    def __init__(self):
        self._stores = {name: {} for name in realm.REALMS}

    def _current(self):
        return self._stores[realm.current()]

    def __getitem__(self, key):
        return self._current()[key]

    def __setitem__(self, key, value):
        self._current()[key] = value

    def __delitem__(self, key):
        del self._current()[key]

    def __iter__(self):
        return iter(self._current())

    def __len__(self):
        return len(self._current())

    def clear(self):
        self._current().clear()

    def copy(self):
        return self._current().copy()
