"""RPC client and server for synchronized testdata."""
from __future__ import annotations

import logging
from contextlib import suppress
from json import dumps as jsondumps
from os import environ
from pathlib import Path
from time import perf_counter
from typing import TYPE_CHECKING, Any, ClassVar, Optional, cast

from gevent import sleep as gsleep
from gevent.event import AsyncResult
from gevent.lock import Semaphore

from grizzly.events import GrizzlyEventDecoder, GrizzlyEvents, event, events
from grizzly.types.locust import LocalRunner, MasterRunner, StopUser, WorkerRunner

from . import GrizzlyVariables
from .utils import transform
from .variables import AtomicVariablePersist

if TYPE_CHECKING:  # pragma: no cover
    from locust.rpc.protocol import Message

    from grizzly.context import GrizzlyContext
    from grizzly.scenarios import GrizzlyScenario
    from grizzly.types import TestdataType
    from grizzly.types.locust import Environment


class KeystoreDecoder(GrizzlyEventDecoder):
    def __call__(
        self,
        *args: Any,
        tags: dict[str, str | None] | None,
        return_value: Any,  # noqa: ARG002
        exception: Exception | None,
        **kwargs: Any,
    ) -> tuple[dict[str, Any], dict[str, str | None]]:
        request = args[self.arg] if isinstance(self.arg, int) else kwargs.get(self.arg)

        tags = {
            'key': request.get('key'),
            'action': request.get('action'),
            'identifier': request.get('identifier'),
            **(tags or {}),
        }

        metrics: dict[str, Any] = {'error': None}

        if exception is not None:
            metrics.update({'error': str(exception)})

        return metrics, tags


class TestdataDecoder(GrizzlyEventDecoder):
    def __call__(
        self,
        *args: Any,
        tags: dict[str, str | None] | None,
        return_value: Any,
        exception: Exception | None,
        **kwargs: Any,
    ) -> tuple[dict[str, Any], dict[str, str | None]]:
        request = args[self.arg] if isinstance(self.arg, int) else kwargs.get(self.arg)

        tags = {
            'action': (return_value or {}).get('action'),
            'identifier': request.get('identifier'),
            **(tags or {}),
        }

        metrics: dict[str, Any] = {'error': None}

        if exception is not None:
            metrics.update({'error': str(exception)})

        return metrics, tags


class TestdataConsumer:
    # need so pytest doesn't raise PytestCollectionWarning
    __test__: bool = False

    _responses: ClassVar[dict[int, AsyncResult]] = {}

    scenario: GrizzlyScenario
    runner: LocalRunner | WorkerRunner
    identifier: str
    stopped: bool
    poll_interval: float
    response: dict[str, Any]
    events: GrizzlyEvents

    semaphore = Semaphore()

    def __init__(self, runner: LocalRunner | WorkerRunner, scenario: GrizzlyScenario, poll_interval: float = 1.0) -> None:
        self.runner = runner
        self.scenario = scenario
        self.identifier = scenario.__class__.__name__

        self.stopped = False
        self.poll_interval = poll_interval

        self.response = {}
        self.logger.debug('started consumer')

    @classmethod
    def handle_response(cls, environment: Environment, msg: Message, **_kwargs: Any) -> None:  # noqa: ARG003
        uid = msg.data['uid']
        response = msg.data['response']

        cls._responses[uid].set(response)

    @property
    def logger(self) -> logging.Logger:
        return self.scenario.logger

    @event(events.testdata_request, tags={'type': 'consumer'}, decoder=TestdataDecoder(arg='request'))
    def _testdata_request(self, *, request: dict[str, Any]) -> dict[str, Any] | None:
        return self._request({'message': 'testdata', **request})

    def testdata(self) -> dict[str, Any] | None:
        request = {
            'identifier': self.identifier,
        }

        response = self._testdata_request(request=request)

        if response is None:
            self.logger.error('no testdata received')
            return None

        if response['action'] == 'stop':
            self.logger.debug('received stop command')
            return None

        if response['action'] != 'consume':
            self.logger.error('unknown action "%s" received, stopping user', response['action'])
            raise StopUser

        data = response['data']

        self.logger.debug('received: %r', data)

        variables: dict[str, Any] | None = None
        if 'variables' in data:
            variables = transform(self.scenario.user._scenario, data['variables'], objectify=True)
            del data['variables']

        data = transform(self.scenario.user._scenario, data, objectify=False)

        if variables is not None:
            data['variables'] = variables

        return cast(dict[str, Any], data)

    def keystore_get(self, key: str) -> Any | None:
        request = {
            'action': 'get',
            'key': key,
        }

        response = self._keystore_request(request=request)

        return (response or {}).get('data', None)

    def keystore_set(self, key: str, value: Any) -> None:
        request = {
            'action': 'set',
            'key': key,
            'data': value,
        }

        self._keystore_request(request=request)

    def keystore_inc(self, key: str, step: int = 1) -> int | None:
        request = {
            'action': 'inc',
            'key': key,
            'data': step,
        }

        response = self._keystore_request(request=request)

        value = (response or {}).get('data', None)

        if value is not None:
            return int(value)

        return value

    def keystore_push(self, key: str, value: Any) -> None:
        request = {
            'action': 'push',
            'key': key,
            'data': value,
        }

        self._keystore_request(request=request)

    def _keystore_pop_poll(self, request: dict[str, Any]) -> str | None:
        response = self._keystore_request(request=request)
        value: str | None = (response or {}).get('data', None)

        return value

    def keystore_pop(self, key: str, wait: int = -1) -> str:
        request = {
            'action': 'pop',
            'key': key,
        }

        value = self._keystore_pop_poll(request)

        start = perf_counter()
        while value is None:
            gsleep(self.poll_interval)
            with suppress(Exception):
                value = self._keystore_pop_poll(request)

            if value is None and wait > -1 and (int(perf_counter() - start) > wait):
                error_message = f'no message received within {wait} seconds'
                raise RuntimeError(error_message)

        return value

    def keystore_del(self, key: str) -> None:
        request = {
            'action': 'del',
            'key': key,
        }

        self._keystore_request(request=request)

    @event(events.keystore_request, tags={'type': 'consumer'}, decoder=KeystoreDecoder(arg='request'))
    def _keystore_request(self, *, request: dict[str, Any]) -> dict[str, Any] | None:
        request.update({'identifier': self.identifier})

        return self._request({'message': 'keystore', **request})

    def _request(self, request: dict[str, str]) -> dict[str, Any] | None:
        with self.semaphore:
            uid = id(self.scenario.user)

            if uid in self._responses:
                self.logger.warning('greenlet %d is already waiting for testdata', uid)

            self._responses.update({uid: AsyncResult()})
            self.runner.send_message('produce_testdata', {'uid': uid, 'cid': self.runner.client_id, 'request': request})

            # waits for async result
            try:
                return cast(Optional[dict[str, Any]], self._responses[uid].get(timeout=10.0))
            finally:
                # remove request as pending
                del self._responses[uid]


