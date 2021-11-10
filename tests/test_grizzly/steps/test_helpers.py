import os
import json
import shutil

from typing import Callable, List, Any, cast

import pytest

from behave.runner import Context
from behave.model import Table, Row
from _pytest.tmpdir import TempdirFactory
from locust.env import Environment
from locust.exception import CatchResponseError
from locust.clients import ResponseContextManager
from requests.models import Response

from grizzly.context import GrizzlyContext
from grizzly.types import RequestMethod, ResponseTarget, ResponseContentType, ResponseAction
from grizzly.task import RequestTask, SleepTask
from grizzly.exceptions import ResponseHandlerError
from grizzly.steps.helpers import (
    add_validation_handler,
    add_save_handler,
    add_request_task,
    add_request_task_response_status_codes,
    normalize_step_name,
    generate_save_handler,
    generate_validation_handler,
    _add_response_handler,
    get_matches,
)

from ..helpers import TestUser
# pylint: disable=unused-import
from ..fixtures import (
    grizzly_context,
    locust_environment,
    behave_context,
    request_task,
)


def test_add_request_task_response_status_codes() -> None:
    request = RequestTask(RequestMethod.SEND, name='test', endpoint='/api/test')

    assert request.response.status_codes == [200]

    add_request_task_response_status_codes(request, '-200')
    assert request.response.status_codes == []

    add_request_task_response_status_codes(request, '200,302, 400')
    assert request.response.status_codes == [200, 302, 400]


@pytest.mark.usefixtures('behave_context', 'grizzly_context')
def test_add_request_task(behave_context: Context, grizzly_context: Callable, tmpdir_factory: TempdirFactory) -> None:
    grizzly = cast(GrizzlyContext, behave_context.grizzly)
    grizzly.scenario.context['host'] = 'http://test'

    assert len(grizzly.scenario.tasks) == 0

    with pytest.raises(ValueError):
        add_request_task(behave_context, method=RequestMethod.POST, source='{}')

    assert len(grizzly.scenario.tasks) == 0

    with pytest.raises(ValueError):
        add_request_task(behave_context, method=RequestMethod.POST, source='{}', endpoint='http://test/api/v1/test')

    with pytest.raises(ValueError):
        add_request_task(behave_context, method=RequestMethod.from_string('TEST'), source='{}', endpoint='/api/v1/test')

    add_request_task(behave_context, method=RequestMethod.POST, source='{}', endpoint='/api/v1/test')

    assert len(grizzly.scenario.tasks) == 1
    assert isinstance(grizzly.scenario.tasks[0], RequestTask)
    assert grizzly.scenario.tasks[0].name == '<unknown>'

    with pytest.raises(ValueError):
        add_request_task(behave_context, method=RequestMethod.from_string('TEST'), source='{}', name='test')

    add_request_task(behave_context, method=RequestMethod.from_string('POST'), source='{}', name='test')

    assert len(grizzly.scenario.tasks) == 2
    assert isinstance(grizzly.scenario.tasks[1], RequestTask)
    assert grizzly.scenario.tasks[0].endpoint == grizzly.scenario.tasks[1].endpoint
    assert grizzly.scenario.tasks[1].name == 'test'

    with pytest.raises(ValueError):
        add_request_task(behave_context, method=RequestMethod.from_string('TEST'), source='{}', name='test', endpoint='/api/v2/test')

    add_request_task(behave_context, method=RequestMethod.POST, source='{}', name='test', endpoint='/api/v2/test')

    assert len(grizzly.scenario.tasks) == 3
    assert isinstance(grizzly.scenario.tasks[2], RequestTask)
    assert grizzly.scenario.tasks[1].endpoint != grizzly.scenario.tasks[2].endpoint
    assert grizzly.scenario.tasks[2].name == 'test'

    _, _, _, (template_path, template_name, _) = grizzly_context()
    template_full_path = os.path.join(template_path, template_name)
    add_request_task(behave_context, method=RequestMethod.SEND, source=template_full_path, name='my_blob', endpoint='my_container')

    with open(template_full_path, 'r') as fd:
        template_source = json.dumps(json.load(fd))

    assert len(grizzly.scenario.tasks) == 4
    assert isinstance(grizzly.scenario.tasks[-1], RequestTask)
    assert grizzly.scenario.tasks[-1].source == template_source
    assert grizzly.scenario.tasks[-1].endpoint == 'my_container'
    assert grizzly.scenario.tasks[-1].name == 'my_blob'

    with pytest.raises(ValueError):
        add_request_task(behave_context, method=RequestMethod.POST, source='{}', name='test')

    add_request_task(behave_context, method=RequestMethod.SEND, source=template_full_path, name='my_blob2')
    assert len(grizzly.scenario.tasks) == 5
    assert isinstance(grizzly.scenario.tasks[-1], RequestTask)
    assert isinstance(grizzly.scenario.tasks[-2], RequestTask)
    assert grizzly.scenario.tasks[-1].source == template_source
    assert grizzly.scenario.tasks[-1].endpoint == grizzly.scenario.tasks[-2].endpoint
    assert grizzly.scenario.tasks[-1].name == 'my_blob2'

    try:
        test_context = tmpdir_factory.mktemp('test_context').mkdir('requests')
        test_context_root = os.path.dirname(str(test_context))
        os.environ['GRIZZLY_CONTEXT_ROOT'] = test_context_root
        behave_context.config.base_dir = test_context_root
        test_template = test_context.join('template.j2.json')
        test_template.write('{{ hello_world }}')

        rows: List[Row] = []
        rows.append(Row(['test'], ['-200,400']))
        rows.append(Row(['test'], ['302']))
        behave_context.table = Table(['test'], rows=rows)

        grizzly.scenario.tasks = [SleepTask(sleep=1.0)]

        with pytest.raises(ValueError) as e:
            add_request_task(behave_context, method=RequestMethod.PUT, source='template.j2.json')
        assert 'previous task was not a request' in str(e)

        add_request_task(behave_context, method=RequestMethod.PUT, source='template.j2.json', name='test', endpoint='/api/test')

        add_request_task(behave_context, method=RequestMethod.PUT, source='template.j2.json', endpoint='/api/test')
        assert cast(RequestTask, grizzly.scenario.tasks[-1]).name == 'template'
    finally:
        del os.environ['GRIZZLY_CONTEXT_ROOT']
        shutil.rmtree(test_context_root)


