import functools
import sys
from typing import (
    Any,
    AsyncIterator,
    Awaitable,
    Callable,
    Coroutine,
    Dict,
    Hashable,
    List,
    Optional,
    Tuple,
    TypeVar,
    cast,
)
import uuid

from async_generator import asynccontextmanager
import trio
import trio_typing

from ._utils import get_task_name, iter_dag
from .abc import ManagerAPI, ServiceAPI
from .base import BaseManager
from .exceptions import DaemonTaskExit, LifecycleError, ServiceCancelled
from .stats import Stats, TaskStats
from .typing import EXC_INFO


class _Task(Hashable):
    _trio_task: Optional[trio.hazmat.Task] = None
    _cancel_scope: trio.CancelScope

    def __init__(
        self,
        name: str,
        daemon: bool,
        parent: Optional["_Task"],
        trio_task: trio.hazmat.Task = None,
    ) -> None:
        # hashable
        self._id = uuid.uuid4()

        # meta
        self.name = name
        self.daemon = daemon

        # management
        self.done = trio.Event()
        self.cancel_scope = trio.CancelScope()

        self.parent = parent

        self._trio_task = trio_task

    def __hash__(self) -> int:
        return hash(self._id)

    def __str__(self) -> str:
        return f"{self.name}[daemon={self.daemon}]"

    @property
    def has_trio_task(self) -> bool:
        return self._trio_task is not None

    @property
    def trio_task(self) -> trio.hazmat.Task:
        if self._trio_task is None:
            raise LifecycleError("Trio task not set yet")
        return self._trio_task

    @trio_task.setter
    def trio_task(self, value: trio.hazmat.Task) -> None:
        if self._trio_task is not None:
            raise LifecycleError(f"Task already set: {self._trio_task}")
        self._trio_task = value


