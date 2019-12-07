import asyncio
import inspect
import logging
import time
from collections import defaultdict
from datetime import timedelta
from itertools import chain
from typing import List, Tuple, Coroutine, Union, Sequence, TYPE_CHECKING, Callable

import janus

from lightbus.api import Api, ApiRegistry
from lightbus.client.event_client import _EventListener
from lightbus.exceptions import (
    InvalidEventArguments,
    UnknownApi,
    EventNotFound,
    InvalidEventListener,
    SuddenDeathException,
    LightbusTimeout,
    LightbusServerError,
    NoApisToListenOn,
    InvalidName,
    InvalidSchedule,
    BusAlreadyClosed,
    TransportIsClosed,
    UnsupportedUse,
)
from lightbus.internal_apis import LightbusStateApi, LightbusMetricsApi
from lightbus.log import LBullets, L, Bold
from lightbus.mediator.commands import SendEventCommand
from lightbus.message import RpcMessage, ResultMessage, EventMessage, Message
from lightbus.plugins import PluginRegistry
from lightbus.schema import Schema
from lightbus.schema.schema import _parameter_names
from lightbus.transports import RpcTransport
from lightbus.utilities.async_tools import (
    block,
    get_event_loop,
    cancel,
    make_exception_checker,
    call_every,
    call_on_schedule,
    run_user_provided_callable,
)
from lightbus.utilities.casting import cast_to_signature
from lightbus.utilities.deforming import deform_to_bus
from lightbus.utilities.features import Feature, ALL_FEATURES
from lightbus.utilities.frozendict import frozendict
from lightbus.utilities.human import human_time

if TYPE_CHECKING:
    # pylint: disable=unused-import,cyclic-import
    from schedule import Job
    from lightbus.config import Config

__all__ = ["BusClient"]


logger = logging.getLogger(__name__)