@pytest.mark.usefixtures('locust_environment')
def test_generate_save_handler(locust_environment: Environment) -> None:
    user = TestUser(locust_environment)
    response = Response()
    response._content = '{}'.encode('utf-8')
    response.status_code = 200
    response_context_manager = ResponseContextManager(response, None, None)

    assert 'test' not in user.context_variables

    handler = generate_save_handler('$.', '.*', 'test')
    with pytest.raises(TypeError) as te:
        handler((ResponseContentType.GUESS, {'test': {'value': 'test'}}), user, response_context_manager)
    assert 'could not find a transformer for GUESS' in str(te)

    with pytest.raises(TypeError) as te:
        handler((ResponseContentType.JSON, {'test': {'value': 'test'}}), user, response_context_manager)
    assert 'is not a valid expression' in str(te)

    handler = generate_save_handler('$.test.value', '.*', 'test')

    handler((ResponseContentType.JSON, {'test': {'value': 'test'}}), user, response_context_manager)
    assert response_context_manager._manual_result is None
    assert user.context_variables.get('test', None) == 'test'
    del user.context_variables['test']

    handler((ResponseContentType.JSON, {'test': {'value': 'nottest'}}), user, response_context_manager)
    assert response_context_manager._manual_result is None
    assert user.context_variables.get('test', None) == 'nottest'
    del user.context_variables['test']

    user.set_context_variable('value', 'test')
    handler = generate_save_handler('$.test.value', '.*({{ value }})$', 'test')

    handler((ResponseContentType.JSON, {'test': {'value': 'test'}}), user, response_context_manager)
    assert response_context_manager._manual_result is None
    assert user.context_variables.get('test', None) == 'test'
    del user.context_variables['test']

    handler((ResponseContentType.JSON, {'test': {'value': 'nottest'}}), user, response_context_manager)
    assert response_context_manager._manual_result is None
    assert user.context_variables.get('test', None) == 'test'
    del user.context_variables['test']

    # failed
    handler((ResponseContentType.JSON, {'test': {'name': 'test'}}), user, response_context_manager)
    assert isinstance(response_context_manager._manual_result, CatchResponseError)
    assert user.context_variables.get('test', 'test') is None

    with pytest.raises(ResponseHandlerError):
        handler((ResponseContentType.JSON, {'test': {'name': 'test'}}), user, None)

    # multiple matches
    handler = generate_save_handler('$.test[*].value', '.*t.*', 'test')
    handler((ResponseContentType.JSON, {'test': [{'value': 'test'}, {'value': 'test'}]}), user, response_context_manager)
    assert isinstance(response_context_manager._manual_result, CatchResponseError)
    assert user._context['variables']['test'] is None

    with pytest.raises(ResponseHandlerError):
        handler((ResponseContentType.JSON, {'test': [{'value': 'test'}, {'value': 'test'}]}), user, None)

    # save object dict
    handler = generate_save_handler(
        '$.test.prop2',
        '.*',
        'test_object',
    )

    handler(
        (
            ResponseContentType.JSON,
            {
                'test': {
                    'prop1': 'value1',
                    'prop2': {
                        'prop21': False,
                        'prop22': 100,
                        'prop23': {
                            'prop231': True,
                            'prop232': 'hello',
                            'prop233': 'world!',
                            'prop234': 200,
                        },
                    },
                    'prop3': 'value3',
                    'prop4': [
                        'prop41',
                        True,
                        'prop42',
                        300,
                    ],
                }
            }
        ),
        user,
        response_context_manager,
    )

    test_object = user.context_variables.get('test_object', None)
    assert json.loads(test_object) == {
        'prop21': False,
        'prop22': 100,
        'prop23': {
            'prop231': True,
            'prop232': 'hello',
            'prop233': 'world!',
            'prop234': 200,
        },
    }

    # save object list
    handler = generate_save_handler(
        '$.test.prop4',
        '.*',
        'test_list',
    )

    handler(
        (
            ResponseContentType.JSON,
            {
                'test': {
                    'prop1': 'value1',
                    'prop2': {
                        'prop21': False,
                        'prop22': 100,
                        'prop23': {
                            'prop231': True,
                            'prop232': 'hello',
                            'prop233': 'world!',
                            'prop234': 200,
                        },
                    },
                    'prop3': 'value3',
                    'prop4': [
                        'prop41',
                        True,
                        'prop42',
                        300,
                    ],
                }
            }
        ),
        user,
        response_context_manager,
    )

    test_list = user.context_variables.get('test_list', None)
    assert json.loads(test_list) == [
        'prop41',
        True,
        'prop42',
        300,
    ]