class TrioManager(BaseManager):
    # A nursery for sub tasks and services.  This nursery is cancelled if the
    # service is cancelled but allowed to exit normally if the service exits.
    _task_nursery: trio_typing.Nursery

    _service_task_dag: Dict[_Task, List[_Task]]

    def __init__(self, service: ServiceAPI) -> None:
        super().__init__(service)

        # events
        self._started = trio.Event()
        self._cancelled = trio.Event()
        self._stopping = trio.Event()
        self._finished = trio.Event()

        # locks
        self._run_lock = trio.Lock()

        # DAG tracking
        self._service_task_dag = {}

    #
    # System Tasks
    #
    async def _handle_cancelled(self) -> None:
        self.logger.debug("%s: _handle_cancelled waiting for cancellation", self)
        await self._cancelled.wait()
        self.logger.debug("%s: _handle_cancelled triggering task cancellation", self)

        # Here we iterate over the task DAG such that we cancel the most deeply
        # nested tasks first ensuring that each has finished completely before
        # we move onto cancelling the parent tasks.
        #
        # We have to make a copy of the task dag because it is possible that
        # there is a task which has just been scheduled. In this case the new
        # task will end up being cancelled as part of it's parent task's cancel
        # scope, **or** if it was scheduled by an external API call it will be
        # cancelled as part of the global task nursery's cancellation.
        for task in iter_dag(self._service_task_dag.copy()):
            task.cancel_scope.cancel()
            await task.done.wait()

        # This finaly cancellation of the task nursery's cancel scope ensures
        # that nothing is left behind and that the service will reliably exit.
        self._task_nursery.cancel_scope.cancel()

    async def _handle_run(self) -> None:
        """
        Run and monitor the actual :meth:`ServiceAPI.run` method.

        In the event that it throws an exception the service will be cancelled.
        """
        task = _Task(
            name="run", daemon=False, parent=None, trio_task=trio.hazmat.current_task()
        )
        self._service_task_dag[task] = []

        try:
            with task.cancel_scope:
                await self._service.run()
        except Exception as err:
            self.logger.debug(
                "%s: _handle_run got error, storing exception and setting cancelled: %s",
                self,
                err,
            )
            self._errors.append(cast(EXC_INFO, sys.exc_info()))
            self.cancel()
        else:
            # NOTE: Any service which uses daemon tasks will need to trigger
            # cancellation in order for the service to exit since this code
            # path does not trigger task cancellation.  It might make sense to
            # trigger cancellation if all of the running tasks are daemon
            # tasks.
            self.logger.debug(
                "%s: _handle_run exited cleanly, waiting for full stop...", self
            )
        finally:
            task.done.set()

    @classmethod
    async def run_service(cls, service: ServiceAPI) -> None:
        manager = cls(service)
        await manager.run()

    async def run(self) -> None:
        if self._run_lock.locked():
            raise LifecycleError(
                "Cannot run a service with the run lock already engaged.  Already started?"
            )
        elif self.is_started:
            raise LifecycleError("Cannot run a service which is already started.")

        try:
            async with self._run_lock:
                async with trio.open_nursery() as system_nursery:
                    system_nursery.start_soon(self._handle_cancelled)

                    try:
                        async with trio.open_nursery() as task_nursery:
                            self._task_nursery = task_nursery

                            task_nursery.start_soon(self._handle_run)

                            self._started.set()

                            # ***BLOCKING HERE***
                            # The code flow will block here until the background tasks have
                            # completed or cancellation occurs.
                    except Exception:
                        # Exceptions from any tasks spawned by our service will be caught by trio
                        # and raised here, so we store them to report together with any others we
                        # have already captured.
                        self._errors.append(cast(EXC_INFO, sys.exc_info()))
                    finally:
                        system_nursery.cancel_scope.cancel()

        finally:
            # We need this inside a finally because a trio.Cancelled exception may be raised
            # here and it wouldn't be swalled by the 'except Exception' above.
            self._stopping.set()
            self.logger.debug("%s stopping", self)
            self._finished.set()
            self.logger.debug("%s finished", self)

        # This is outside of the finally block above because we don't want to suppress
        # trio.Cancelled or trio.MultiError exceptions coming directly from trio.
        if self.did_error:
            raise trio.MultiError(
                tuple(
                    exc_value.with_traceback(exc_tb)
                    for _, exc_value, exc_tb in self._errors
                )
            )

    #
    # Event API mirror
    #
    @property
    def is_started(self) -> bool:
        return self._started.is_set()

    @property
    def is_cancelled(self) -> bool:
        return self._cancelled.is_set()

    @property
    def is_stopping(self) -> bool:
        return self._stopping.is_set() and not self.is_finished

    @property
    def is_finished(self) -> bool:
        return self._finished.is_set()

    #
    # Control API
    #
    def cancel(self) -> None:
        if not self.is_started:
            raise LifecycleError("Cannot cancel as service which was never started.")
        elif not self.is_running:
            return
        else:
            self._cancelled.set()

    #
    # Wait API
    #
    async def wait_started(self) -> None:
        await self._started.wait()

    async def wait_stopping(self) -> None:
        await self._stopping.wait()

    async def wait_finished(self) -> None:
        await self._finished.wait()

    async def _run_and_manage_task(
        self,
        async_fn: Callable[..., Awaitable[Any]],
        *args: Any,
        daemon: bool,
        name: str,
        task: _Task,
    ) -> None:
        self.logger.debug("running task '%s[daemon=%s]'", name, daemon)

        task.trio_task = trio.hazmat.current_task()

        try:
            with task.cancel_scope:
                try:
                    await async_fn(*args)
                except Exception as err:
                    self.logger.debug(
                        "task '%s[daemon=%s]' exited with error: %s",
                        name,
                        daemon,
                        err,
                        exc_info=True,
                    )
                    self._errors.append(cast(EXC_INFO, sys.exc_info()))
                    self.cancel()
                else:
                    self.logger.debug("task '%s[daemon=%s]' finished.", name, daemon)
                    if daemon:
                        self.logger.debug(
                            "daemon task '%s' exited unexpectedly.  Cancelling service: %s",
                            name,
                            self,
                        )
                        self.cancel()
                        raise DaemonTaskExit(f"Daemon task {name} exited")
        finally:
            task.done.set()

    def _get_parent_task(self, trio_task: trio.hazmat.Task) -> Optional[_Task]:
        """
        Find the :class:`async_service.trio._Task` instance that corresponds to
        the given :class:`trio.hazmat.Task` instance.
        """
        for task in self._service_task_dag:
            # Any task that has not had it's `trio_task` set can be safely
            # skipped as those are still in the process of starting up which
            # means that they cannot be the parent task since they will not
            # have had a chance to schedule an child tasks.
            if not task.has_trio_task:
                continue
            elif trio_task is task.trio_task:
                return task
        else:
            # In the case that no tasks match we assume this is a new `root`
            # task and return `None` as the parent.
            return None

    def run_task(
        self,
        async_fn: Callable[..., Awaitable[Any]],
        *args: Any,
        daemon: bool = False,
        name: str = None,
    ) -> None:
        if not self.is_running:
            raise LifecycleError(
                "Tasks may not be scheduled if the service is not running"
            )

        task_name = get_task_name(async_fn, name)
        if self.is_running and self.is_cancelled:
            self.logger.debug(
                "Service is in the process of being cancelled. Not running task "
                "%s[daemon=%s]",
                task_name,
                daemon,
            )
            return

        task = _Task(
            name=task_name,
            daemon=daemon,
            parent=self._get_parent_task(trio.hazmat.current_task()),
        )

        if task.parent is None:
            self.logger.debug("New root task %s added to DAG", task)
        else:
            self.logger.debug("New child task %s -> %s added to DAG", task.parent, task)
            self._service_task_dag[task.parent].append(task)

        self._service_task_dag[task] = []

        self._task_nursery.start_soon(
            functools.partial(
                self._run_and_manage_task, daemon=daemon, name=task_name, task=task
            ),
            async_fn,
            *args,
            name=task_name,
        )

    def run_child_service(
        self, service: ServiceAPI, daemon: bool = False, name: str = None
    ) -> ManagerAPI:
        child_manager = type(self)(service)
        task_name = get_task_name(service, name)
        self.run_task(child_manager.run, daemon=daemon, name=task_name)
        return child_manager

    @property
    def stats(self) -> Stats:
        # The `max` call here ensures that if this is called prior to the
        # `Service.run` method starting we don't return `-1`
        total_count = max(0, len(self._service_task_dag) - 1)

        # Since we track `Service.run` as a task, the `min` call here ensures
        # that when the service is fully done that we don't represent the
        # `Service.run` method in this count.
        #
        # TODO: There is currenty a case where the service is still running but
        # the `Service.run` method has finished where this will show an
        # inflated number of finished tasks.
        finished_count = min(
            total_count,
            len([task for task in self._service_task_dag if task.done.is_set()]),
        )
        return Stats(
            tasks=TaskStats(total_count=total_count, finished_count=finished_count)
        )


