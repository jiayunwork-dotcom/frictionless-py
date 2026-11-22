from __future__ import annotations

import contextvars
from typing import TYPE_CHECKING, Any, Dict, Optional, Set, Tuple

import attrs

from .. import errors
from ..exception import FrictionlessException
from ..platform import platform

if TYPE_CHECKING:
    from ..resources import TableResource

# Inline data of these types survives a descriptor round-trip, so an isolated
# copy of the resource reads the same rows. Anything else (generators,
# callables, dataframes, ...) cannot be replayed without consuming the source
REPLAYABLE_DATA_TYPES = (str, bool, int, float, list, dict)

_current_scope: contextvars.ContextVar[Optional["LookupScope"]] = contextvars.ContextVar(
    "frictionless_lookup_scope", default=None
)


@attrs.define(kw_only=True, repr=False)
class LookupScope:
    """Foreign key lookup collection boundary

    A scope collects the foreign key sets needed by one validation run (or
    by one standalone resource opening). All the (target resource, key)
    combinations registered into the scope are collected with a single
    isolated scan per target resource, and the collected key sets are
    released when the scope is released. `Package.validate` shares one
    scope between the resources it validates; a resource opened outside
    of a current scope uses a private one living until it is closed.
    """

    _plans: Dict[int, "LookupTargetPlan"] = attrs.field(factory=dict, init=False)
    _token: Optional[contextvars.Token] = attrs.field(  # type: ignore[var-annotated]
        default=None, init=False
    )

    def __enter__(self) -> "LookupScope":
        self._token = _current_scope.set(self)
        return self

    def __exit__(self, type: Any, value: Any, traceback: Any) -> None:
        self.release()

    def release(self) -> None:
        """Release the collected key sets and detach the scope"""
        if self._token is not None:
            _current_scope.reset(self._token)
            self._token = None
        self._plans.clear()

    # Current

    @classmethod
    def current(cls) -> Optional["LookupScope"]:
        """Return the scope of the running validation, if any"""
        return _current_scope.get()

    # Register

    def register(self, target: TableResource, key: Tuple[str, ...]) -> None:
        """Declare a key combination needed from the target resource"""
        plan = self._plans.get(id(target))
        if plan is None:
            plan = self._plans[id(target)] = LookupTargetPlan(target=target)
        plan.keys.add(key)

    def register_resource(self, resource: TableResource) -> None:
        """Declare all the foreign key targets of the resource (best-effort)

        Unresolvable references are skipped here; they are reported by the
        resource's own validation when it opens and resolves them again.
        """
        package = resource.package
        for fk in resource.schema.foreign_keys:
            source_name = fk["reference"]["resource"]
            source_key = tuple(fk["reference"]["fields"])
            if source_name:
                if not package or not package.has_resource(source_name):
                    continue
                target = package.get_resource(source_name)
            else:
                target = resource
            if isinstance(target, platform.frictionless_resources.TableResource):
                self.register(target, source_key)

    # Collect

    def get_keys(
        self, target: TableResource, key: Tuple[str, ...]
    ) -> Set[Tuple[Any, ...]]:
        """Return the key tuples existing in the target for the combination"""
        self.register(target, key)
        plan = self._plans[id(target)]
        if plan.collected is None or key not in plan.collected:
            plan.collected = self._collect(plan)
        return plan.collected[key]

    def _collect(
        self, plan: "LookupTargetPlan"
    ) -> Dict[Tuple[str, ...], Set[Tuple[Any, ...]]]:
        # The target is read through an isolated copy so that neither its
        # own cursor/schema nor streams held by its callers are affected;
        # every registered key combination is collected in the same scan
        collected: Dict[Tuple[str, ...], Set[Tuple[Any, ...]]] = {
            key: set() for key in plan.keys
        }
        resource = create_isolated_resource(plan.target)
        with resource:
            for row in resource.row_stream:  # type: ignore
                for key, cells_set in collected.items():
                    cells = tuple(row.get(name) for name in key)  # type: ignore
                    if set(cells) == {None}:  # type: ignore
                        continue
                    cells_set.add(cells)
        return collected


@attrs.define(kw_only=True, repr=False)
class LookupTargetPlan:
    """Registered key combinations and collected keys for one target"""

    target: TableResource
    keys: Set[Tuple[str, ...]] = attrs.field(factory=set)
    collected: Optional[Dict[Tuple[str, ...], Set[Tuple[Any, ...]]]] = None


def create_isolated_resource(resource: TableResource) -> TableResource:
    """Create an independently readable copy of a foreign key target

    The copy is detached from the resource's state: it has its own schema
    object (without foreign keys, to avoid recursive lookups) and its own
    parser/loader, so reading it neither advances nor closes the original
    resource. Sources that cannot be replayed independently are rejected
    before the original resource is touched.
    """
    if not resource.tabular:
        note = (
            f'cannot build a foreign key lookup for resource "{resource.name}" '
            "as it is not tabular"
        )
        raise FrictionlessException(errors.ResourceError(note=note))
    data = resource.data
    if data is not None and not isinstance(data, REPLAYABLE_DATA_TYPES + (bytes,)):
        note = (
            f'cannot build a foreign key lookup for resource "{resource.name}" '
            "as its data source cannot be re-read independently"
        )
        raise FrictionlessException(errors.ResourceError(note=note))
    copy = resource.to_copy()
    if isinstance(data, bytes):
        # Raw bytes are immutable hence re-readable, but they do not survive
        # the descriptor round-trip so the payload is re-attached directly
        copy.data = data
    # Strip the relations on the copy only, and only when some are declared:
    # touching an otherwise empty schema would mark it as assigned and
    # disable the schema detection during the copy's opening
    if copy.schema.foreign_keys:
        copy.schema.foreign_keys = []
    return copy
