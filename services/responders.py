"""The OpenLobby core server: PlayOnline login, lobby, world and mail responders.

The code lives in the `core` package, one module per concern (see
core/__init__.py for the layout). This module is the entry point the
containers run (`python responders.py <mode[,mode...]>`) and a compatibility
facade: `import responders as R` still resolves every name, reading or
writing, to the module that owns it, so tools and tests written against the
old single-file layout keep working while they are moved over.

    modes: patch directory authcap authserv lobby world mail all
"""
import importlib
import sys
import types

# ONE COPY OF THIS MODULE. The containers run `python responders.py`, so the
# running copy is `__main__`, and a late `import responders` (a title plugin's
# helper, polbridge) would otherwise load a second copy whose module-level
# code re-runs, including the title binding in core.boot.
if __name__ == "__main__":
    sys.modules.setdefault("responders", sys.modules[__name__])

import core  # noqa: E402
from core.main import main  # noqa: E402

# Every core module, in the package's own order; a name is owned by the first
# module that defines it (deps first, so a shared import such as `log` has one
# owner), which is what a rebinding through the facade has to reach.
_MODULES = {}
_OWNERS = {}
for _name in core.MODULES:
    _mod = importlib.import_module("core." + _name)
    _MODULES[_name] = _mod
    for _attr, _val in vars(_mod).items():
        if _attr.startswith("__"):
            continue
        # a module-valued name (accounts, polpro, yaml, ...) is one of the
        # optional imports, which deps owns; elsewhere it is just an import
        if isinstance(_val, types.ModuleType) and _name != "deps":
            continue
        _OWNERS.setdefault(_attr, _name)
del _name, _mod, _attr, _val


class _Facade(types.ModuleType):
    """`responders.<name>` reads and writes go to the owning core module."""

    def __getattr__(self, name):
        mod = _OWNERS.get(name)
        if mod is None:
            raise AttributeError(f"module 'responders' has no attribute {name!r}")
        return getattr(_MODULES[mod], name)

    def __setattr__(self, name, value):
        mod = _OWNERS.get(name)
        if mod is None:
            super().__setattr__(name, value)
            return
        setattr(_MODULES[mod], name, value)
        if mod == "deps":
            # an imported name (log, accounts, ...) is a copy in every module
            # that imported it; a patch has to reach each copy
            for other_name, other in _MODULES.items():
                if other_name != "deps" and hasattr(other, name):
                    setattr(other, name, value)

    def __dir__(self):
        return sorted(set(super().__dir__()) | set(_OWNERS))


sys.modules[__name__].__class__ = _Facade

if __name__ == "__main__":
    main()