class TestdataProducer:
    # need so pytest doesn't raise PytestCollectionWarning
    __test__: bool = False

    _stopping: bool
    _persist_file: Path

    logger: logging.Logger
    semaphore = Semaphore()
    scenarios_iteration: dict[str, int]
    testdata: TestdataType
    has_persisted: bool
    keystore: dict[str, Any]
    runner: MasterRunner | LocalRunner
    grizzly: GrizzlyContext

    def __init__(self, runner: MasterRunner | LocalRunner, testdata: TestdataType) -> None:
        self.testdata = testdata
        self.runner = runner

        self.logger = logging.getLogger(f'{__name__}/producer')

        self.scenarios_iteration = {}

        self.has_persisted = False

        self.logger.debug('serving:\n%r', self.testdata)

        feature_file = environ.get('GRIZZLY_FEATURE_FILE', None)
        context_root = environ.get('GRIZZLY_CONTEXT_ROOT', None)
        assert feature_file is not None
        assert context_root is not None

        persist_root = Path(context_root) / 'persistent'
        self._persist_file = persist_root / f'{Path(feature_file).stem}.json'

        self.keystore = {}

        from grizzly.context import grizzly
        self.grizzly = grizzly

    def on_test_stop(self) -> None:
        self.logger.debug('test stopping')
        with self.semaphore:
            self.persist_data()
            for scenario_name in self.scenarios_iteration:
                self.scenarios_iteration[scenario_name] = 0

    def persist_data(self) -> None:
        if self.has_persisted:
            return

        try:
            variables_state: dict[str, dict[str, str | dict[str, Any]]] = {}

            for scenario_name, testdata in self.testdata.items():
                variable_state: dict[str, str | dict[str, Any]] = {}
                for key, variable in testdata.items():
                    if '.' not in key or variable == '__on_consumer__':
                        continue

                    with suppress(Exception):
                        _, _, variable_name, _ = GrizzlyVariables.get_variable_spec(key)

                        if not isinstance(variable, AtomicVariablePersist):
                            continue

                        variable_state.update({key: variable.generate_initial_value(variable_name)})

                if len(variable_state) > 0:
                    variables_state.update({scenario_name: variable_state})

            # only write file if we actually have something to write
            if len(variables_state) > 0:
                self._persist_file.parent.mkdir(exist_ok=True, parents=True)
                self._persist_file.write_text(jsondumps(variables_state, indent=2))
                self.logger.info('feature file data persisted in %s', self._persist_file)
                self.has_persisted = True
        except:
            self.logger.exception('failed to persist feature file data')

    def stop(self) -> None:
        self.persist_data()

    @event(events.keystore_request, tags={'type': 'producer'}, decoder=KeystoreDecoder(arg='request'))
    def _handle_request_keystore(self, *, request: dict[str, Any]) -> dict[str, Any]:  # noqa: PLR0915, PLR0912
        response = request
        key: str | None  = response.get('key', None)

        if key is None:
            message = 'key is not present in request'
            self.logger.error(message)
            response.update({'data': None, 'error': message})
            return response

        action: str | None = request.get('action')

        if action == 'get':
            response.update({'data': self.keystore.get(key, None)})
        elif action == 'set':
            set_value: str | None = response.get('data', None)

            self.keystore.update({key: set_value})
            response.update({'data': set_value})
        elif action == 'inc':
            step: int = response.get('data', 1)
            response.update({'data': None})

            inc_value: Any = self.keystore.get(key, 0)

            if isinstance(inc_value, int):
                new_value = inc_value + step
            elif isinstance(inc_value, str) and inc_value.isnumeric():
                new_value = int(inc_value) + step
            else:
                message = f'value {inc_value} for key "{key}" cannot be incremented'
                self.logger.error(message)
                response.update({'error': message})
                return response

            self.keystore.update({key: new_value})
            response.update({'data': new_value})
        elif action == 'push':
            push_value: str | None = response.get('data', None)

            if key not in self.keystore:
                self.keystore.update({key: []})

            self.keystore[key].append(push_value)
            response.update({'data': push_value})
        elif action == 'pop':
            pop_value: str | None
            response.update({'data': None})
            try:
                # since dict throws `KeyError` on pop, and str `AttributeError`
                if key in self.keystore and not isinstance(self.keystore[key], list):
                    raise AttributeError

                pop_value = self.keystore[key].pop(0)
            except AttributeError:
                message = f'key "{key}" is not a list, it has not been pushed to'
                self.logger.exception(message)
                pop_value = None
                response.update({'error': message})
            except (KeyError, IndexError):
                pop_value = None

            response.update({'data': pop_value})
        elif action == 'del':
            response.update({'data': None})
            try:
                del self.keystore[key]
            except:
                message = f'failed to remove key "{key}"'
                self.logger.exception(message)
                response.update({'error': message})
        else:
            message = f'received unknown keystore action "{action}"'
            self.logger.error(message)
            response.update({'data': None, 'error': message})

        return response

    @event(events.testdata_request, tags={'type': 'producer'}, decoder=TestdataDecoder(arg='request'))
    def _handle_request_testdata(self, *, request: dict[str, Any]) -> dict[str, Any]:  # noqa: PLR0912
        scenario_name = request.get('identifier', '')
        response: dict[str, Any] = {
            'action': 'stop',
        }

        try:
            scenario = self.grizzly.scenarios.find_by_class_name(scenario_name)

            if scenario is not None:
                if scenario_name not in self.scenarios_iteration and scenario.iterations > 0:
                    self.scenarios_iteration[scenario_name] = 0

                if not (
                    scenario_name in self.scenarios_iteration
                    and self.scenarios_iteration[scenario_name] < scenario.iterations
                ) or scenario_name not in self.scenarios_iteration:
                    return response

                testdata = self.testdata.get(scenario_name, {})
                response['action'] = 'consume'
                data: dict[str, Any] = {'variables': {}}
                loaded_variable_datatypes: dict[str, Any] = {}

                for key, variable in testdata.items():
                    if '.' in key and variable != '__on_consumer__':
                        module_name, variable_type, variable_name, _ = GrizzlyVariables.get_variable_spec(key)
                        _, data_attribute = key.rsplit('.', 1)

                        if variable_name != data_attribute:
                            testdata_type = f'{variable_type}.{variable_name}'
                            if module_name != 'grizzly.testdata.variables':
                                testdata_type = f'{module_name}.{testdata_type}'

                            if testdata_type not in loaded_variable_datatypes:
                                try:
                                    loaded_variable_datatypes[testdata_type] = variable[variable_name]
                                except NotImplementedError:
                                    continue

                            value = loaded_variable_datatypes[testdata_type][data_attribute]
                        else:
                            try:
                                value = variable[variable_name]
                            except NotImplementedError:
                                continue
                    else:
                        value = variable

                    if value is None and scenario_name not in self.scenarios_iteration:
                        response['action'] = 'stop'
                        self.logger.warning('%s does not have a value and iterations is not set for %s, stop test', key, scenario_name)
                        data = {}
                        break

                    data['variables'][key] = value

                    alias = scenario.variables.alias.get(key, None)
                    if alias is not None:
                        data_key = alias
                        data[data_key] = value

                response['data'] = data

                if scenario_name in self.scenarios_iteration:
                    self.scenarios_iteration[scenario_name] += 1
                    self.logger.debug('%s: iteration=%d', scenario_name, self.scenarios_iteration[scenario_name])
        except TypeError:
            response = {
                'action': 'stop',
            }
            self.logger.exception('test data error, stop consumer %s', scenario_name)

        return response

    def handle_request(self, environment: Environment, msg: Message, **_kwargs: Any) -> None:  # noqa: ARG002
        with self.semaphore:
            self.logger.debug('handling message')
            uid = msg.data['uid']
            cid = msg.data['cid']
            request = msg.data['request']

            if request['message'] == 'keystore':
                response = self._handle_request_keystore(request=request)
            elif request['message'] == 'testdata':
                response = self._handle_request_testdata(request=request)
            else:
                self.logger.error('received unknown message "%s"', request['message'])
                response = {}

            self.runner.send_message('consume_testdata', {'uid': uid, 'response': response}, client_id=cid)
