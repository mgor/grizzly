from __future__ import annotations
import logging
import re

from typing import Any, Dict, List, Union, Optional, NamedTuple, Callable, Match, Generator, Tuple, cast
from pathlib import Path
from enum import Enum
from dataclasses import dataclass, field
from tokenize import tokenize, TokenInfo, TokenError
from token import OP, STRING, NAME
from io import BytesIO
from ast import literal_eval

import frontmatter
import mistune

from novella.markdown.preprocessor import MarkdownPreprocessor, MarkdownFiles, MarkdownFile
from novella.novella import NovellaContext
from novella.templates.mkdocs import MkdocsTemplate, MkdocsUpdateConfigAction
from novella.markdown.flavor import MkDocsFlavor
from novella.action import CopyFilesAction, RunAction
from novella.markdown.tags.anchor import AnchorTagProcessor
from novella.markdown.preprocessor import MarkdownPreprocessorAction
from pydoc_markdown.novella.preprocessor import PydocTagPreprocessor
from pydoc_markdown.contrib.renderers.markdown import MarkdownRenderer as PydocMarkdownRenderer
from mistune.renderers.markdown import MarkdownRenderer
from mistune.inline_parser import InlineParser
from mistune.core import BlockState, InlineState


logger = logging.getLogger('grizzly.novella')


class GrizzlyMkdocsTemplate(MkdocsTemplate):  # pragma: no cover
    def define_pipeline(self, context: NovellaContext) -> None:
        context.option("serve", description="Use mkdocs serve", flag=True)
        context.option("port", description="The port to serve under", default="8000")
        context.option("site-dir", description='Build directory for MkDocs (defaults to "_site")', default="_site", metavar='PATH')
        context.option("base-url", description='The base URL to prefix to autogenerated link inside the documentation.', metavar='URL')

        copy_files = cast(CopyFilesAction, context.do('copy-files', name='copy-files'))
        copy_files.paths = [self.content_directory]

        for extension in ['yml', 'yaml']:
            if (context.project_directory / f'mkdocs.{extension}').exists():
                copy_files.paths.append(f'mkdocs.{extension}')
                break

        update_config = cast(MkdocsUpdateConfigAction, context.do('mkdocs-update-config', name='mkdocs-update-config'))
        update_config.content_directory = self.content_directory

        preprocessor = cast(MarkdownPreprocessorAction, context.do('preprocess-markdown', name='preprocess-markdown'))
        preprocessor.path = self.content_directory
        preprocessor.use('grizzly')  # <-- diff compared to MkdocsTemplate.define_pipeline

        def configure_anchor(anchor: AnchorTagProcessor) -> None:
            anchor.flavor = MkDocsFlavor(cast('str | None', context.options['base-url']) or self.base_url or '')

        context.delay(lambda: preprocessor.preprocessor('anchor', cast(Any, configure_anchor)))
        context.delay(lambda: preprocessor.preprocessor('grizzly'))  # <!-- diff compared to MkdocsTemplate.define_pipeline

        def configure_run(run: RunAction) -> None:
            run.args = ['mkdocs']
            if context.options['serve']:
                port = int(str(context.options['port']))
                run.supports_reloading = True
                run.args += ['serve', '--dev-addr', f'localhost:{port}']
            else:
                run.args += ['build', '-d', context.project_directory / str(context.options['site-dir'])]
        context.do('run', configure_run, name='mkdocs-run')


class MarkdownAstType(Enum):
    TEXT = 'text'
    PARAGRAPH = 'paragraph'
    HEADER = 'heading'
    BLANK_LINE = 'blank_line'
    INLINE_HTML = 'inline_html'
    BLOCK_HTML = 'block_html'
    BLOCK_TEXT = 'block_text'
    BLOCK_CODE = 'block_code'
    LINK = 'link'
    STRONG = 'strong'
    LIST = 'list'
    IMAGE = 'image'
    CODEPSPAN = 'codespan'
    BLOCK_QUOTE = 'block_quote'
    LIST_ITEM = 'list_item'
    SOFTBREAK = 'softbreak'
    EMPHASIS = 'emphasis'

    NONE = None

    @classmethod
    def from_value(cls, value: Optional[str]) -> MarkdownAstType:
        for enum_value in cls:
            if enum_value.value == value:
                return enum_value

        raise ValueError(f'"{value}" is not a valid value of {cls.__name__}')


