"""@anchor pydoc:grizzly.tasks.keystore Keystore task
This tasks sets and gets values from a distributed keystore. This makes is possible to share values between scenarios.

Retreived (get) values are rendered before setting the variable.
Stored (set) values are not rendered, so it is possible to store templates.


## Step implementations

* {@pylink grizzly.steps.scenario.tasks.keystore.step_task_keystore_get}

* {@pylink grizzly.steps.scenario.tasks.keystore.step_task_keystore_get_default}

* {@pylink grizzly.steps.scenario.tasks.keystore.step_task_keystore_set}

* {@pylink grizzly.steps.scenario.tasks.keystore.step_task_keystore_inc_default_step}

## Statistics

This task only has request statistics entry, of type `KEYS`, if a key (without `default_value`) that does not have a value set is retrieved.

## Arguments

* `key` _str_: name of key in keystore

* `action` _Action_: literal `set` or `get`

* `action_context` _str | Any_: when `action` is `get` it must be a `str` (variable name), for `set` any goes (as long as it is json serializable and not `None`)

* `default_value` _Any (Optional)_: used when `action` is `get` and `key` does not exist in the keystore
"""
from __future__ import annotations

from json import dumps as jsondumps
from json import loads as jsonloads
from typing import TYPE_CHECKING, Any, Literal, Optional, Union, cast, get_args

from . import GrizzlyTask, grizzlytask, template

if TYPE_CHECKING:  # pragma: no cover
    from grizzly.scenarios import GrizzlyScenario

Action = Literal['get', 'set', 'inc']


@template('action_context')
class KeystoreTask(GrizzlyTask):
    key: str
    action: Action
    action_context: Union[str, Optional[Any]]
    default_value: Optional[Any]

    def __init__(self, key: str, action: Action, action_context: Union[str, Any], default_value: Optional[Any] = None) -> None:
        super().__init__()

        self.key = key
        self.action = action
        self.action_context = action_context
        self.default_value = default_value

        try:
            assert self.action in get_args(Action), f'{self.action} is not a valid action'

            if self.action in ['get', 'inc']:
                assert isinstance(self.action_context, str), f'action context for {self.action} must be a string'
                assert action_context in self.grizzly.state.variables, f'variable "{action_context}" has not been initialized'
            else:  # == 'set'
                assert self.action_context is not None, 'action context for set cannot be None'
        except AssertionError as e:
            raise RuntimeError(str(e)) from e

    def __call__(self) -> grizzlytask:
        @grizzlytask
        def task(parent: GrizzlyScenario) -> Any:
            try:
                if self.action == 'get':
                    value = parent.consumer.keystore_get(self.key)

                    if value is None and self.default_value is not None:
                        parent.consumer.keystore_set(self.key, self.default_value)
                        value = cast(Any, self.default_value)

                    if value is not None:
                        parent.user._context['variables'][self.action_context] = jsonloads(parent.render(jsondumps(value)))
                    else:
                        message = f'key {self.key} does not exist in keystore'
                        raise RuntimeError(message)
                elif self.action == 'inc':
                    value = parent.consumer.keystore_inc(self.key, step=1)

                    if value is not None:
                        parent.user._context['variables'][self.action_context] = jsonloads(parent.render(jsondumps(value)))
                    else:
                        message = f'key {self.key} does not exist in keystore'
                        raise RuntimeError(message)
                elif self.action == 'set':
                    # do not render set values, might want it to be a template
                    parent.consumer.keystore_set(self.key, self.action_context)
                else:  # pragma: no cover
                    pass
            except Exception as e:
                parent.user.logger.exception('keystore action %s failed', self.action)
                parent.user.environment.events.request.fire(
                    request_type='KEYS',
                    name=f'{parent.user._scenario.identifier} {self.key}',
                    response_time=0,
                    response_length=1,
                    context=parent.user._context,
                    exception=e,
                )

                if parent.user._scenario.failure_exception is not None:
                    raise parent.user._scenario.failure_exception from e

        return task
