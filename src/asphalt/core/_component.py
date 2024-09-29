from __future__ import annotations

import logging
import sys
from abc import ABCMeta, abstractmethod
from collections.abc import Coroutine, MutableMapping, Sequence
from dataclasses import dataclass, field
from inspect import isclass
from traceback import StackSummary
from types import FrameType
from typing import Any, Callable, Literal, TypeVar, overload

from anyio import (
    CancelScope,
    create_task_group,
    get_current_task,
    get_running_tasks,
    sleep,
)
from anyio.abc import TaskStatus

from ._concurrent import start_service_task
from ._context import (
    Context,
    FactoryCallback,
    T_Resource,
    TeardownCallback,
    current_context,
)
from ._exceptions import NoCurrentContext
from ._utils import PluginContainer, merge_config, qualified_name

if sys.version_info >= (3, 11):
    pass
else:
    pass

logger = logging.getLogger("asphalt.core")

TComponent = TypeVar("TComponent", bound="Component")


class Component(metaclass=ABCMeta):
    """This is the base class for all Asphalt components."""

    _child_components: dict[str, dict[str, Any]] | None = None
    _component_started = False

    def add_component(
        self, alias: str, /, type: str | type[Component] | None = None, **config: Any
    ) -> None:
        """
        Add a child component.

        This will store the type and configuration options of the named child component,
        to be later instantiated by :func:`start_component`.

        If the ``type`` argument is omitted, then the value of the ``alias`` argument is
        used to derive the type.

        The locally given configuration can be overridden by component configuration
        parameters supplied to the constructor (via the ``components`` argument).

        When configuration values are provided both as keyword arguments to this method
        and component configuration through the ``components`` constructor argument, the
        configurations are merged together using :func:`~asphalt.core.merge_config`
        in a way that the configuration values from the ``components`` argument override
        the keyword arguments to this method.

        :param alias: a name for the component instance, unique within this container
        :param type: name of and entry point in the ``asphalt.components`` namespace or
            a :class:`Component` subclass
        :param config: mapping of keyword arguments passed to the component's
            initializer
        :raises RuntimeError: if there is already a child component with the same alias

        """
        if self._component_started:
            raise RuntimeError(
                "child components cannot be added once start_component() has been "
                "called on the component"
            )

        if not isinstance(alias, str) or not alias:
            raise TypeError("alias must be a nonempty string")

        if self._child_components is None:
            self._child_components = {}
        elif alias in self._child_components:
            raise ValueError(f'there is already a child component named "{alias}"')

        self._child_components[alias] = {"type": type or alias, **config}

    async def prepare(self) -> None:
        """
        Perform any necessary initialization before starting the component.

        This method is called by :func:`start_component` *before* starting the child
        components of this component, so it can be used to add any resources required
        by the child components.

        The rules for resource handling during component startup are as follows:

        * Any resources added here without an explicit name will use the default
          resource name of ``default``, regardless of what alias this component was
          configured with, in contrast to how :meth:`start` works.
        * Any resources added here will only be seen by the direct children of this
          component. Parent and sibling components will not be able to see these
          resources.
        """

    async def start(self) -> None:
        """
        Perform any necessary tasks to start the services provided by this component.

        .. warning:: Do not call this method directly; use :func:`start_component`
            instead.

        This method is called by :func:`start_component` *after* the child components of
        this component have been started, so any resources provided by the child
        components are available at this point.

        The rules for resource handling during component startup are as follows:

        * Any resources added here without an explicit name will use the default
          resource name that may not be ``default``. For example, if this component is
          started as a child component of another component, with the alias of
          ``foo/bar``, then the default resource name used by
          :meth:`Context.add_resource` will be ``bar``.
        * Any resources added here will only be seen by sibling components and direct
          parent components.
        """


class CLIApplicationComponent(Component):
    """
    Specialized :class:`.Component` subclass for command line tools.

    Command line tools and similar applications should use this as their root component
    and implement their main code in the :meth:`run` method.

    When all the subcomponents have been started, :meth:`run` is started as a new task.
    When the task is finished, the application will exit using the return value as its
    exit code.

    If :meth:`run` raises an exception, a stack trace is printed and the exit code will
    be set to 1. If the returned exit code is out of range or of the wrong data type,
    it is set to 1 and a warning is emitted.
    """

    @abstractmethod
    async def run(self) -> int | None:
        """
        Run the business logic of the command line tool.

        Do not call this method yourself.

        :return: the application's exit code (0-127; ``None`` = 0)
        """


component_types = PluginContainer("asphalt.components", Component)