NO_CHILD: Dict[str, Any] = {'type': None, 'raw': None}


@dataclass
class MarkdownAstNode:
    ast: Dict[str, Any]
    index: int

    _first_child: Optional[MarkdownAstNode] = field(init=False, default=None)
    keep: bool = field(init=False, default=True)

    @property
    def first_child(self) -> MarkdownAstNode:
        if self._first_child is None:
            for child_node in self.ast.get('children', [NO_CHILD]):
                child = MarkdownAstNode(child_node, self.index)
                if child.type != MarkdownAstType.BLANK_LINE:
                    self._first_child = child
                    break

            if self._first_child is None:
                self._first_child = MarkdownAstNode(NO_CHILD, self.index)

        return self._first_child

    def get_child(self, index: int) -> MarkdownAstNode:
        child_node = self.ast.get('children', [NO_CHILD])[index]

        return MarkdownAstNode(child_node, self.index)

    @property
    def type(self) -> MarkdownAstType:
        return MarkdownAstType.from_value(self.ast.get('type', None))

    @property
    def raw(self) -> Optional[str]:
        return cast(Optional[str], self.ast.get('raw', None))


class MarkdownHeading(NamedTuple):
    text: str
    level: int


def make_human_readable(input: str) -> str:
    words: List[str] = []

    for word in input.split('_'):
        words.append(word.capitalize())

    output = ' '.join(words)

    for word in ['http', 'sftp', 'api', 'csv']:
        output = output.replace(word.capitalize(), word.upper())
        output = output.replace(word, word.upper())

    to_replace = dict(Iot='IoT', hub='Hub')
    for value, replace_value in to_replace.items():
        output = output.replace(value, replace_value)

    return output


def _create_nav_node(target: List[Union[str, Dict[str, str]]], path: str, node: Path, with_index: bool = True) -> None:
    if not (node.is_file() and (node.stem == '__init__' or not node.stem.startswith('_'))):
        return

    if node.stem == '__init__':
        if not with_index:
            return

        target.insert(0, f'{path}/index.md')
    else:
        target.append({make_human_readable(node.stem): f'{path}/{node.stem}.md'})


