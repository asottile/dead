import argparse
import ast
import collections
import contextlib
import os.path
import re
import subprocess
import tokenize
from typing import DefaultDict
from typing import Generator
from typing import NewType
from typing import Optional
from typing import Pattern
from typing import Sequence
from typing import Set
from typing import Tuple

from identify.identify import tags_from_path

FileLine = NewType('FileLine', str)
UsageMap = DefaultDict[str, Set[FileLine]]
# https://github.com/python/typed_ast/blob/55420396/ast27/Parser/tokenizer.c#L102-L104
TYPE_COMMENT_RE = re.compile(r'^#\s*type:\s*')
# https://github.com/python/typed_ast/blob/55420396/ast27/Parser/tokenizer.c#L1400
TYPE_IGNORE_RE = re.compile(TYPE_COMMENT_RE.pattern + r'ignore\s*(#|$)')
# https://github.com/python/typed_ast/blob/55420396/ast27/Grammar/Grammar#L147
TYPE_FUNC_RE = re.compile(r'^(\(.*?\))\s*->\s*(.*)$')
DISABLE_COMMENT_RE = re.compile(r'dead\s*:\s*disable')


class Visitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.filename = ''
        self.is_test = False
        self.reads: UsageMap = collections.defaultdict(set)
        self.defines: Set[Tuple[str, str, int]] = set()
        self.reads_tests: UsageMap = collections.defaultdict(set)
        self.disabled: Set[Tuple[str, int]] = set()

    @contextlib.contextmanager
    def file_ctx(
            self,
            filename: str,
            *,
            is_test: bool,
    ) -> Generator[None, None, None]:
        orig_filename, self.filename = self.filename, filename
        orig_is_test, self.is_test = self.is_test, is_test
        try:
            yield
        finally:
            self.filename = orig_filename
            self.is_test = orig_is_test

    @property
    def reads_target(self) -> UsageMap:
        return self.reads_tests if self.is_test else self.reads

    def definition_str(self, node: ast.AST) -> FileLine:
        return FileLine(f'{self.filename}:{node.lineno}')

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for name in node.names:
            self.reads_target[name.name].add(self.definition_str(node))
            if not self.is_test and name.asname:
                self.defines.add((name.asname, self.filename, node.lineno))

        self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        if not self.is_test:
            self.defines.add((node.name, self.filename, node.lineno))
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        if not self.is_test:
            self.defines.add((node.name, self.filename, node.lineno))
        self.generic_visit(node)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Assign(self, node: ast.Assign) -> None:
        if not self.is_test:
            for target in node.targets:
                if isinstance(target, ast.Name):
                    self.defines.add((target.id, self.filename, node.lineno))
        self.generic_visit(node)

    # TODO: AnnAssign

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Load):
            self.reads_target[node.id].add(self.definition_str(node))

        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if isinstance(node.ctx, ast.Load):
            self.reads_target[node.attr].add(self.definition_str(node))

        self.generic_visit(node)

    def visit_comment(self, lineno: int, line: str) -> None:
        if DISABLE_COMMENT_RE.search(line):
            self.disabled.add((self.filename, lineno))

        if not TYPE_COMMENT_RE.match(line) or TYPE_IGNORE_RE.match(line):
            return

        line = line.split(':', 1)[1].strip()
        func_match = TYPE_FUNC_RE.match(line)
        if not func_match:
            parts: Tuple[str, ...] = (line,)
        else:
            parts = (
                func_match.group(1).replace('*', ''),
                func_match.group(2).strip(),
            )

        for part in parts:
            ast_obj = ast.parse(part, f'<{self.filename}:{lineno}: comment>')
            # adjust the line number to be that of the comment
            for descendant in ast.walk(ast_obj):
                if 'lineno' in descendant._attributes:
                    descendant.lineno = lineno

            self.visit(ast_obj)

    def visit_file(self, filename: str, is_test: bool) -> None:
        tree = _ast(filename)

        with self.file_ctx(filename, is_test=is_test):
            self.visit(tree)

            with open(filename, 'rb') as f:
                for tp, s, (lineno, _), _, _ in tokenize.tokenize(f.readline):
                    if tp == tokenize.COMMENT:
                        self.visit_comment(lineno, s)


def _filenames(
        files_re: Pattern[str],
        exclude_re: Pattern[str],
        tests_re: Pattern[str],
) -> Generator[Tuple[str, bool], None, None]:
    # TODO: zsplit is more correct than splitlines
    out = subprocess.check_output(('git', 'ls-files')).decode()
    for filename in out.splitlines():
        if (
                not files_re.search(filename) or
                exclude_re.search(filename) or
                'python' not in tags_from_path(filename)
        ):
            continue

        yield filename, bool(tests_re.search(filename))


def _ast(filename: str) -> ast.AST:
    with open(filename, 'rb') as f:
        return ast.parse(f.read(), filename=filename)


ENTRYPOINT_RE = re.compile('^[^=]+=[^:]+:([a-zA-Z0-9_]+)$')


class ParsesEntryPoints(ast.NodeVisitor):
    """Mark entry_points attributes as used"""

    def __init__(self, visitor: Visitor) -> None:
        self.visitor = visitor

    def visit_Str(self, node: ast.Str) -> None:
        match = ENTRYPOINT_RE.match(node.s)
        if match:
            location = self.visitor.definition_str(node)
            self.visitor.reads[match.group(1)].add(location)
        self.generic_visit(node)


def parse_entry_points_setup_py(visitor: Visitor) -> None:
    if not os.path.exists('setup.py'):
        return

    with visitor.file_ctx('setup.py', is_test=False):
        ParsesEntryPoints(visitor).visit(_ast('setup.py'))


def find_unused(visitor: Visitor) -> Generator[str, None, None]:
    for name, filename, lineno in visitor.defines:
        if (filename, lineno) in visitor.disabled:
            continue

        if name.startswith('__') and name.endswith('__'):
            continue  # skip magic methods, probably an interface

        location = f'{filename}:{lineno}'

        if name not in visitor.reads and name not in visitor.reads_tests:
            yield f'{name} is never read, defined in {location}'
        elif name not in visitor.reads:
            yield f'{name} is only referenced in tests, defined in {location}'


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        '--files', default='',
        help='regex for file inclusion, default: %(default)r',
    )
    parser.add_argument(
        '--exclude', default='^$',
        help='regex for file exclusion, default %(default)r',
    )
    parser.add_argument(
        '--tests', default='(^|/)(tests?|testing)/',
        help='regex to mark files as tests, default %(default)r',
    )
    args = parser.parse_args(argv)

    # TODO:
    #
    # class FooEnum(Enum):
    #   BAR = 1  # if not referenced directly, hunter assumes unused
    #
    # for f in FooEnum:  # actually a reference to BAR
    #   ...

    # TODO: v common for methods to only exist to satisfy interface

    visitor = Visitor()

    # TODO: maybe look in setup.cfg / pyproject.toml
    parse_entry_points_setup_py(visitor)

    files_re = re.compile(args.files)
    exclude_re = re.compile(args.exclude)
    tests_re = re.compile(args.tests)

    for filename, is_test in _filenames(files_re, exclude_re, tests_re):
        visitor.visit_file(filename, is_test)

    retv = 0
    for msg in find_unused(visitor):
        print(msg)
        retv = 1

    if visitor.disabled:
        print(f'disabled {len(visitor.disabled)} times')

    return retv


if __name__ == '__main__':
    exit(main())