@pytest.mark.usefixtures('locust_environment')
def test_generate_validation_handler_negative(locust_environment: Environment) -> None:
    user = TestUser(locust_environment)
    response = Response()
    response._content = '{}'.encode('utf-8')
    response.status_code = 200
    response_context_manager = ResponseContextManager(response, None, None)

    handler = generate_validation_handler('$.test.value', 'test', False)

    # match fixed string expression
    handler((ResponseContentType.JSON, {'test': {'value': 'test'}}), user, response_context_manager)
    assert response_context_manager._manual_result is None

    # no match fixed string expression
    handler((ResponseContentType.JSON, {'test': {'value': 'nottest'}}), user, response_context_manager)
    assert not response_context_manager._manual_result == None
    response_context_manager._manual_result = None

    # regexp match expression value
    user.set_context_variable('expression', '$.test.value')
    user.set_context_variable('value', 'test')
    handler = generate_validation_handler('{{ expression }}', '.*({{ value }})$', False)
    handler((ResponseContentType.JSON, {'test': {'value': 'nottest'}}), user, response_context_manager)
    assert response_context_manager._manual_result is None

    # ony allows 1 match per expression
    handler = generate_validation_handler('$.test[*].value', '.*(test)$', False)
    handler(
        (ResponseContentType.JSON, {'test': [{'value': 'nottest'}, {'value': 'reallynottest'}, {'value': 'test'}]}),
        user,
        response_context_manager,
    )
    assert not response_context_manager._manual_result == None
    response_context_manager._manual_result = None

    # 1 match expression
    handler(
        (ResponseContentType.JSON, {'test': [{'value': 'not'}, {'value': 'reallynot'}, {'value': 'test'}]}),
        user,
        response_context_manager,
    )
    assert response_context_manager._manual_result is None

    handler = generate_validation_handler('$.[*]', 'ID_31337', False)

    # 1 match expression
    handler((ResponseContentType.JSON, ['ID_1337', 'ID_31337', 'ID_73313']), user, response_context_manager)
    assert response_context_manager._manual_result is None

    example = {
        'glossary': {
            'title': 'example glossary',
            'GlossDiv': {
                'title': 'S',
                'GlossList': {
                    'GlossEntry': {
                        'ID': 'SGML',
                        'SortAs': 'SGML',
                        'GlossTerm': 'Standard Generalized Markup Language',
                        'Acronym': 'SGML',
                        'Abbrev': 'ISO 8879:1986',
                        'GlossDef': {
                            'para': 'A meta-markup language, used to create markup languages such as DocBook.',
                            'GlossSeeAlso': ['GML', 'XML']
                        },
                        'GlossSee': 'markup',
                        'Additional': [
                            {
                                'addtitle': 'test1',
                                'addvalue': 'hello world',
                            },
                            {
                                'addtitle': 'test2',
                                'addvalue': 'good stuff',
                            },
                        ]
                    }
                }
            }
        }
    }

    # 1 match in multiple values (list)
    handler = generate_validation_handler('$.*..GlossSeeAlso[*]', 'XML', False)
    handler((ResponseContentType.JSON, example), user, response_context_manager)
    assert response_context_manager._manual_result is None

    # no match in multiple values (list)
    handler = generate_validation_handler('$.*..GlossSeeAlso[*]', 'YAML', False)
    handler((ResponseContentType.JSON, example), user, response_context_manager)
    assert not response_context_manager._manual_result == None
    response_context_manager._manual_result = None

    handler = generate_validation_handler('$.glossary.title', '.*ary$', False)
    handler((ResponseContentType.JSON, example), user, response_context_manager)
    assert response_context_manager._manual_result is None

    handler = generate_validation_handler('$..Additional[?addtitle="test2"].addvalue', '.*stuff$', False)
    handler((ResponseContentType.JSON, example), user, response_context_manager)
    assert response_context_manager._manual_result is None

    handler = generate_validation_handler('$.`this`', 'False', False)
    handler((ResponseContentType.JSON, True), user, response_context_manager)
    assert isinstance(response_context_manager._manual_result, CatchResponseError)
    response_context_manager._manual_result = None

    with pytest.raises(ResponseHandlerError):
        handler((ResponseContentType.JSON, True), user, None)

    handler((ResponseContentType.JSON, False), user, response_context_manager)
    assert response_context_manager._manual_result is None