def mkdocs_update_config(config: Dict[str, Any]) -> None:  # pragma: no cover
    root = Path.cwd().parent
    config_nav_tasks = config['nav'][3]['Framework'][0]['Usage'][0]['Tasks']
    config_nav_tasks_clients = config_nav_tasks.pop()
    tasks = root / 'grizzly' / 'tasks'

    nav_tasks: List[Union[str, Dict[str, str]]] = []
    for task in tasks.iterdir():
        _create_nav_node(nav_tasks, 'framework/usage/tasks', task)
    config_nav_tasks.extend(nav_tasks)

    tasks_clients = tasks / 'clients'
    nav_tasks_clients: List[Union[str, Dict[str, str]]] = []
    for task_client in tasks_clients.iterdir():
        _create_nav_node(nav_tasks_clients, 'framework/usage/tasks/clients', task_client)
    config_nav_tasks_clients['Clients'] = nav_tasks_clients
    config_nav_tasks.append(config_nav_tasks_clients)

    config_nav_testdata = config['nav'][3]['Framework'][0]['Usage'][1]['Variables'][1]
    nav_testdata: List[Union[str, Dict[str, str]]] = []
    variables = Path.cwd() / '..' / 'grizzly' / 'testdata' / 'variables'
    for variable in variables.iterdir():
        _create_nav_node(nav_testdata, 'framework/usage/variables/testdata', variable)
    config_nav_testdata['Testdata'] = nav_testdata

    config_nav_users = config['nav'][3]['Framework'][0]['Usage'][2]
    users = root / 'grizzly' / 'users'
    nav_users: List[Union[str, Dict[str, str]]] = []
    for user in users.iterdir():
        _create_nav_node(nav_users, 'framework/usage/load-users', user)

    config_nav_users['Load Users'] = nav_users

    config_nav_steps = config['nav'][3]['Framework'][0]['Usage'][3]['Steps']
    steps = root / 'grizzly' / 'steps'
    for step in steps.iterdir():
        _create_nav_node(config_nav_steps, 'framework/usage/steps', step)

    config_nav_steps_background = config_nav_steps[1]
    steps_background = steps / 'background'
    nav_steps_background: List[Union[str, Dict[str, str]]] = []
    for step in steps_background.iterdir():
        _create_nav_node(nav_steps_background, 'framework/usage/steps/background', step)

    config_nav_steps_background['Background'] = nav_steps_background

    config_nav_steps_scenario = config_nav_steps[2]
    steps_scenario = steps / 'scenario'
    nav_steps_scenario: List[Union[str, Dict[str, str]]] = []
    for step in steps_scenario.iterdir():
        _create_nav_node(nav_steps_scenario, 'framework/usage/steps/scenario', step)

    config_nav_steps_scenario['Scenario'] = nav_steps_scenario

    steps_scenario_tasks = steps_scenario / 'tasks'
    nav_steps_scenario_tasks: List[Union[str, Dict[str, str]]] = []
    for task in steps_scenario_tasks.iterdir():
        _create_nav_node(nav_steps_scenario_tasks, 'framework/usage/steps/scenario/tasks', task, with_index=False)

    config_nav_steps_scenario['Scenario'].append({'Tasks': nav_steps_scenario_tasks})


def preprocess_markdown_update_with_header_levels(processor: MarkdownPreprocessor, levels: Dict[str, int]) -> None:  # pragma: no cover
    if isinstance(processor, PydocTagPreprocessor) and isinstance(processor._renderer, PydocMarkdownRenderer):
        processor._renderer.header_level_by_type.update(levels)


def _generate_dynamic_page(input_file: Path, output_path: Path, title: str, namespace: str) -> None:
    if not (input_file.is_file() and (input_file.stem == '__init__' or not input_file.stem.startswith('_'))):
        return

    if input_file.stem == '__init__':
        filename = 'index'
    else:
        filename = input_file.stem
        title = f'{title} / {make_human_readable(input_file.stem)}'
        namespace = f'{namespace}.{input_file.stem}'

    file = output_path / f'{filename}.md'
    file.parent.mkdir(parents=True, exist_ok=True)
    if not file.exists():
        file.write_text(f'''---
title: {title}
---
@pydoc {namespace}
''')


def generate_dynamic_pages(directory: Path) -> None:  # pragma: no cover
    root = Path.cwd().parent

    tasks = root / 'grizzly' / 'tasks'
    output_path = directory / 'content' / 'framework' / 'usage' / 'tasks'
    for task in tasks.iterdir():
        _generate_dynamic_page(task, output_path, 'Tasks', 'grizzly.tasks')

    tasks_clients = tasks / 'clients'
    output_path = directory / 'content' / 'framework' / 'usage' / 'tasks' / 'clients'
    for task_client in tasks_clients.iterdir():
        _generate_dynamic_page(task_client, output_path, 'Clients', 'grizzly.tasks.clients')

    variables = root / 'grizzly' / 'testdata' / 'variables'
    output_path = directory / 'content' / 'framework' / 'usage' / 'variables' / 'testdata'
    for variable in variables.iterdir():
        _generate_dynamic_page(variable, output_path, 'Testdata', 'grizzly.testdata.variables')

    steps = root / 'grizzly' / 'steps'
    output_path = directory / 'content' / 'framework' / 'usage' / 'steps'
    for step in steps.iterdir():
        _generate_dynamic_page(step, output_path, 'Steps', 'grizzly.steps')

    steps_background = steps / 'background'
    output_path = directory / 'content' / 'framework' / 'usage' / 'steps' / 'background'
    for step in steps_background.iterdir():
        _generate_dynamic_page(step, output_path, 'Steps / Background', 'grizzly.steps.background')

    steps_scenario = steps / 'scenario'
    output_path = directory / 'content' / 'framework' / 'usage' / 'steps' / 'scenario'
    for step in steps_scenario.iterdir():
        _generate_dynamic_page(step, output_path, 'Steps / Scenario', 'grizzly.steps.scenario')

    steps_scenario_tasks = steps_scenario / 'tasks'
    output_path = directory / 'content' / 'framework' / 'usage' / 'steps' / 'scenario' / 'tasks'
    for task in steps_scenario_tasks.iterdir():
        _generate_dynamic_page(task, output_path, 'Steps / Scenario / Tasks', 'grizzly.steps.scenario.tasks')

    users = root / 'grizzly' / 'users'
    output_path = directory / 'content' / 'framework' / 'usage' / 'load-users'
    for user in users.iterdir():
        _generate_dynamic_page(user, output_path, 'Load Users', 'grizzly.users')