class BusClient:
    """Provides a the lower level interface for accessing the bus

    The low-level `BusClient` is less expressive than the interface provided by `BusPath`,
    but does allow for more control in some situations.

    All functionality in `BusPath` is provided by `BusClient`.
    """

    def __init__(
        self,
        config: "Config",
        schema: Schema,
        plugin_registry: PluginRegistry,
        features: Sequence[Union[Feature, str]] = ALL_FEATURES,
    ):
        self._event_listeners: List[_EventListener] = []  # Event listeners
        self._consumers = []  # RPC consumers
        # Coroutines added via schedule/every/add_background_task which should be started up
        # once the server starts
        self._background_coroutines = []
        # Tasks produced from the values in self._background_coroutines. Will be closed on bus shutdown
        self._background_tasks = []
        self._hook_callbacks = defaultdict(list)
        self.config = config
        self.features: List[Union[Feature, str]] = ALL_FEATURES
        self.set_features(list(features))
        self.api_registry = ApiRegistry()
        self.schema = None
        self._server_shutdown_queue: janus.Queue = None
        self._shutdown_monitor_task = None
        self.exit_code = 0
        self._closed = False
        self._server_tasks = []
        self._lazy_load_complete = False
        self.schema = schema
        self.plugin_registry = plugin_registry

    def close(self):
        """Close the bus client

        This will cancel all tasks and close all transports/connections
        """
        block(self.close_async())

    async def close_async(self):
        """Async version of close()
        """
        try:
            if self._closed:
                raise BusAlreadyClosed()

            listener_tasks = [
                task for task in asyncio.all_tasks() if getattr(task, "is_listener", False)
            ]

            for task in chain(listener_tasks, self._background_tasks):
                # pylint: disable=broad-except
                try:
                    await cancel(task)
                except Exception as e:
                    logger.exception(e)

            for transport in self.transport_registry.get_all_transports():
                await transport.close()

            await self.schema.schema_transport.close()

            self._closed = True
        finally:
            await self._transport_invoker.stop()

    @property
    def loop(self):
        return get_event_loop()

    def run_forever(self):
        if not self.api_registry.all() and Feature.RPCS in self.features:
            logger.info("Disabling serving of RPCs as no APIs have been registered")
            self.features.remove(Feature.RPCS)

        self.start_server()

        self._actually_run_forever()
        logger.debug("Main thread event loop was stopped")

        # Stopping the server requires access to the worker,
        # so do this first
        logger.debug("Stopping server")
        self.stop_server()

        # Here we close connections and shutdown the worker thread
        logger.debug("Closing bus")
        self.close()

    def shutdown_server(self, exit_code):
        if self._server_shutdown_queue is not None:
            # If this shutdown queue *is* None, then it is safe to assume
            # the server hasn't started up yet, so no need to
            # put anything in the shutdown queue
            self._server_shutdown_queue.sync_q.put(exit_code)

    def start_server(self):
        """Server startup procedure

        Must be called from within the main thread. Handles the niceties around
        starting and stopping the server. The interesting setup happens in
        BusClient._setup_server()
        """
        self.welcome_message()

        # Ensure an event loop exists
        get_event_loop()

        self._server_shutdown_queue = janus.Queue()
        self._server_tasks = set()

        async def server_shutdown_monitor():
            exit_code = await self._server_shutdown_queue.async_q.get()
            self.exit_code = exit_code
            self.loop.stop()
            self._server_shutdown_queue.async_q.task_done()

        shutdown_monitor_task = asyncio.ensure_future(server_shutdown_monitor())
        shutdown_monitor_task.add_done_callback(make_exception_checker(self, die=True))
        self._shutdown_monitor_task = shutdown_monitor_task

        logger.info(
            LBullets(
                f"Enabled features ({len(self.features)})", items=[f.value for f in self.features]
            )
        )

        disabled_features = set(ALL_FEATURES) - set(self.features)
        logger.info(
            LBullets(
                f"Disabled features ({len(disabled_features)})",
                items=[f.value for f in disabled_features],
            )
        )

        block(self._setup_server())

    async def _setup_server(self):
        self.api_registry.add(LightbusStateApi())
        self.api_registry.add(LightbusMetricsApi())

        logger.info(
            LBullets(
                "APIs in registry ({})".format(len(self.api_registry.all())),
                items=self.api_registry.names(),
            )
        )

        # Push all registered APIs into the global schema
        for api in self.api_registry.all():
            await self.schema.add_api(api)

        # We're running as a server now (e.g. lightbus run), so
        # do the lazy loading immediately
        await self.lazy_load_now()

        # Setup schema monitoring
        monitor_task = asyncio.ensure_future(self.schema.monitor())
        monitor_task.add_done_callback(make_exception_checker(self, die=True))

        logger.info("Executing before_worker_start & on_start hooks...")
        await self._execute_hook("before_worker_start")
        logger.info("Execution of before_worker_start & on_start hooks was successful")

        # Setup RPC consumption
        if Feature.RPCS in self.features:
            consume_rpc_task = asyncio.ensure_future(self.consume_rpcs())
            consume_rpc_task.add_done_callback(make_exception_checker(self, die=True))
        else:
            consume_rpc_task = None

        # Start off any registered event listeners
        if Feature.EVENTS in self.features:
            for event_listener in self._event_listeners:
                event_listener.start_task(bus_client=self)

        # Start off any background tasks
        if Feature.TASKS in self.features:
            for coroutine in self._background_coroutines:
                task = asyncio.ensure_future(coroutine)
                task.add_done_callback(make_exception_checker(self, die=True))
                self._background_tasks.append(task)

        self._server_tasks = [consume_rpc_task, monitor_task]

    def stop_server(self):
        block(cancel(self._shutdown_monitor_task))
        block(self._stop_server_inner())

    async def _stop_server_inner(self):
        # Cancel the tasks we created above
        await cancel(*self._server_tasks)

        logger.info("Executing after_worker_stopped & on_stop hooks...")
        await self._execute_hook("after_worker_stopped")
        logger.info("Execution of after_worker_stopped & on_stop hooks was successful")

    def _actually_run_forever(self):  # pragma: no cover
        """Simply start the loop running forever

        This just makes testing easier as we can mock out this method
        """
        self.loop.run_forever()

    async def lazy_load_now(self):
        """Perform lazy tasks immediately

        When lightbus is used as a client it performs network tasks
        lazily. This speeds up import of your bus module, and prevents
        getting surprising errors at import time.

        However, in some cases you may wish to hurry up these lazy tasks
        (or perform them at a known point). In which case you can call this
        method to execute them immediately.
        """
        if self._lazy_load_complete:
            return

        # 1. Load the schema
        logger.debug("Loading schema...")
        await self.schema.ensure_loaded_from_bus()

        logger.info(
            LBullets(
                "Loaded the following remote schemas ({})".format(len(self.schema.remote_schemas)),
                items=self.schema.remote_schemas.keys(),
            )
        )

        # 2. Add any local APIs to the schema
        for api in self.api_registry.all():
            await self.schema.add_api(api)

        logger.info(
            LBullets(
                "Loaded the following local schemas ({})".format(len(self.schema.remote_schemas)),
                items=self.schema.local_schemas.keys(),
            )
        )

        # 3. Open the transports
        for transport in self.transport_registry.get_all_transports():
            await transport.open()

        # 4. Done
        self._lazy_load_complete = True

    # RPCs

    async def consume_rpcs(self, apis: List[Api] = None):
        """Start a background task to consume RPCs

        This will consumer RPCs on APIs which have been registered with this
        bus client.
        """
        await self.lazy_load_now()

        if apis is None:
            apis = self.api_registry.all()

        if not apis:
            raise NoApisToListenOn(
                "No APIs to consume on in consume_rpcs(). Either this method was called with apis=[], "
                "or the API registry is empty."
            )

        # Not all APIs will necessarily be served by the same transport, so group them
        # accordingly
        api_names = [api.meta.name for api in apis]
        api_names_by_transport = self.transport_registry.get_rpc_transports(api_names)

        coroutines = []
        for rpc_transport, transport_api_names in api_names_by_transport:
            transport_apis = list(map(self.api_registry.get, transport_api_names))
            coroutines.append(
                self._consume_rpcs_with_transport(rpc_transport=rpc_transport, apis=transport_apis)
            )

        task = asyncio.ensure_future(asyncio.gather(*coroutines))
        task.add_done_callback(make_exception_checker(self, die=True))
        self._consumers.append(task)

    async def _consume_rpcs_with_transport(
        self, rpc_transport: RpcTransport, apis: List[Api] = None
    ):
        # TODO: Invoker command
        await self.lazy_load_now()

        while True:
            try:
                rpc_messages = await rpc_transport.consume_rpcs(apis, bus_client=self)
            except TransportIsClosed:
                return

            for rpc_message in rpc_messages:
                self._validate(rpc_message, "incoming")

                await self._execute_hook("before_rpc_execution", rpc_message=rpc_message)
                try:
                    result = await self._call_rpc_local(
                        api_name=rpc_message.api_name,
                        name=rpc_message.procedure_name,
                        kwargs=rpc_message.kwargs,
                    )
                except SuddenDeathException:
                    # Used to simulate message failure for testing
                    return
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    result = e
                else:
                    result = deform_to_bus(result)

                result_message = ResultMessage(result=result, rpc_message_id=rpc_message.id)
                await self._execute_hook(
                    "after_rpc_execution", rpc_message=rpc_message, result_message=result_message
                )

                if not result_message.error:
                    self._validate(
                        result_message,
                        "outgoing",
                        api_name=rpc_message.api_name,
                        procedure_name=rpc_message.procedure_name,
                    )

                await self.send_result(rpc_message=rpc_message, result_message=result_message)

    async def call_rpc_remote(
        self, api_name: str, name: str, kwargs: dict = frozendict(), options: dict = frozendict()
    ):
        """ Perform an RPC call

        Call an RPC and return the result.
        """
        # TODO: Invoker command
        await self.lazy_load_now()

        rpc_transport = self.transport_registry.get_rpc_transport(api_name)
        result_transport = self.transport_registry.get_result_transport(api_name)

        kwargs = deform_to_bus(kwargs)
        rpc_message = RpcMessage(api_name=api_name, procedure_name=name, kwargs=kwargs)
        return_path = result_transport.get_return_path(rpc_message)
        rpc_message.return_path = return_path
        options = options or {}
        timeout = options.get("timeout", self.config.api(api_name).rpc_timeout)
        # TODO: rpc_timeout is in three different places in the config!
        #       Fix this. Really it makes most sense for the use if it goes on the
        #       ApiConfig rather than having to repeat it on both the result & RPC
        #       transports.
        self._validate_name(api_name, "rpc", name)

        logger.info("📞  Calling remote RPC {}.{}".format(Bold(api_name), Bold(name)))

        start_time = time.time()
        # TODO: It is possible that the RPC will be called before we start waiting for the
        #       response. This is bad.

        self._validate(rpc_message, "outgoing")

        future = asyncio.gather(
            self.receive_result(rpc_message, return_path, options=options),
            rpc_transport.call_rpc(rpc_message, options=options, bus_client=self),
        )

        await self._execute_hook("before_rpc_call", rpc_message=rpc_message)

        try:
            result_message, _ = await asyncio.wait_for(future, timeout=timeout)
            future.result()
        except asyncio.TimeoutError:
            # Allow the future to finish, as per https://bugs.python.org/issue29432
            try:
                await future
                future.result()
            except asyncio.CancelledError:
                pass

            # TODO: Remove RPC from queue. Perhaps add a RpcBackend.cancel() method. Optional,
            #       as not all backends will support it. No point processing calls which have timed out.
            raise LightbusTimeout(
                f"Timeout when calling RPC {rpc_message.canonical_name} after {timeout} seconds. "
                f"It is possible no Lightbus process is serving this API, or perhaps it is taking "
                f"too long to process the request. In which case consider raising the 'rpc_timeout' "
                f"config option."
            ) from None

        await self._execute_hook(
            "after_rpc_call", rpc_message=rpc_message, result_message=result_message
        )

        if not result_message.error:
            logger.info(
                L(
                    "🏁  Remote call of {} completed in {}",
                    Bold(rpc_message.canonical_name),
                    human_time(time.time() - start_time),
                )
            )
        else:
            logger.warning(
                L(
                    "⚡ Server error during remote call of {}. Took {}: {}",
                    Bold(rpc_message.canonical_name),
                    human_time(time.time() - start_time),
                    result_message.result,
                )
            )
            raise LightbusServerError(
                "Error while calling {}: {}\nRemote stack trace:\n{}".format(
                    rpc_message.canonical_name, result_message.result, result_message.trace
                )
            )

        self._validate(result_message, "incoming", api_name, procedure_name=name)

        return result_message.result

    async def _call_rpc_local(self, api_name: str, name: str, kwargs: dict = frozendict()):
        # TODO: Invoker command
        await self.lazy_load_now()

        api = self.api_registry.get(api_name)
        self._validate_name(api_name, "rpc", name)

        start_time = time.time()
        try:
            method = getattr(api, name)
            if self.config.api(api_name).cast_values:
                kwargs = cast_to_signature(kwargs, method)
            result = await run_user_provided_callable(
                method, args=[], kwargs=kwargs, bus_client=self
            )
        except (asyncio.CancelledError, SuddenDeathException):
            raise
        except Exception as e:
            logging.exception(e)
            logger.warning(
                L(
                    "⚡  Error while executing {}.{}. Took {}",
                    Bold(api_name),
                    Bold(name),
                    human_time(time.time() - start_time),
                )
            )
            raise
        else:
            logger.info(
                L(
                    "⚡  Executed {}.{} in {}",
                    Bold(api_name),
                    Bold(name),
                    human_time(time.time() - start_time),
                )
            )
            return result

    # Events

    async def fire_event(self, api_name, name, kwargs: dict = None, options: dict = None):
        """Fire an event onto the bus"""
        return self.event_client.listen(
            api_name=api_name, name=name, kwargs=kwargs, options=options
        )

    def listen_for_event(
        self, api_name: str, name: str, listener: Callable, listener_name: str, options: dict = None
    ):
        """Listen for a single event

        Wraps `listen_for_events()`
        """
        self.listen_for_events(
            [(api_name, name)], listener, listener_name=listener_name, options=options
        )

    def listen_for_events(
        self,
        events: List[Tuple[str, str]],
        listener: Callable,
        listener_name: str,
        options: dict = None,
    ):
        """Listen for a list of events

        `events` is in the form:

            events=[
                ('company.first_api', 'event_name'),
                ('company.second_api', 'event_name'),
            ]

        `listener_name` is an arbitrary string which uniquely identifies this listener.
        This can generally be the same as the function name of the `listener` callable, but
        it should not change once deployed.
        """
        return self.event_client.listen(
            events=events, listener=listener, listener_name=listener_name, options=options
        )

    # Results

    async def send_result(self, rpc_message: RpcMessage, result_message: ResultMessage):
        # TODO: Invoker command
        await self.lazy_load_now()
        result_transport = self.transport_registry.get_result_transport(rpc_message.api_name)
        return await result_transport.send_result(
            rpc_message, result_message, rpc_message.return_path, bus_client=self
        )

    async def receive_result(self, rpc_message: RpcMessage, return_path: str, options: dict):
        # TODO: Invoker command
        await self.lazy_load_now()
        result_transport = self.transport_registry.get_result_transport(rpc_message.api_name)
        return await result_transport.receive_result(
            rpc_message, return_path, options, bus_client=self
        )

    def add_background_task(self, coroutine: Union[Coroutine, asyncio.Future]):
        """Run a coroutine in the background

        The provided coroutine will be run in the background once
        Lightbus startup is complete.

        The coroutine will be cancelled when the bus client is closed.

        The Lightbus process will exit if the coroutine raises an exception.
        See lightbus.utilities.async_tools.check_for_exception() for details.
        """

        # Store coroutine for starting once the server starts
        self._background_coroutines.append(coroutine)

    # Utilities

    def set_features(self, features: List[Union[Feature, str]]):
        """Set the features this bus clients should serve.

        Features should be a list of: `rpcs`, `events`, `tasks`
        """
        features = list(features)
        for i, feature in enumerate(features):
            try:
                features[i] = Feature(feature)
            except ValueError:
                features_str = ", ".join([f.value for f in Feature])
                raise UnsupportedUse(f"Feature '{feature}' is not one of: {features_str}\n")

        self.features = features

    # Hooks

    async def _execute_hook(self, name, **kwargs):
        # Hooks that need to run before plugins
        for callback in self._hook_callbacks[(name, True)]:
            await run_user_provided_callable(
                callback, args=[], kwargs=dict(client=self, **kwargs), bus_client=self
            )

        await self.plugin_registry.execute_hook(name, client=self, **kwargs)

        # Hooks that need to run after plugins
        for callback in self._hook_callbacks[(name, False)]:
            await run_user_provided_callable(
                callback, args=[], kwargs=dict(client=self, **kwargs), bus_client=self
            )

    def _register_hook_callback(self, name, fn, before_plugins=False):
        self._hook_callbacks[(name, bool(before_plugins))].append(fn)

    def _make_hook_decorator(self, name, before_plugins=False, callback=None):
        if callback and not callable(callback):
            raise AssertionError("The provided callback is not callable")
        if callback:
            self._register_hook_callback(name, callback, before_plugins)
            return None
        else:

            def hook_decorator(fn):
                self._register_hook_callback(name, fn, before_plugins)
                return fn

            return hook_decorator

    def on_start(self, callback=None, *, before_plugins=False):
        """Decorator to register a function to be called before the worker starts up

        Callback will be called with the following arguments:

            callback(self, *, client: "BusClient")
        """
        return self.before_worker_start(callback, before_plugins=before_plugins)

    def on_stop(self, callback=None, *, before_plugins=False):
        """Decorator to register a function to be called after the worker stops

        Callback will be called with the following arguments:

            callback(self, *, client: "BusClient")
        """
        return self.before_worker_start(callback, before_plugins=before_plugins)

    def before_worker_start(self, callback=None, *, before_plugins=False):
        """Decorator to register a function to be called before the worker starts up

        See `on_start()`
        """
        return self._make_hook_decorator("before_worker_start", before_plugins, callback)

    def after_worker_stopped(self, callback=None, *, before_plugins=False):
        """Decorator to register a function to be called after the worker stops

        See `on_stop()`
        """
        return self._make_hook_decorator("after_worker_stopped", before_plugins, callback)

    def before_rpc_call(self, callback=None, *, before_plugins=False):
        """Decorator to register a function to be called prior to an RPC call

        Callback will be called with the following arguments:

            callback(self, *, rpc_message: RpcMessage, client: "BusClient")
        """
        return self._make_hook_decorator("before_rpc_call", before_plugins, callback)

    def after_rpc_call(self, callback=None, *, before_plugins=False):
        """Decorator to register a function to be called after an RPC call

        Callback will be called with the following arguments:

            callback(self, *, rpc_message: RpcMessage, result_message: ResultMessage, client: "BusClient")
        """
        return self._make_hook_decorator("after_rpc_call", before_plugins, callback)

    def before_rpc_execution(self, callback=None, *, before_plugins=False):
        """Decorator to register a function to be called prior to a local RPC execution

        Callback will be called with the following arguments:

            callback(self, *, rpc_message: RpcMessage, client: "BusClient")
        """
        return self._make_hook_decorator("before_rpc_execution", before_plugins, callback)

    def after_rpc_execution(self, callback=None, *, before_plugins=False):
        """Decorator to register a function to be called after a local RPC execution

        Callback will be called with the following arguments:

            callback(self, *, rpc_message: RpcMessage, result_message: ResultMessage, client: "BusClient")
        """
        return self._make_hook_decorator("after_rpc_execution", before_plugins, callback)

    def before_event_sent(self, callback=None, *, before_plugins=False):
        """Decorator to register a function to be called prior to an event being sent

        Callback will be called with the following arguments:

            callback(self, *, event_message: EventMessage, client: "BusClient")
        """
        return self._make_hook_decorator("before_event_sent", before_plugins, callback)

    def after_event_sent(self, callback=None, *, before_plugins=False):
        """Decorator to register a function to be called after an event was sent

        Callback will be called with the following arguments:

            callback(self, *, event_message: EventMessage, client: "BusClient")
        """
        return self._make_hook_decorator("after_event_sent", before_plugins, callback)

    def before_event_execution(self, callback=None, *, before_plugins=False):
        """Decorator to register a function to be called prior to a local event handler execution

        Callback will be called with the following arguments:

            callback(self, *, event_message: EventMessage, client: "BusClient")
        """
        return self._make_hook_decorator("before_event_execution", before_plugins, callback)

    def after_event_execution(self, callback=None, *, before_plugins=False):
        """Decorator to register a function to be called after a local event handler execution

        Callback will be called with the following arguments:

            callback(self, *, event_message: EventMessage, client: "BusClient")
        """
        return self._make_hook_decorator("after_event_execution", before_plugins, callback)

    # Scheduling

    def every(
        self,
        *,
        seconds=0,
        minutes=0,
        hours=0,
        days=0,
        also_run_immediately=False,
        **timedelta_extra,
    ):
        """ Call a coroutine at the specified interval

        This is a simple scheduling mechanism which you can use in your bus module to setup
        recurring tasks. For example:

            bus = lightbus.create()

            @bus.client.every(seconds=30)
            def my_func():
                print("Hello")

        This can also be used to decorate async functions. In this case the function will be awaited.

        Note that the timing is best effort and is not guaranteed. That being said, execution
        time is accounted for.

        See Also:

            @bus.client.schedule()
        """
        td = timedelta(seconds=seconds, minutes=minutes, hours=hours, days=days, **timedelta_extra)

        if td.total_seconds() == 0:
            raise InvalidSchedule(
                "The @bus.client.every() decorator must be provided with a non-zero time argument. "
                "Ensure you are passing at least one time argument, and that it has a non-zero value."
            )

        # TODO: There is an argument that the backgrounding of this should be done only after
        #       on_start() has been fired. Otherwise this will be run before the on_start() setup
        #       has happened in cases where also_run_immediately=True.
        def wrapper(f):
            coroutine = call_every(  # pylint: assignment-from-no-return
                callback=f, timedelta=td, also_run_immediately=also_run_immediately, bus_client=self
            )
            self.add_background_task(coroutine)
            return f

        return wrapper

    def schedule(self, schedule: "Job", also_run_immediately=False):
        """ Call a coroutine on the specified schedule

        Schedule a task using the `schedule` library:

            import lightbus
            import schedule

            bus = lightbus.create()

            # Run the task every 1-3 seconds, varying randomly
            @bus.client.schedule(schedule.every(1).to(3).seconds)
            def do_it():
                print("Hello using schedule library")

        This can also be used to decorate async functions. In this case the function will be awaited.

        See Also:

            @bus.client.every()
        """

        def wrapper(f):
            coroutine = call_on_schedule(
                callback=f,
                schedule=schedule,
                also_run_immediately=also_run_immediately,
                bus_client=self,
            )
            self.add_background_task(coroutine)
            return f

        return wrapper

    # API registration

    def register_api(self, api: Api):
        """Register an API with this bus client

        You must register APIs which you wish this server to fire events
        on or handle RPCs calls for.

        See Also: https://lightbus.org/explanation/apis/
        """
        self.api_registry.add(api)