@pytest.mark.usefixtures('locust_environment')
def test_generate_validation_handler_positive(locust_environment: Environment) -> None:
    user = TestUser(locust_environment)
    try:
        response = Response()
        response._content = '{}'.encode('utf-8')
        response.status_code = 200
        response_context_manager = ResponseContextManager(response, None, None)

        handler = generate_validation_handler('$.test.value', 'test', True)

        # match fixed string expression
        handler((ResponseContentType.JSON, {'test': {'value': 'test'}}), user, response_context_manager)
        assert isinstance(response_context_manager._manual_result, CatchResponseError)
        response_context_manager._manual_result = None

        # no match fixed string expression
        handler((ResponseContentType.JSON, {'test': {'value': 'nottest'}}), user, response_context_manager)
        assert response_context_manager._manual_result is None

        # regexp match expression value
        handler = generate_validation_handler('$.test.value', '.*(test)$', True)
        handler((ResponseContentType.JSON, {'test': {'value': 'nottest'}}), user, response_context_manager)
        assert isinstance(response_context_manager._manual_result, CatchResponseError)
        response_context_manager._manual_result = None

        # ony allows 1 match per expression
        handler = generate_validation_handler('$.test[*].value', '.*(test)$', True)
        handler(
            (ResponseContentType.JSON, {'test': [{'value': 'nottest'}, {'value': 'reallynottest'}, {'value': 'test'}]}),
            user,
            response_context_manager,
        )
        assert response_context_manager._manual_result is None

        # 1 match expression
        handler(
            (ResponseContentType.JSON, {'test': [{'value': 'not'}, {'value': 'reallynot'}, {'value': 'test'}]}),
            user,
            response_context_manager,
        )
        assert isinstance(response_context_manager._manual_result, CatchResponseError)
        response_context_manager._manual_result = None

        handler = generate_validation_handler('$.[*]', 'STTO_31337', True)

        # 1 match expression
        handler((ResponseContentType.JSON, ['STTO_1337', 'STTO_31337', 'STTO_73313']), user, response_context_manager)
        assert isinstance(response_context_manager._manual_result, CatchResponseError)
        response_context_manager._manual_result = None

        example = {
            'glossary': {
                'title': 'example glossary',
                'GlossDiv': {
                    'title': 'S',
                    'GlossList': {
                        'GlossEntry': {
                            'ID': 'SGML',
                            'SortAs': 'SGML',
                            'GlossTerm': 'Standard Generalized Markup Language',
                            'Acronym': 'SGML',
                            'Abbrev': 'ISO 8879:1986',
                            'GlossDef': {
                                'para': 'A meta-markup language, used to create markup languages such as DocBook.',
                                'GlossSeeAlso': ['GML', 'XML']
                            },
                            'GlossSee': 'markup',
                            'Additional': [
                                {
                                    'addtitle': 'test1',
                                    'addvalue': 'hello world',
                                },
                                {
                                    'addtitle': 'test2',
                                    'addvalue': 'good stuff',
                                },
                            ]
                        }
                    }
                }
            }
        }

        # 1 match in multiple values (list)
        user.set_context_variable('format', 'XML')
        handler = generate_validation_handler('$.*..GlossSeeAlso[*]', '{{ format }}', True)
        handler((ResponseContentType.JSON, example), user, response_context_manager)
        assert isinstance(response_context_manager._manual_result, CatchResponseError)
        response_context_manager._manual_result = None

        with pytest.raises(ResponseHandlerError):
            handler((ResponseContentType.JSON, example), user, None)

        # no match in multiple values (list)
        user.set_context_variable('format', 'YAML')
        handler = generate_validation_handler('$.*..GlossSeeAlso[*]', '{{ format }}', True)
        handler((ResponseContentType.JSON, example), user, response_context_manager)
        assert response_context_manager._manual_result is None

        user.set_context_variable('property', 'title')
        user.set_context_variable('regexp', '.*ary$')
        handler = generate_validation_handler('$.glossary.{{ property }}', '{{ regexp }}', True)
        handler((ResponseContentType.JSON, example), user, response_context_manager)
        assert isinstance(response_context_manager._manual_result, CatchResponseError)
        response_context_manager._manual_result = None

        handler = generate_validation_handler('$..Additional[?addtitle="test1"].addvalue', '.*world$', True)
        handler((ResponseContentType.JSON, example), user, response_context_manager)
        assert isinstance(response_context_manager._manual_result, CatchResponseError)
        response_context_manager._manual_result = None

        handler = generate_validation_handler('$.`this`', 'False', True)
        handler((ResponseContentType.JSON, True), user, response_context_manager)
        assert response_context_manager._manual_result is None

        handler((ResponseContentType.JSON, False), user, response_context_manager)
        assert isinstance(response_context_manager._manual_result, CatchResponseError)
        response_context_manager._manual_result = None
    finally:
        assert user._context['variables'] != TestUser(locust_environment)._context['variables']


