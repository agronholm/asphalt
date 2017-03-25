import logging
import re
from asyncio import get_event_loop, iscoroutinefunction
from functools import wraps
from inspect import isawaitable, getattr_static
from typing import Optional, Callable, Any, Sequence, Dict, Tuple, Type, List, Set, Union  # noqa

from async_generator import async_generator, isasyncgenfunction
from typeguard import check_argument_types

from asphalt.core.event import Signal, Event, wait_event
from asphalt.core.utils import qualified_name, callable_name

__all__ = ('ResourceContainer', 'ResourceEvent', 'ResourceConflict', 'ResourceNotFound', 'Context',
           'context_teardown')

logger = logging.getLogger(__name__)
factory_callback_type = Callable[['Context'], Any]
resource_name_re = re.compile(r'\w+')


class ResourceContainer:
    """
    Contains the resource value or its factory callable, plus some metadata.

    :ivar value_or_factory: the resource value or the factory callback
    :ivar types: type names the resource was registered with
    :vartype types: Tuple[type, ...]
    :ivar str name: name of the resource
    :ivar str context_attr: the context attribute of the resource
    :ivar bool is_factory: ``True`` if ``value_or_factory`` if this is a resource factory
    """

    __slots__ = 'value_or_factory', 'types', 'name', 'context_attr', 'is_factory'

    def __init__(self, value_or_factory, types: Tuple[type, ...], name: str,
                 context_attr: Optional[str], is_factory: bool):
        self.value_or_factory = value_or_factory
        self.types = types
        self.name = name
        self.context_attr = context_attr
        self.is_factory = is_factory

    def generate_value(self, ctx: 'Context'):
        assert self.is_factory, 'generate_value() only works for resource factories'
        value = self.value_or_factory(ctx)
        ctx.add_resource(value, self.name, self.context_attr, self.types)
        return value

    def __repr__(self):
        typenames = ', '.join(qualified_name(cls) for cls in self.types)
        value_repr = ('factory=%s' % callable_name(self.value_or_factory) if self.is_factory
                      else 'value=%r' % self.value_or_factory)
        return ('{self.__class__.__name__}({value_repr}, types=[{typenames}], name={self.name!r}, '
                'context_attr={self.context_attr!r})'.format(
                    self=self, value_repr=value_repr, typenames=typenames))


class ResourceEvent(Event):
    """
    Dispatched when a resource or resource factory has been added to a context.

    :ivar ResourceContainer resource: container for the resource or resource factory
    """

    __slots__ = 'resource'

    def __init__(self, source: 'Context', topic: str, resource: ResourceContainer):
        super().__init__(source, topic)
        self.resource = resource


class ResourceConflict(Exception):
    """
    Raised when a new resource that is being published conflicts with an existing resource or
    context variable.
    """


class ResourceNotFound(LookupError):
    """Raised when a resource request cannot be fulfilled within the allotted time."""

    def __init__(self, type: type, name: str):
        super().__init__(type, name)
        self.type = type
        self.name = name

    def __str__(self):
        return 'no matching resource was found for type={typename} name={self.name!r}'.\
            format(self=self, typename=qualified_name(self.type))