class ComponentContext(Context):
    def __init__(
        self, component: Component, path: str, default_resource_name: str
    ) -> None:
        super().__init__(lookup_depth=1)
        self.description = f"{component.__class__.__qualname__} ({path or '(root)'})"
        self.path = path
        self._default_resource_name = default_resource_name
        self.status: Literal["preparing", "starting"] = "preparing"

    def add_resource(
        self,
        value: T_Resource,
        name: str | None = None,
        types: type | Sequence[type] = (),
        *,
        description: str | None = None,
        teardown_callback: Callable[[], Any] | None = None,
    ) -> None:
        if self.status == "preparing":
            name = "default"

        super().add_resource(
            value,
            name,
            types,
            description=description,
            teardown_callback=teardown_callback,
        )
        if self._parent is not None and self.status == "starting":
            Context.add_resource(
                self._parent,
                value,
                name or self._default_resource_name,
                types,
                description=description,
                teardown_callback=teardown_callback,
            )

    def add_resource_factory(
        self,
        factory_callback: FactoryCallback,
        name: str | None = None,
        *,
        types: Sequence[type] | None = None,
        description: str | None = None,
    ) -> None:
        if self.status == "preparing":
            name = "default"

        super().add_resource_factory(
            factory_callback,
            name,
            types=types,
            description=description,
        )
        if self._parent is not None and self.status == "starting":
            self._parent.add_resource_factory(
                factory_callback,
                name,
                types=types,
                description=description,
            )

    def add_teardown_callback(
        self, callback: TeardownCallback, pass_exception: bool = False
    ) -> None:
        assert self._parent is not None
        self._parent.add_teardown_callback(callback, pass_exception)


def _init_component(
    config: object,
    path: str,
    child_components_by_alias: dict[str, dict[str, Component]],
) -> Component:
    if not isinstance(config, MutableMapping):
        raise TypeError(
            f"{path}: config must be a mutable mapping, not {qualified_name(config)}"
        )

    # Separate the child components from the config
    child_components_config = config.pop("components", {})

    # Resolve the type to a class
    component_type = config.pop("type")
    component_class = component_types.resolve(component_type)
    if not isclass(component_class) or not issubclass(component_class, Component):
        raise TypeError(
            f"{component_type!r} resolved to {component_class} which is not a subclass "
            f"of Component"
        )

    # Instantiate the component
    component = component_class(**config)

    # Merge the overrides to the hard-coded configuration
    child_components_config = merge_config(
        component._child_components, child_components_config
    )

    # Create the child components
    child_components = child_components_by_alias[path] = {}
    for alias, child_config in child_components_config.items():
        if child_config is None:
            child_config = {}

        if not isinstance(child_config, MutableMapping):
            raise TypeError(
                f"{path}: child component configuration must be either None or a dict "
                f"(or other mutable mapping type)"
            )

        # If the type was specified only via an alias, use that as a type
        child_config.setdefault("type", alias)

        # If the type contains a forward slash, split the latter part out of it
        if isinstance(child_config["type"], str) and "/" in child_config["type"]:
            child_config["type"] = child_config["type"].split("/")[0]

        final_path = f"{path}.{alias}" if path else alias

        child_component = _init_component(
            child_config, final_path, child_components_by_alias
        )
        child_components[alias] = child_component

    return component


async def _start_component(
    component: Component,
    path: str,
    default_resource_name: str,
    child_components_by_alias: dict[str, dict[str, Component]],
) -> None:
    # Prevent add_component() from being called beyond this point
    component._component_started = True

    # Call prepare() on the component itself
    async with ComponentContext(component, path, default_resource_name) as ctx:
        await component.prepare()
        ctx.status = "starting"

        # Start the child components
        if child_components := child_components_by_alias.get(path):
            async with create_task_group() as tg:
                for alias, child_component in child_components.items():
                    if "/" in alias:
                        default_resource_name = alias.split("/", 1)[1]
                    else:
                        default_resource_name = "default"

                    final_path = f"{path}.{alias}" if path else alias
                    tg.start_soon(
                        _start_component,
                        child_component,
                        final_path,
                        default_resource_name,
                        child_components_by_alias,
                        name=f"Starting {final_path} ({qualified_name(child_component)})",
                    )

        await component.start()


@overload
async def start_component(
    component_class: type[TComponent],
    config: dict[str, Any] | None = ...,
    *,
    timeout: float | None = ...,
) -> TComponent: ...


@overload
async def start_component(
    component_class: str,
    config: dict[str, Any] | None = ...,
    *,
    timeout: float | None = ...,
) -> Component: ...


