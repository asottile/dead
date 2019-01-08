import argparse
import ast
import collections
import contextlib
import os.path
import re
import subprocess
from typing import DefaultDict
from typing import Generator
from typing import Optional
from typing import Pattern
from typing import Sequence
from typing import Set
from typing import Tuple

from identify.identify import tags_from_path

UsageMap = DefaultDict[str, Set[str]]


class Visitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.filename = ''
        self.is_test = False
        self.reads: UsageMap = collections.defaultdict(set)
        self.defines: UsageMap = collections.defaultdict(set)
        self.reads_tests: UsageMap = collections.defaultdict(set)

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

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for name in node.names:
            self.reads_target[name.name].add(self.filename)
            if not self.is_test and name.asname:
                self.defines[name.asname].add(self.filename)

        self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        if not self.is_test:
            self.defines[node.name].add(self.filename)
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        if not self.is_test:
            self.defines[node.name].add(self.filename)
        self.generic_visit(node)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Assign(self, node: ast.Assign) -> None:
        if not self.is_test:
            for target in node.targets:
                if isinstance(target, ast.Name):
                    self.defines[target.id].add(self.filename)
        self.generic_visit(node)

    # TODO: AnnAssign

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Load):
            self.reads_target[node.id].add(self.filename)

        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if isinstance(node.ctx, ast.Load):
            self.reads_target[node.attr].add(self.filename)

        self.generic_visit(node)


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
            self.visitor.reads[match.group(1)].add('setup.py')
        self.generic_visit(node)


def parse_entry_points_setup_py(visitor: Visitor) -> None:
    if not os.path.exists('setup.py'):
        return

    ParsesEntryPoints(visitor).visit(_ast('setup.py'))


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
        tree = _ast(filename)

        with visitor.file_ctx(filename, is_test=is_test):
            visitor.visit(tree)

    for k, v in visitor.defines.items():
        if k.startswith('__') and k.endswith('__'):
            pass  # skip magic methods, probably an interface
        elif k not in visitor.reads and k not in visitor.reads_tests:
            print(f'{k} is never read, defined in {v}')
        elif k not in visitor.reads:
            print(f'{k} is only referenced in tests, defined in {v}')

    return 0


if __name__ == '__main__':
    exit(main())
