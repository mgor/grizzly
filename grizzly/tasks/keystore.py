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

* `default_value` _Any | None_: used when `action` is `get` and `key` does not exist in the keystore

Values for `set` and `push` operations are not rendered by default, they will be pushed as is. By using argument `render`, it is possible to change this behaviour, e.g.:

```gherkin
Given value of variable "identification" is "foobar"
Then push "processed" in keystore with value "{{ identification }} | render=True"
```
"""
from __future__ import annotations

from json import JSONDecodeError
from json import dumps as jsondumps
from json import loads as jsonloads
from typing import TYPE_CHECKING, Any, Literal, cast, get_args

from grizzly.exceptions import failure_handler
from grizzly.testdata import GrizzlyVariables
from grizzly.utils import has_template
from grizzly_extras.arguments import parse_arguments, split_value
from grizzly_extras.text import has_separator

from . import GrizzlyTask, grizzlytask, template

if TYPE_CHECKING:  # pragma: no cover
    from grizzly.scenarios import GrizzlyScenario

Action = Literal['get', 'set', 'inc', 'push', 'pop', 'del']


@template('action_context', 'key')
class KeystoreTask(GrizzlyTask):
    key: str
    action: Action
    action_context: str | Any | None
    default_value: Any | None

    arguments: dict[str, Any]

    def __init__(self, key: str, action: Action, action_context: str | None, default_value: str | None = None) -> None:
        super().__init__(timeout=None)

        self.key = key
        self.action = action
        self.action_context = action_context
        self.default_value = self.json_serialize(default_value)
        self.arguments = {}

        if self.action_context is not None and has_separator('|', self.action_context):
            self.action_context, value_arguments = split_value(self.action_context)
            arguments = parse_arguments(value_arguments, unquote=True)
            for key, value in arguments.items():
                self.arguments.update({key: GrizzlyVariables.guess_datatype(value)})

        assert self.action in get_args(Action), f'"{self.action}" is not a valid action'

        if self.action in ['get', 'inc', 'pop']:
            assert isinstance(self.action_context, str), f'action context for "{self.action}" must be a string'
            assert action_context in self.grizzly.scenario.variables, f'variable "{action_context}" has not been initialized'
        elif self.action in ['set', 'push']:
            assert self.action_context is not None, f'action context for "{self.action}" must be declared'
            self.action_context = self.json_serialize(self.action_context)
        elif self.action in ['del']:
            assert self.action_context is None, f'action context for "{self.action}" cannot be declared'
        else:  # pragma: no cover
            pass

    @classmethod
    def json_serialize(cls, value: str | None) -> str | None:
        if value is None:
            return value

        if not has_template(value):
            if "'" in value:
                value = value.replace("'", '"')

            try:
                value = jsonloads(value)
            except JSONDecodeError as e:
                message = f'"{value}" is not valid JSON'
                raise AssertionError(message) from e

        return value

    def __call__(self) -> grizzlytask:
        @grizzlytask
        def task(parent: GrizzlyScenario) -> Any:
            key = parent.user.render(self.key)

            try:
                if self.action == 'get':
                    value = parent.consumer.keystore_get(key)

                    if value is None and self.default_value is not None:
                        parent.consumer.keystore_set(key, self.default_value)
                        value = cast(Any, self.default_value)

                    if value is not None and self.action_context is not None:
                        parent.user.set_variable(self.action_context, jsonloads(parent.user.render(jsondumps(value))))
                    else:
                        message = f'key {key} does not exist in keystore'
                        raise RuntimeError(message)
                elif self.action == 'inc':
                    value = parent.consumer.keystore_inc(key, step=1)

                    if value is not None and self.action_context is not None:
                        parent.user.set_variable(self.action_context, jsonloads(parent.user.render(jsondumps(value))))
                    else:
                        message = f'key {key} does not exist in keystore'
                        raise RuntimeError(message)
                elif self.action == 'set':
                    value = parent.user.render(cast(str, self.action_context)) if self.arguments.get('render', False) else self.action_context
                    parent.consumer.keystore_set(key, value)
                elif self.action == 'push':
                    value = parent.user.render(cast(str, self.action_context)) if self.arguments.get('render', False) else self.action_context
                    parent.consumer.keystore_push(key, value)
                elif self.action == 'pop':
                    value = parent.consumer.keystore_pop(key)
                    if value is not None and self.action_context is not None:
                        parent.user.set_variable(self.action_context, jsonloads(parent.user.render(jsondumps(value))))
                elif self.action == 'del':
                    parent.consumer.keystore_del(key)
                else:  # pragma: no cover
                    pass
            except Exception as e:
                parent.user.logger.exception('keystore action %s failed', self.action)
                parent.user.environment.events.request.fire(
                    request_type='KEYS',
                    name=f'{parent.user._scenario.identifier} {key}',
                    response_time=0,
                    response_length=1,
                    context=parent.user._context,
                    exception=e,
                )

                failure_handler(e, parent.user._scenario)

        return task