class GrizzlyMarkdownInlineParser(InlineParser):
    def parse_codespan(self, match: Match, state: InlineState) -> int:
        """
        Default `mistune.inline_parser.InlineParser.parse_codespan` escapes code, which messes
        things up, it will escape some character to their HTML entity representation, which will be literal
        when rendering as HTML (& -> &amp; etc.).
        """
        marker = match.group(0)
        # require same marker with same length at end

        pattern = re.compile(r'(.*?[^`])' + marker + r'(?!`)', re.S)

        pos = match.end()
        m = pattern.match(state.src, pos)
        if m:
            end_pos = m.end()
            code = m.group(1)
            # Line endings are treated like spaces
            code = code.replace('\n', ' ')
            if len(code.strip()):
                if code.startswith(' ') and code.endswith(' '):
                    code = code[1:-1]
            state.append_token({'type': 'codespan', 'raw': code})
            #                                               ^
            # only diff compared tomistune.inline_parser.InlineParser.parse_codespan
            return end_pos
        else:
            state.append_token({'type': 'text', 'raw': marker})
            return pos


class GrizzlyMarkdown:
    _markdown: mistune.Markdown
    _document: MarkdownFile
    _ast_tree_original: List[Dict[str, Any]]
    _ast_tree_modified: List[Dict[str, Any]]
    _index: int
    ignore_until: Optional[Callable[[MarkdownAstNode], bool]]

    def __init__(self, markdown: mistune.Markdown, document: MarkdownFile) -> None:
        self._markdown = markdown
        self._document = document
        self._index = 0
        self.ignore_until = None

    @classmethod
    def _is_anchor(cls, value: str) -> bool:
        tokens = [token for token in cls._get_tokens(value) if token.type in [OP, NAME, STRING]]

        try:
            return tokens[0].type == OP and tokens[0].string == '<' and tokens[1].type == NAME and tokens[1].string == 'a' and tokens[-1].type == OP and tokens[-1].string == '>'
        except IndexError:
            return False

    @classmethod
    def _get_header(cls, node: MarkdownAstNode) -> MarkdownHeading:
        text = ''.join([child.get('raw', '') for child in node.ast.get('children', [])])
        level = node.ast.get('attrs', {}).get('level', 0)

        return MarkdownHeading(text, level)

    @property
    def index(self) -> int:
        return self._index

    @index.setter
    def index(self, value: int) -> None:
        self._index = value

    def get_code_block(self, start_node: MarkdownAstNode) -> Optional[str]:
        code_block: Optional[str] = None

        for index in range(start_node.index + 1, len(self._ast_tree_original)):
            node = MarkdownAstNode(self._ast_tree_original[index], index)

            # do not look beyond next header
            if node.type == MarkdownAstType.HEADER:
                break

            if node.type == MarkdownAstType.BLOCK_CODE and node.raw is not None:
                code_block = node.raw
                self.index = index + 1
                break

        return code_block

    @classmethod
    def _get_tokens(cls, text: str) -> List[TokenInfo]:
        tokens: List[TokenInfo] = []

        try:
            for token in tokenize(BytesIO(text.encode('utf8')).readline):
                tokens.append(token)
        except TokenError as e:
            if 'EOF in multi-line statement' not in str(e):
                raise

        return tokens

    @classmethod
    def get_step_expression_from_code_block(cls, code_block: str) -> Optional[Tuple[str, str]]:
        tokens = cls._get_tokens(code_block)

        for index, token in enumerate(tokens):
            if token.type == OP and token.string == '@':
                step_type = tokens[index + 1].string
                future_index = index + 2
                future_token = tokens[future_index]

                if not (future_token.type == OP and future_token.string == '('):
                    continue

                future_index += 1

                while not (future_token.type == OP and future_token.string == ')') and future_index < len(tokens):
                    future_token = tokens[future_index]
                    if future_token.type == STRING:
                        return (step_type, cast(str, literal_eval(future_token.string)),)

                    future_index += 1

        return None

    def to_ast(self, content: str) -> List[Dict[str, Any]]:
        return cast(List[Dict[str, Any]], self._markdown(content))

    def next(self) -> Generator[MarkdownAstNode, None, None]:
        for index, node_ast in enumerate(self._ast_tree_original):
            node = MarkdownAstNode(node_ast, index)

            if index < self.index:
                continue

            if self.ignore_until is not None:
                if not self.ignore_until(node):
                    self.ignore_until = None
                continue

            self.index = index

            if node.type == MarkdownAstType.BLANK_LINE:
                self._ast_tree_modified.append(node.ast)
                continue

            yield node

            if node.keep:
                self._ast_tree_modified.insert(node.index, node.ast)

    def peek(self) -> MarkdownAstNode:
        for index in range(self.index + 1, len(self._ast_tree_original)):
            node = MarkdownAstNode(self._ast_tree_original[index], self.index + index)

            if node.type != MarkdownAstType.BLANK_LINE:
                return node

        return MarkdownAstNode(self._ast_tree_original[self.index + 1], self.index + 1)

    @classmethod
    def ast_reformat_block_code(cls, node: MarkdownAstNode) -> MarkdownAstNode:
        if node.type == MarkdownAstType.BLOCK_CODE and node.raw is not None and node.raw.startswith('```'):
            style = node.ast.get('style', 'indent')
            if style == 'indent':
                indent = '    '
            else:
                indent = ''
            code_lines = [f'{indent}{line}' for line in node.raw.splitlines()]
            marker = code_lines[-1].strip()[-3:]
            if code_lines[-1].strip() != marker:
                raw = '\n'.join(code_lines[1:])
                raw = raw[:-3]
            else:
                raw = '\n'.join(code_lines[1:-1])
            _, info = code_lines[0].split(marker, 1)
            node.ast = {
                'type': 'block_code',
                'raw': raw,
                'style': 'indent',
                'marker': f'{indent}{marker}',
                'attrs': {
                    'info': info.strip(),
                },
            }

        return node

    @classmethod
    def ast_reformat_admonitions(cls, node: MarkdownAstNode) -> MarkdownAstNode:
        if node.type == MarkdownAstType.PARAGRAPH and (node.first_child.raw or '').startswith('!!!'):
            indent_next = False
            for index, child_ast in enumerate(node.ast.get('children', [])):
                if index == 0:
                    continue

                child = MarkdownAstNode(child_ast, node.index)
                if child.type == MarkdownAstType.SOFTBREAK:
                    indent_next = True
                    continue

                if indent_next and child.type == MarkdownAstType.TEXT and child.raw is not None:
                    child.ast['raw'] = f'    {child.raw}'
                    node.ast['children'][index] = child.ast
                    indent_next = False

        return node

    @classmethod
    def ast_reformat_recursive(cls, node: MarkdownAstNode, func: Callable[[MarkdownAstNode], MarkdownAstNode]) -> MarkdownAstNode:
        node = func(node)
        for i, child_ast in enumerate(node.ast.get('children', [])):
            child = func(MarkdownAstNode(child_ast, node.index))

            for j, grand_child_ast in enumerate(child.ast.get('children', [])):
                grand_child = func(MarkdownAstNode(grand_child_ast, node.index))

                if grand_child.ast != grand_child_ast:
                    child.ast['children'][j] = grand_child.ast

            if child.ast != child_ast:
                node.ast['children'][i] = child.ast

        return node

    def _process_content(self, content: str) -> str:
        self._ast_tree_original = self.to_ast(content)
        self._ast_tree_modified: List[Dict[str, Any]] = []

        single_docstring_class = content.count('## Class ') <= 1

        move_forward = False

        for node in self.next():
            # <!-- work around for indented code blocks
            # mkdocs material `===` messes up ast parser, let's trick the renderer
            # so it works
            node = self.ast_reformat_recursive(node, self.ast_reformat_block_code)
            # // -->

            # <!-- workaround for admonitions (!!!)
            node = self.ast_reformat_recursive(node, self.ast_reformat_admonitions)
            # // -->

            if not move_forward:
                if node.type != MarkdownAstType.PARAGRAPH:
                    continue

                if node.first_child.type != MarkdownAstType.INLINE_HTML:
                    continue

                text = node.first_child.raw

                if text is None:
                    continue

                if not self._is_anchor(text):
                    continue

                next_node = self.peek()
                if next_node.type != MarkdownAstType.HEADER:
                    continue

                move_forward = True
                continue
            else:
                move_forward = False
                header = self._get_header(node)

                # no class documentation, and any methods under it
                if header.text.startswith('Class '):
                    if single_docstring_class:
                        def condition(_: MarkdownAstNode) -> bool:
                            return True

                        node.keep = False
                        self.ignore_until = condition
                        continue

                # rewrite headers for step implementation, replace function name with step expression
                if header.text.startswith('step'):
                    code_block = self.get_code_block(node)
                    if code_block is None:
                        continue

                    step = self.get_step_expression_from_code_block(code_block)

                    if step is not None:
                        step_type, step_expression = step
                        _, _, header_new = header.text.split('_', 2)
                        header_new = f'{step_type.capitalize()} {header_new.replace("_", " ")}'

                        node.ast.update({'children': [{'type': 'text', 'raw': header_new}]})
                        self._ast_tree_modified.append({
                            'type': MarkdownAstType.BLOCK_CODE.value,
                            'raw': f'{step_type.capitalize()} {step_expression}',
                            'style': 'fenced',
                            'marker': '```',
                            'attrs': {
                                'info': 'gherkin',
                            }
                        })

        # remove orphan stuff that might be left in the end
        last_node: Optional[MarkdownAstNode] = None

        for index, node_ast in enumerate(reversed(self._ast_tree_modified)):
            node = MarkdownAstNode(node_ast, -index)

            if (
                not node.type == MarkdownAstType.BLANK_LINE
                and not (
                    node.type == MarkdownAstType.PARAGRAPH
                    and node.first_child.type == MarkdownAstType.INLINE_HTML
                    and self._is_anchor(node.first_child.raw or '')
                )
            ):
                last_node = node
                break

        if last_node is not None and last_node.index < 0:
            self._ast_tree_modified = self._ast_tree_modified[:last_node.index]

        renderer = MarkdownRenderer()

        content = cast(str, renderer(self._ast_tree_modified, state=BlockState()))

        return content

    def __call__(self) -> None:
        content = self._document.content

        if not frontmatter.checks(content):
            content = self._process_content(content)
        else:
            frontmatter_document = frontmatter.loads(content)
            frontmatter_document.content = self._process_content(frontmatter_document.content)
            content = cast(str, frontmatter.dumps(frontmatter_document))

        self._document.content = content


class GrizzlyMarkdownProcessor(MarkdownPreprocessor):
    _markdown: mistune.Markdown

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)

        self._markdown = mistune.Markdown(inline=GrizzlyMarkdownInlineParser())

    def process_files(self, files: MarkdownFiles) -> None:
        for file in files:
            GrizzlyMarkdown(self._markdown, file)()