@pytest.mark.usefixtures('behave_context', 'locust_environment')
def test_add_save_handler(behave_context: Context, locust_environment: Environment) -> None:
    user = TestUser(locust_environment)
    response = Response()
    response._content = '{}'.encode('utf-8')
    response.status_code = 200
    response_context_manager = ResponseContextManager(response, None, None)
    grizzly = cast(GrizzlyContext, behave_context.grizzly)
    tasks = grizzly.scenario.tasks

    assert len(tasks) == 0
    assert len(user.context_variables) == 0

    # not preceeded by a request source
    with pytest.raises(ValueError):
        add_save_handler(grizzly, ResponseTarget.METADATA, '$.test.value', 'test', 'test-variable')

    assert len(user.context_variables) == 0

    # add request source
    add_request_task(behave_context, method=RequestMethod.GET, source='{}', name='test', endpoint='/api/v2/test')

    assert len(tasks) == 1

    task = cast(RequestTask, tasks[0])

    with pytest.raises(ValueError):
        add_save_handler(grizzly, ResponseTarget.METADATA, '', 'test', 'test-variable')

    with pytest.raises(ValueError):
        add_save_handler(grizzly, ResponseTarget.METADATA, '$.test.value', '.*', 'test-variable-metadata')

    try:
        grizzly.state.variables['test-variable-metadata'] = 'none'
        add_save_handler(grizzly, ResponseTarget.METADATA, '$.test.value', '.*', 'test-variable-metadata')
        assert len(task.response.handlers.metadata) == 1
        assert len(task.response.handlers.payload) == 0
    finally:
        del grizzly.state.variables['test-variable-metadata']

    with pytest.raises(ValueError):
        add_save_handler(grizzly, ResponseTarget.PAYLOAD, '$.test.value', '.*', 'test-variable-payload')

    try:
        grizzly.state.variables['test-variable-payload'] = 'none'

        add_save_handler(grizzly, ResponseTarget.PAYLOAD, '$.test.value', '.*', 'test-variable-payload')
        assert len(task.response.handlers.metadata) == 1
        assert len(task.response.handlers.payload) == 1
    finally:
        del grizzly.state.variables['test-variable-payload']

    metadata_handler = list(task.response.handlers.metadata)[0]
    payload_handler = list(task.response.handlers.payload)[0]

    metadata_handler((ResponseContentType.JSON, {'test': {'value': 'metadata'}}), user, response_context_manager)
    assert response_context_manager._manual_result is None
    assert user.context_variables.get('test-variable-metadata', None) == 'metadata'

    payload_handler((ResponseContentType.JSON, {'test': {'value': 'payload'}}), user, response_context_manager)
    assert response_context_manager._manual_result is None
    assert user.context_variables.get('test-variable-metadata', None) == 'metadata'
    assert user.context_variables.get('test-variable-payload', None) == 'payload'

    metadata_handler((ResponseContentType.JSON, {'test': {'name': 'metadata'}}), user, response_context_manager)
    assert isinstance(response_context_manager._manual_result, CatchResponseError)
    response_context_manager._manual_result = None
    assert user.context_variables.get('test-variable-metadata', 'metadata') is None

    payload_handler((ResponseContentType.JSON, {'test': {'name': 'payload'}}), user, response_context_manager)
    assert isinstance(response_context_manager._manual_result, CatchResponseError)
    response_context_manager._manual_result = None
    assert user.context_variables.get('test-variable-payload', 'payload') is None

    # previous non RequestTask task
    grizzly.scenario.tasks.append(SleepTask(sleep=1.0))

    grizzly.state.variables['test'] = 'none'
    with pytest.raises(ValueError):
        add_save_handler(grizzly, ResponseTarget.PAYLOAD, '$.test.value', '.*', 'test')

    # remove non RequestTask task
    grizzly.scenario.tasks.pop()

    # add_save_handler calling _add_response_handler incorrectly
    with pytest.raises(ValueError) as e:
        _add_response_handler(grizzly, ResponseTarget.PAYLOAD, ResponseAction.SAVE, '$test.value', '.*', variable=None)
    assert 'variable is not set' in str(e)