async def start_component(
    component_class: type[Component] | str,
    config: dict[str, Any] | None = None,
    *,
    timeout: float | None = 20,
) -> Component:
    """
    Start a component and its subcomponents.

    :param component_class: the root component class, an entry point name in the
        ``asphalt.components`` namespace or a ``modulename:varname`` reference
    :param config: configuration for the root component (and its child components)
    :param timeout: seconds to wait for all the components in the hierarchy to start
        (default: ``20``; set to ``None`` to disable timeout)
    :raises RuntimeError: if this function is called without an active :class:`Context`
    :raises TimeoutError: if the startup of the component hierarchy takes more than
        ``timeout`` seconds
    :raises TypeError: if ``component_class`` is neither a :class:`Component` subclass
        or a string
    :return: the root component instance

    """
    try:
        current_context()
    except NoCurrentContext:
        raise RuntimeError(
            "start_component() requires an active Asphalt context"
        ) from None

    if config is None:
        config = {}

    config.setdefault("type", component_class)
    child_components_by_alias: dict[str, dict[str, Component]] = {}
    root_component = _init_component(config, "", child_components_by_alias)

    with CancelScope() as startup_scope:
        startup_watcher_scope: CancelScope | None = None
        if timeout is not None:
            startup_watcher_scope = await start_service_task(
                lambda task_status: _component_startup_watcher(
                    startup_scope,
                    root_component,
                    timeout,
                    task_status=task_status,
                ),
                "Asphalt component startup watcher task",
            )

        await _start_component(root_component, "", "default", child_components_by_alias)

    if startup_scope.cancel_called:
        raise TimeoutError("timeout starting component")

    # Cancel the startup timeout, if any
    if startup_watcher_scope:
        startup_watcher_scope.cancel()

    return root_component


async def _component_startup_watcher(
    startup_cancel_scope: CancelScope,
    root_component: Component,
    start_timeout: float,
    *,
    task_status: TaskStatus[CancelScope],
) -> None:
    def get_coro_stack_summary(coro: Any) -> StackSummary:
        import gc

        frames: list[FrameType] = []
        while isinstance(coro, Coroutine):
            while coro.__class__.__name__ == "async_generator_asend":
                # Hack to get past asend() objects
                coro = gc.get_referents(coro)[0].ag_await

            if frame := getattr(coro, "cr_frame", None):
                frames.append(frame)

            coro = getattr(coro, "cr_await", None)

        frame_tuples = [(f, f.f_lineno) for f in frames]
        return StackSummary.extract(frame_tuples)

    current_task = get_current_task()
    parent_task = next(
        task_info
        for task_info in get_running_tasks()
        if task_info.id == current_task.parent_id
    )

    with CancelScope() as cancel_scope:
        task_status.started(cancel_scope)
        await sleep(start_timeout)

    if cancel_scope.cancel_called:
        return

    @dataclass
    class ComponentStatus:
        name: str
        alias: str | None
        parent_task_id: int | None
        traceback: list[str] = field(init=False, default_factory=list)
        children: list[ComponentStatus] = field(init=False, default_factory=list)

    import re
    import textwrap

    component_task_re = re.compile(r"^Starting (\S+) \((.+)\)$")
    component_statuses: dict[int, ComponentStatus] = {}
    for task in get_running_tasks():
        if task.id == parent_task.id:
            status = ComponentStatus(qualified_name(root_component), None, None)
        elif task.name and (match := component_task_re.match(task.name)):
            name: str
            alias: str
            alias, name = match.groups()
            status = ComponentStatus(name, alias, task.parent_id)
        else:
            continue

        status.traceback = get_coro_stack_summary(task.coro).format()
        component_statuses[task.id] = status

    root_status: ComponentStatus
    for task_id, component_status in component_statuses.items():
        if component_status.parent_task_id is None:
            root_status = component_status
        elif parent_status := component_statuses.get(component_status.parent_task_id):
            parent_status.children.append(component_status)

    def format_status(status_: ComponentStatus, level: int) -> str:
        title = f"{status_.alias or 'root'} ({status_.name})"
        if status_.children:
            children_output = ""
            for i, child in enumerate(status_.children):
                prefix = "| " if i < (len(status_.children) - 1) else "  "
                children_output += "+-" + textwrap.indent(
                    format_status(child, level + 1),
                    prefix,
                    lambda line: line[0] in " +|",
                )

            output = title + "\n" + children_output
        else:
            formatted_traceback = "".join(status_.traceback)
            if level == 0:
                formatted_traceback = textwrap.indent(formatted_traceback, "| ")

            output = title + "\n" + formatted_traceback

        return output

    logger.error(
        "Timeout waiting for the root component to start\n"
        "Components still waiting to finish startup:\n%s",
        textwrap.indent(format_status(root_status, 0).rstrip(), "  "),
    )
    startup_cancel_scope.cancel()