class Context:
    """
    Contexts give request handlers and callbacks access to resources.

    Contexts are stacked in a way that accessing an attribute that is not present in the current
    context causes the attribute to be looked up in the parent instance and so on, until the
    attribute is found (or :class:`AttributeError` is raised).

    :param parent: the parent context, if any
    :param default_timeout: default timeout for :meth:`request_resource` if omitted from the call
        arguments

    :ivar loop: the event loop associated with the context (comes from the parent context, or
        :func:`~asyncio.get_event_loop()` when no parent context is given)
    :vartype loop: asyncio.AbstractEventLoop
    :var Signal resource_added: a signal (:class:`ResourceEvent`) dispatched when a resource
        has been published in this context
    """

    resource_added = Signal(ResourceEvent)

    def __init__(self, parent: 'Context' = None, *, default_timeout: int = 5):
        assert check_argument_types()
        self._parent = parent
        self._resources = {}  # type: Dict[Tuple[type, str], ResourceContainer]
        self._resource_factories = {}  # type: Dict[Tuple[type, str], ResourceContainer]
        self._resource_factories_by_context_attr = {}  # type: Dict[str, ResourceContainer]
        self._teardown_callbacks = []  # type: List[Tuple[Callable, bool]]
        self._closed = False
        self.default_timeout = default_timeout
        self.loop = parent.loop if parent is not None else get_event_loop()

    def __getattr__(self, name):
        # First look for a resource factory in the whole context chain
        for ctx in self.context_chain:
            factory = ctx._resource_factories_by_context_attr.get(name)
            if factory:
                return factory.generate_value(self)

        # When that fails, look directly for an attribute in the parents
        for ctx in self.context_chain[1:]:
            value = getattr_static(ctx, name, None)
            if value is not None:
                return getattr(ctx, name)

        raise AttributeError('no such context variable: {}'.format(name))

    @property
    def parent(self) -> Optional['Context']:
        """Return the parent of this context or ``None`` if there is no parent context."""
        return self._parent

    @property
    def context_chain(self) -> List['Context']:
        """Return the parent of this context or ``None`` if there is no parent context."""
        contexts = []
        ctx = self
        while ctx is not None:
            contexts.append(ctx)
            ctx = ctx.parent

        return contexts

    def _check_closed(self):
        if self._closed:
            raise RuntimeError('this context has already been closed')

    def add_teardown_callback(self, callback: Callable, pass_exception: bool = False) -> None:
        """
        Add a callback to be called when this context closes.

        This is intended for cleanup of resources, and the list of callbacks is processed in the
        reverse order in which they were added, so the last added callback will be called first.

        The callback may return an awaitable. If it does, the awaitable is awaited on before
        calling any further callbacks.

        :param callback: a callable that is called with either no arguments or with the exception
            that ended this context, based on the value of ``pass_exception``
        :param pass_exception: ``True`` to pass the callback the exception that ended this context
            (or ``None`` if the context ended cleanly)

        """
        assert check_argument_types()
        self._teardown_callbacks.append((callback, pass_exception))

    async def close(self, exception: BaseException = None) -> None:
        """
        Close this context and call any necessary resource teardown callbacks.

        If a teardown callback returns an awaitable, the return value is awaited on before calling
        any further teardown callbacks.

        After this method has been called, resources can no longer be requested or published on
        this context.

        :param exception: the exception, if any, that caused this context to be closed

        """
        self._check_closed()
        self._closed = True

        callbacks = reversed(self._teardown_callbacks)
        del self._teardown_callbacks
        for callback, pass_exception in callbacks:
            try:
                retval = callback(exception) if pass_exception else callback()
                if isawaitable(retval):
                    await retval
            except Exception:
                logger.exception('Error calling teardown callback %s', callable_name(callback))

    def __enter__(self):
        self._check_closed()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.loop.run_until_complete(self.close(exc_val))

    async def __aenter__(self):
        self._check_closed()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close(exc_val)

    def add_resource(self, value, name: str = 'default', context_attr: str = None,
                     types: Union[type, Sequence[Type]] = ()) -> ResourceContainer:
        """
        Add a resource to this context.

        This will cause a ``resource_added`` event to be dispatched.

        :param value: the actual resource value
        :param name: name of this resource (unique among all its registered types within a single
            context)
        :param context_attr: name of the context attribute this resource will be accessible as
        :param types: type(s) to register the resource as (omit to use the type of ``value``)
        :raises asphalt.core.context.ResourceConflict: if the resource conflicts with an existing
            one in any way

        """
        assert check_argument_types()
        if isinstance(types, type):
            types = (types,)
        elif not types:
            types = (type(value),)

        if value is None:
            raise ValueError('"value" must not be None')
        if not resource_name_re.fullmatch(name):
            raise ValueError('"name" must be a nonempty string consisting only of alphanumeric '
                             'characters and underscores')
        if context_attr and getattr_static(self, context_attr, None) is not None:
            raise ResourceConflict('this context already has an attribute {!r}'.format(
                context_attr))
        for resource_type in types:
            if (resource_type, name) in self._resources:
                raise ResourceConflict(
                    'this context already contains a resource of type {} using the name {!r}'.
                    format(qualified_name(resource_type), name))

        resource = ResourceContainer(value, types, name, context_attr, False)
        for type_ in resource.types:
            self._resources[(type_, name)] = resource

        if context_attr:
            setattr(self, context_attr, value)

        # Notify listeners that a new resource has been made available
        self.resource_added.dispatch(resource)
        return resource

    def add_resource_factory(self, factory_callback: factory_callback_type,
                             types: Union[type, Sequence[Type]], name: str = 'default',
                             context_attr: str = None) -> ResourceContainer:
        """
        Add a resource factory to this context.

        This will cause a ``resource_added`` event to be dispatched.

        A resource factory is a callable that generates a "contextual" resource when it is
        requested by either using any of the methods :meth:`get_resource`, :meth:`require_resource`
        or :meth:`request_resource` or its context attribute is accessed.

        When a new resource is created in this manner, it is always bound to the context through
        it was requested, regardless of where in the chain the factory itself was added to.

        :param factory_callback: a (non-coroutine) callable that takes a context instance as
            argument and returns a tuple of (resource object, teardown callback)
        :param types: one or more types to register the generated resource as on the target context
        :param name: name of the resource that will be created in the target context
        :param context_attr: name of the context attribute the created resource will be accessible
            as
        :raises asphalt.core.context.ResourceConflict: if there is an existing resource factory for
            the given type/name combinations or the given context variable

        """
        assert check_argument_types()
        types = (types,) if isinstance(types, type) else types
        if not resource_name_re.fullmatch(name):
            raise ValueError('"name" must be a nonempty string consisting only of alphanumeric '
                             'characters and underscores')
        if iscoroutinefunction(factory_callback):
            raise TypeError('"factory_callback" must not be a coroutine function')
        if not types:
            raise ValueError('"types" must not be empty')

        # Check for a conflicting context attribute
        if context_attr in self._resource_factories_by_context_attr:
            raise ResourceConflict(
                'this context already contains a resource factory for the context attribute {!r}'.
                format(context_attr))

        # Check for conflicts with existing resource factories
        types = tuple(types)
        for type_ in types:
            if (type_, name) in self._resource_factories:
                raise ResourceConflict('this context already contains a resource factory for the '
                                       'type {}'.format(qualified_name(type_)))

        # Add the resource factory to the appropriate lookup tables
        resource = ResourceContainer(factory_callback, types, name, context_attr, True)
        for type_ in types:
            self._resource_factories[(type_, name)] = resource

        if context_attr:
            self._resource_factories_by_context_attr[context_attr] = resource

        # Notify listeners that a new resource has been made available
        self.resource_added.dispatch(resource)
        return resource

    def get_resources(self, type: type = None, *,
                      include_parents: bool = True) -> Set[ResourceContainer]:
        """
        Return containers for resources and resource factories specific to one type or all types.

        :param type: type of the resources to return, or ``None`` to return all resources
        :param include_parents: include the resources from parent contexts

        """
        resources = set(resource for resource in self._resources.values()
                        if type is None or type in resource.types)
        if include_parents and self._parent:
            resources.update(self._parent.get_resources(type))

        return resources

    def get_resource(self, type: type, name: str = 'default'):
        """
        Look up a resource in the chain of contexts.

        :param type: type of the requested resource
        :param name: name of the requested resource
        :return: the requested resource, or ``None`` if none was available

        """
        key = (type, name)

        # First check if there's already a matching resource in this context
        resource = self._resources.get(key)
        if resource is not None:
            return resource.value_or_factory

        # Next, check if there's a resource factory available on the context chain
        resource = next((ctx._resource_factories[key] for ctx in self.context_chain
                         if key in ctx._resource_factories), None)
        if resource is not None:
            return resource.generate_value(self)

        # Finally, check parents for a matching resource
        return next((ctx._resources[key].value_or_factory for ctx in self.context_chain
                     if key in ctx._resources), None)

    def require_resource(self, type: type, name: str = 'default'):
        """
        Look up a resource in the chain of contexts and raise an exception if it is not found.

        This is like :meth:`get_resource` except that instead of returning ``None`` when a resource
        is not found, it will raise :exc:`~asphalt.core.context.ResourceNotFound`.

        :param type: type of the requested resource
        :param name: name of the requested resource
        :return: the requested resource
        :raises asphalt.core.context.ResourceNotFound: if a resource of the given type and name was
            not found

        """
        resource = self.get_resource(type, name)
        if resource is None:
            raise ResourceNotFound(type, name)

        return resource

    async def request_resource(self, type: type, name: str = 'default'):
        """
        Look up a resource in the chain of contexts.

        This is like :meth:`get_resource` except that if the resource is not already available, it
        will wait for one to become available.

        :param type: type of the requested resource
        :param name: name of the requested resource
        :return: the requested resource

        """
        # First try to locate an existing resource in this context and its parents
        value = self.get_resource(type, name)
        if value is not None:
            return value

        # Wait until a matching resource or resource factory is available
        signals = [ctx.resource_added for ctx in self.context_chain]
        await wait_event(
            signals, lambda event: event.resource.name == name and type in event.resource.types)
        return self.get_resource(type, name)