@pytest.mark.usefixtures('behave_context', 'locust_environment')
def test_add_validation_handler(behave_context: Context, locust_environment: Environment) -> None:
    user = TestUser(locust_environment)
    response = Response()
    response._content = '{}'.encode('utf-8')
    response.status_code = 200
    response_context_manager = ResponseContextManager(response, None, None)
    grizzly = cast(GrizzlyContext, behave_context.grizzly)
    tasks = grizzly.scenario.tasks
    assert len(tasks) == 0

    # not preceeded by a request source
    with pytest.raises(ValueError):
        add_validation_handler(grizzly, ResponseTarget.METADATA, '$.test.value', 'test', False)

    # add request source
    add_request_task(behave_context, method=RequestMethod.GET, source='{}', name='test', endpoint='/api/v2/test')

    assert len(tasks) == 1

    # empty expression, fail
    with pytest.raises(ValueError):
        add_validation_handler(grizzly, ResponseTarget.METADATA, '', 'test', False)

    # add metadata response handler
    add_validation_handler(grizzly, ResponseTarget.METADATA, '$.test.value', 'test', False)
    task = cast(RequestTask, tasks[0])
    assert len(task.response.handlers.metadata) == 1
    assert len(task.response.handlers.payload) == 0

    # add payload response handler
    add_validation_handler(grizzly, ResponseTarget.PAYLOAD, '$.test.value', 'test', False)
    assert len(task.response.handlers.metadata) == 1
    assert len(task.response.handlers.payload) == 1

    metadata_handler = list(task.response.handlers.metadata)[0]
    payload_handler = list(task.response.handlers.payload)[0]

    # test that they validates
    metadata_handler((ResponseContentType.JSON, {'test': {'value': 'test'}}), user, response_context_manager)
    assert response_context_manager._manual_result is None
    payload_handler((ResponseContentType.JSON, {'test': {'value': 'test'}}), user, response_context_manager)
    assert response_context_manager._manual_result is None

    # test that they validates, negative
    metadata_handler((ResponseContentType.JSON, {'test': {'value': 'no-test'}}), user, response_context_manager)
    assert isinstance(response_context_manager._manual_result, CatchResponseError)
    response_context_manager._manual_result = None

    payload_handler((ResponseContentType.JSON, {'test': {'value': 'no-test'}}), user, response_context_manager)
    assert isinstance(response_context_manager._manual_result, CatchResponseError)
    response_context_manager._manual_result = None

    # add a second payload response handler
    user.add_context({'variables': {'property': 'name', 'name': 'bob'}})
    add_validation_handler(grizzly, ResponseTarget.PAYLOAD, '$.test.{{ property }}', '{{ name }}', False)
    assert len(task.response.handlers.payload) == 2

    # test that they validates
    for handler in task.response.handlers.payload:
        handler((ResponseContentType.JSON, {'test': {'value': 'test', 'name': 'bob'}}), user, response_context_manager)
        assert response_context_manager._manual_result is None

    # add_validation_handler calling _add_response_handler incorrectly
    with pytest.raises(ValueError) as e:
        _add_response_handler(grizzly, ResponseTarget.PAYLOAD, ResponseAction.VALIDATE, '$.test', 'value', condition=None)
    assert 'condition is not set' in str(e)


def test_normalize_step_name() -> None:
    expected = 'this is just a "" of text with quoted ""'
    actual = normalize_step_name('this is just a "string" of text with quoted "words"')

    assert expected == actual


def test_get_matches() -> None:
    def match_get_values(input_payload: Any) -> List[str]:
        if str(input_payload) == 'world':
            return ['world']
        elif str(input_payload) == 'hello':
            return ['']
        else:
            return []


    def input_get_values(input_payload: Any) -> List[str]:
        return cast(List[str], input_payload)

    matches = get_matches(input_get_values, match_get_values, ['hello', 'world', 'foo', 'bar'])

    assert matches == (['hello', 'world', 'foo', 'bar'], ['world'],)