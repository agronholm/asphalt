from __future__ import annotations

import sys
from typing import NoReturn

import anyio
import pytest
from anyio import Event, fail_after, get_current_task, sleep
from anyio.abc import TaskStatus
from pytest import LogCaptureFixture

from asphalt.core import (
    Context,
    add_resource,
    get_resource_nowait,
    start_background_task_factory,
    start_service_task,
)

if sys.version_info < (3, 11):
    from exceptiongroup import ExceptionGroup

pytestmark = pytest.mark.anyio()


class TestTaskFactory:
    async def test_start(self) -> None:
        async def taskfunc() -> str:
            assert get_current_task().name == "taskfunc"
            return "returnvalue"

        async with Context():
            factory = await start_background_task_factory()
            handle = await factory.start_task(taskfunc, "taskfunc")
            assert handle.start_value is None
            await handle.wait_finished()

    async def test_start_empty_name(self) -> None:
        async def taskfunc() -> None:
            assert get_current_task().name == expected_name

        expected_name = (
            f"{__name__}.{self.__class__.__name__}.test_start_empty_name.<locals>"
            f".taskfunc"
        )
        async with Context():
            factory = await start_background_task_factory()
            handle = await factory.start_task(taskfunc)
            assert handle.name == expected_name

    async def test_start_in_subcontext(self) -> None:
        async def taskfunc() -> str:
            assert get_current_task().name == "taskfunc"
            return "returnvalue"

        async with Context(), Context():
            factory = await start_background_task_factory()
            handle = await factory.start_task(taskfunc, "taskfunc")
            assert handle.start_value is None
            await handle.wait_finished()

    async def test_start_status(self) -> None:
        async def taskfunc(task_status: TaskStatus[str]) -> str:
            assert get_current_task().name == "taskfunc"
            task_status.started("startval")
            return "returnvalue"

        async with Context():
            factory = await start_background_task_factory()
            handle = await factory.start_task(taskfunc, "taskfunc")
            assert handle.start_value == "startval"
            await handle.wait_finished()

    async def test_start_cancel(self) -> None:
        started = False
        finished = False

        async def taskfunc() -> None:
            nonlocal started, finished
            assert get_current_task().name == "taskfunc"
            started = True
            await sleep(3)
            finished = True

        async with Context():
            factory = await start_background_task_factory()
            handle = await factory.start_task(taskfunc, "taskfunc")
            handle.cancel()

        assert started
        assert not finished

    async def test_start_exception(self) -> None:
        async def taskfunc() -> NoReturn:
            raise Exception("foo")

        with pytest.raises(ExceptionGroup) as excinfo:
            async with Context():
                factory = await start_background_task_factory()
                await factory.start_task(taskfunc, "taskfunc")

        assert len(excinfo.value.exceptions) == 1
        assert isinstance(excinfo.value.exceptions[0], ExceptionGroup)
        excgrp = excinfo.value.exceptions[0]
        assert len(excgrp.exceptions) == 1
        assert str(excgrp.exceptions[0]) == "foo"

    async def test_start_exception_handled(self) -> None:
        handled_exception: Exception | None = None

        def handle_exception(exc: Exception) -> bool:
            nonlocal handled_exception
            handled_exception = exc
            return True

        async def taskfunc() -> NoReturn:
            raise Exception("foo")

        async with Context():
            factory = await start_background_task_factory(
                exception_handler=handle_exception
            )
            await factory.start_task(taskfunc, "taskfunc")

        assert str(handled_exception) == "foo"

    @pytest.mark.parametrize("name", ["taskname", None])
    async def test_start_soon(self, name: str | None) -> None:
        expected_name = (
            name
            or f"{__name__}.{self.__class__.__name__}.test_start_soon.<locals>.taskfunc"
        )

        async def taskfunc() -> str:
            assert get_current_task().name == expected_name
            return "returnvalue"

        async with Context():
            factory = await start_background_task_factory()
            handle = factory.start_task_soon(taskfunc, name)
            await handle.wait_finished()

        assert handle.name == expected_name

    async def test_context_isolation(self) -> None:
        """
        Test that the background task has no access to resources added after it was
        started.

        """

        async def taskfunc() -> None:
            assert get_resource_nowait(str) == "test"
            assert get_resource_nowait(int, optional=True) is None

        async with Context():
            add_resource("test")
            factory = await start_background_task_factory()
            add_resource(5)
            await factory.start_task(taskfunc)

    async def test_all_task_handles(self) -> None:
        event = Event()

        async def taskfunc() -> None:
            await event.wait()

        async with Context():
            factory = await start_background_task_factory()
            handle1 = await factory.start_task(taskfunc)
            handle2 = factory.start_task_soon(taskfunc)
            assert factory.all_task_handles() == {handle1, handle2}
            event.set()
            for handle in (handle1, handle2):
                await handle.wait_finished()

            assert factory.all_task_handles() == set()


class TestServiceTask:
    async def test_bad_teardown_action(self, caplog: LogCaptureFixture) -> None:
        async def service_func() -> None:
            await event.wait()

        event = anyio.Event()
        async with Context():
            with pytest.raises(ValueError, match="teardown_action must be a callable"):
                await start_service_task(
                    service_func,
                    "Dummy",
                    teardown_action="fail",  # type: ignore[arg-type]
                )

    async def test_teardown_async(self) -> None:
        async def teardown_callback() -> None:
            event.set()

        async def service_func() -> None:
            await event.wait()

        event = anyio.Event()
        with fail_after(1):
            async with Context():
                await start_service_task(
                    service_func, "Dummy", teardown_action=teardown_callback
                )

    async def test_teardown_fail(self, caplog: LogCaptureFixture) -> None:
        def teardown_callback() -> NoReturn:
            raise Exception("foo")

        async def service_func() -> None:
            await event.wait()

        event = anyio.Event()
        with fail_after(1):
            async with Context():
                await start_service_task(
                    service_func, "Dummy", teardown_action=teardown_callback
                )

        assert caplog.messages == [
            f"Error calling teardown callback ({__name__}.{self.__class__.__name__}"
            f".test_teardown_fail.<locals>.teardown_callback) for service task 'Dummy'"
        ]