def context_teardown(func: Callable):
    """
    Wrap an async generator function to execute the rest of the function at context teardown.

    This function returns an async function, which, when called, starts the wrapped async
    generator. The wrapped async function is run until the first ``yield`` statement
    (``await async_generator.yield_()`` on Python 3.5). When the context is being torn down, the
    exception that ended the context, if any, is sent to the generator.

    For example::

        class SomeComponent(Component):
            @context_teardown
            async def start(self, ctx: Context):
                service = SomeService()
                ctx.add_resource(service)
                exception = yield
                service.stop()

    :param func: an async generator function
    :return: an async function

    """
    @wraps(func)
    async def wrapper(*args, **kwargs) -> None:
        async def teardown_callback(exception: Optional[Exception]):
            try:
                await generator.asend(exception)
            except StopAsyncIteration:
                pass
            finally:
                await generator.aclose()

        if len(args) > 0 and isinstance(args[0], Context):
            ctx = args[0]
        elif len(args) > 1 and isinstance(args[1], Context):
            ctx = args[1]
        else:
            raise RuntimeError(
                'either the first or second positional argument needs to be a Context instance')

        generator = func(*args, **kwargs)
        try:
            await generator.asend(None)
        except StopAsyncIteration:
            raise RuntimeError('{} did not do "await yield_()"'.format(qualified_name(func)))
        except BaseException:
            await generator.aclose()
            raise
        else:
            ctx.add_teardown_callback(teardown_callback, True)

    if iscoroutinefunction(func):
        func = async_generator(func)
    elif not isasyncgenfunction(func):
        raise TypeError('{} must be an async generator function'.format(qualified_name(func)))

    return wrapper
