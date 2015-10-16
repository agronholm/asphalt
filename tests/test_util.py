from asyncio import coroutine
from unittest.mock import Mock
import asyncio
import threading
from pkg_resources import EntryPoint

import pytest

from asphalt.core.util import (
    resolve_reference, qualified_name, blocking, asynchronous, PluginContainer, merge_config)


class BaseDummyPlugin:
    pass


class DummyPlugin(BaseDummyPlugin):
    def __init__(self, **kwargs):
        self.kwargs = kwargs


@pytest.mark.parametrize('inputval', [
    'asphalt.core.util:resolve_reference',
    resolve_reference
], ids=['reference', 'object'])
def test_resolve_reference(inputval):
    assert resolve_reference(inputval) is resolve_reference


@pytest.mark.parametrize('inputval, error_type, error_text', [
    ('x.y', ValueError, 'invalid reference (no ":" contained in the string)'),
    ('x.y:foo', LookupError, 'error resolving reference x.y:foo: could not import module'),
    ('asphalt.core:foo', LookupError,
     'error resolving reference asphalt.core:foo: error looking up object')
], ids=['invalid_ref', 'module_not_found', 'object_not_found'])
def test_resolve_reference_error(inputval, error_type, error_text):
    exc = pytest.raises(error_type, resolve_reference, inputval)
    assert str(exc.value) == error_text


@pytest.mark.parametrize('overrides', [
    {'a': 2, 'foo': 6, 'b': {'x': 5, 'z': {'r': 'bar', 's': [6, 7]}}},
    {'a': 2, 'foo': 6, 'b.x': 5, 'b.z': {'r': 'bar', 's': [6, 7]}},
    {'a': 2, 'foo': 6, 'b.x': 5, 'b.z.r': 'bar', 'b.z.s': [6, 7]}
], ids=['nested_dicts', 'part_nested', 'dotted_paths'])
def test_merge_config(overrides):
    original = {'a': 1, 'b': {'x': 2, 'y': 3, 'z': {'r': [1, 2], 's': [3, 4]}}}
    expected = {'a': 2, 'foo': 6, 'b': {'x': 5, 'y': 3, 'z': {'r': 'bar', 's': [6, 7]}}}
    assert merge_config(original, overrides) == expected


@pytest.mark.parametrize('inputval, expected', [
    (qualified_name, 'asphalt.core.util.qualified_name'),
    (asyncio.Event(), 'asyncio.locks.Event'),
    (int, 'int')
], ids=['func', 'instance', 'builtintype'])
def test_qualified_name(inputval, expected):
    assert qualified_name(inputval) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize('run_in_threadpool', [False, True], ids=['eventloop', 'threadpool'])
def test_wrap_blocking_callable(event_loop, run_in_threadpool):
    @blocking
    def func(x, y):
        assert threading.current_thread() is not threading.main_thread()
        return x + y

    if run_in_threadpool:
        retval = yield from event_loop.run_in_executor(None, func, 1, 2)
    else:
        retval = yield from func(1, 2)

    assert retval == 3


@pytest.mark.asyncio
@pytest.mark.parametrize('run_in_threadpool', [False, True], ids=['eventloop', 'threadpool'])
def test_wrap_async_callable(event_loop, run_in_threadpool):
    @asynchronous
    @coroutine
    def func(x, y):
        assert threading.current_thread() is threading.main_thread()
        yield from asyncio.sleep(0.2)
        return x + y

    if run_in_threadpool:
        retval = yield from event_loop.run_in_executor(None, func, 1, 2)
    else:
        retval = yield from func(1, 2)

    assert retval == 3


@pytest.mark.asyncio
def test_wrap_async_callable_exception(event_loop):
    @coroutine
    def async_func():
        yield from asyncio.sleep(0.2)
        raise ValueError('test')

    wrapped_async_func = asynchronous(async_func)
    with pytest.raises(ValueError) as exc:
        yield from event_loop.run_in_executor(None, wrapped_async_func)
    assert str(exc.value) == 'test'


class TestPluginContainer:
    @pytest.fixture
    def container(self):
        container = PluginContainer('asphalt.core.test_plugin_container', BaseDummyPlugin)
        entrypoint = EntryPoint('dummy', 'test_util')
        entrypoint.load = Mock(return_value=DummyPlugin)
        container._entrypoints = {'dummy': entrypoint}
        return container

    @pytest.mark.parametrize('inputvalue', [
        'dummy',
        'test_util:DummyPlugin',
        DummyPlugin
    ], ids=['entrypoint', 'reference', 'arbitrary_object'])
    def test_resolve(self, container, inputvalue):
        assert container.resolve(inputvalue) is DummyPlugin

    def test_resolve_bad_entrypoint(self, container):
        exc = pytest.raises(LookupError, container.resolve, 'blah')
        assert str(exc.value) == 'no such entry point in asphalt.core.test_plugin_container: blah'

    @pytest.mark.parametrize('argument', [DummyPlugin, 'dummy', 'test_util:DummyPlugin'],
                             ids=['explicit_class', 'entrypoint', 'class_reference'])
    def test_create_object(self, container, argument):
        """
        Tests that create_object works with all three supported ways of passing a plugin class
        reference.
        """

        component = container.create_object(argument, a=5, b=2)

        assert isinstance(component, DummyPlugin)
        assert component.kwargs == {'a': 5, 'b': 2}

    def test_create_object_bad_type(self, container):
        exc = pytest.raises(TypeError, container.create_object, int)
        assert str(exc.value) == 'int is not a subclass of test_util.BaseDummyPlugin'

    def test_all(self, container):
        assert container.all() == [DummyPlugin]

    def test_repr(self, container):
        assert repr(container) == (
            "PluginContainer(namespace='asphalt.core.test_plugin_container', "
            "base_class=test_util.BaseDummyPlugin)")