TFunc = TypeVar("TFunc", bound=Callable[..., Coroutine[Any, Any, Any]])


_ChannelPayload = Tuple[Optional[Any], Optional[BaseException]]


async def _wait_stopping_or_finished(
    service: ServiceAPI,
    api_func: Callable[..., Any],
    channel: trio.abc.SendChannel[_ChannelPayload],
) -> None:
    manager = service.get_manager()

    if manager.is_stopping or manager.is_finished:
        await channel.send(
            (
                None,
                ServiceCancelled(
                    f"Cannot access external API {api_func}.  Service is not running: "
                    f"started={manager.is_started}  running={manager.is_running} "
                    f"stopping={manager.is_stopping}  finished={manager.is_finished}"
                ),
            )
        )
        return

    await manager.wait_stopping()
    await channel.send(
        (
            None,
            ServiceCancelled(
                f"Cannot access external API {api_func}.  Service is not running: "
                f"started={manager.is_started}  running={manager.is_running} "
                f"stopping={manager.is_stopping}  finished={manager.is_finished}"
            ),
        )
    )


async def _wait_api_fn(
    self: ServiceAPI,
    api_fn: Callable[..., Any],
    args: Tuple[Any, ...],
    kwargs: Dict[str, Any],
    channel: trio.abc.SendChannel[_ChannelPayload],
) -> None:
    try:
        result = await api_fn(self, *args, **kwargs)
    except Exception:
        _, exc_value, exc_tb = sys.exc_info()
        if exc_value is None or exc_tb is None:
            raise Exception(
                "This should be unreachable but acts as a type guard for mypy"
            )
        await channel.send((None, exc_value.with_traceback(exc_tb)))
    else:
        await channel.send((result, None))


def external_api(func: TFunc) -> TFunc:
    @functools.wraps(func)
    async def inner(self: ServiceAPI, *args: Any, **kwargs: Any) -> Any:
        if not hasattr(self, "manager"):
            raise ServiceCancelled(
                f"Cannot access external API {func}.  Service has not been run."
            )

        manager = self.get_manager()

        if not manager.is_running:
            raise ServiceCancelled(
                f"Cannot access external API {func}.  Service is not running: "
                f"started={manager.is_started}  running={manager.is_running} "
                f"stopping={manager.is_stopping}  finished={manager.is_finished}"
            )

        channels: Tuple[
            trio.abc.SendChannel[_ChannelPayload],
            trio.abc.ReceiveChannel[_ChannelPayload],
        ] = trio.open_memory_channel(0)
        send_channel, receive_channel = channels

        async with trio.open_nursery() as nursery:
            # mypy's type hints for start_soon break with this invocation.
            nursery.start_soon(
                _wait_api_fn, self, func, args, kwargs, send_channel  # type: ignore
            )
            nursery.start_soon(_wait_stopping_or_finished, self, func, send_channel)
            result, err = await receive_channel.receive()
            nursery.cancel_scope.cancel()
        if err is None:
            return result
        else:
            raise err

    return cast(TFunc, inner)


@asynccontextmanager
async def background_trio_service(service: ServiceAPI) -> AsyncIterator[ManagerAPI]:
    """
    Run a service in the background.

    The service is running within the context
    block and will be properly cleaned up upon exiting the context block.
    """
    async with trio.open_nursery() as nursery:
        manager = TrioManager(service)
        nursery.start_soon(manager.run)
        await manager.wait_started()
        try:
            yield manager
        finally:
            await manager.stop()
