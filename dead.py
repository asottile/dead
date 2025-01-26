from __future__ import annotations

import argparse
import ast
import collections
import configparser
import contextlib
import os.path
import re
import subprocess
import tokenize
from collections.abc import Generator
from collections.abc import Sequence
from re import Pattern
from typing import DefaultDict
from typing import NewType
from typing import Protocol
from typing import Union

from identify.identify import tags_from_path

FileLine = NewType('FileLine', str)
UsageMap = DefaultDict[str, set[FileLine]]
FunctionDef = Union[ast.AsyncFunctionDef, ast.FunctionDef]
DISABLE_COMMENT_RE = re.compile(r'\bdead\s*:\s*disable')
STUB_EXCEPTIONS = frozenset(('AssertionError', 'NotImplementedError'))


class _HasLineno(Protocol):
    @property
    def lineno(self) -> int: ...


class Scope:
    def __init__(self) -> None:
        self.reads: UsageMap = collections.defaultdict(set)
        self.defines: UsageMap = collections.defaultdict(set)
        self.reads_tests: UsageMap = collections.defaultdict(set)


class Visitor(ast.NodeVisitor):
    def __init__(self, *, track_args: bool) -> None:
        self._track_args = track_args

        self.filename = ''
        self.is_test = False
        self.previous_scopes: list[Scope] = []
        self.scopes = [Scope()]
        self.disabled: set[FileLine] = set()

    @contextlib.contextmanager
    def file_ctx(
            self,
            filename: str,
            *,
            is_test: bool,
    ) -> Generator[None]:
        orig_filename, self.filename = self.filename, filename
        orig_is_test, self.is_test = self.is_test, is_test
        try:
            yield
        finally:
            self.filename = orig_filename
            self.is_test = orig_is_test

    @contextlib.contextmanager
    def scope(self) -> Generator[None]:
        self.scopes.append(Scope())
        try:
            yield
        finally:
            self.previous_scopes.append(self.scopes.pop())

    def _file_line(self, filename: str, line: int) -> FileLine:
        return FileLine(f'{filename}:{line}')

    def definition_str(self, node: _HasLineno) -> FileLine:
        return self._file_line(self.filename, node.lineno)

    def define(self, name: str, node: _HasLineno) -> None:
        if not self.is_test:
            self.scopes[-1].defines[name].add(self.definition_str(node))

    def read(self, name: str, node: _HasLineno) -> None:
        for scope in self.scopes:
            if self.is_test:
                scope.reads_tests[name].add(self.definition_str(node))
            else:
                scope.reads[name].add(self.definition_str(node))

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for name in node.names:
            self.read(name.name, node)
            if name.asname:
                self.define(name.asname, node)

        self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.define(node.name, node)
        self.generic_visit(node)
        # this is a bad middle ground to handle TypedDict and related classes
        # simply mark any annotated assignment directly in a class body as
        # "used"
        # *ideally* with full semantic analysis you'd be able to know when
        # a dictionary literal uses the same key as a specific TypedDict
        # and know if it is used.  unfortunately it's difficult to know
        # statically in isolation whether a class definition is a TypedDict
        # due to inheritance (and multiple inheritance (and module boundaries))
        for stmt in node.body:
            if (
                    isinstance(stmt, ast.AnnAssign) and
                    isinstance(stmt.target, ast.Name)
            ):
                self.read(stmt.target.id, stmt)

    def _is_stub_function(self, node: FunctionDef) -> bool:
        for stmt in node.body:
            if (
                    isinstance(stmt, ast.Expr) and
                    isinstance(stmt.value, ast.Constant) and
                    isinstance(stmt.value.value, (str, type(Ellipsis)))
            ):
                continue  # docstring or ...
            elif isinstance(stmt, ast.Pass):
                continue  # pass
            elif (
                    isinstance(stmt, ast.Raise) and
                    stmt.cause is None and (
                        (
                            isinstance(stmt.exc, ast.Name) and
                            stmt.exc.id in STUB_EXCEPTIONS
                        ) or (
                            isinstance(stmt.exc, ast.Call) and
                            isinstance(stmt.exc.func, ast.Name) and
                            stmt.exc.func.id in STUB_EXCEPTIONS
                        )
                    )
            ):
                continue  # raise NotImplementedError
            else:
                return False
        else:
            return True

    def visit_FunctionDef(self, node: FunctionDef) -> None:
        self.define(node.name, node)
        with self.scope():
            if self._track_args and not self._is_stub_function(node):
                for arg in (
                        *node.args.posonlyargs,
                        *node.args.args,
                        node.args.vararg,
                        *node.args.kwonlyargs,
                        node.args.kwarg,
                ):
                    if arg is not None:
                        self.define(arg.arg, arg)
            self.generic_visit(node)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Assign(self, node: ast.Assign) -> None:
        for target in node.targets:
            if isinstance(target, ast.Name):
                self.define(target.id, node)

        if (
                len(node.targets) == 1 and
                isinstance(node.targets[0], ast.Name) and
                node.targets[0].id == '__all__' and
                isinstance(node.value, (ast.Tuple, ast.List))
        ):
            for elt in node.value.elts:
                if (
                        isinstance(elt, ast.Constant) and
                        isinstance(elt.value, str)
                ):
                    self.read(elt.value, elt)

        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if isinstance(node.target, ast.Name):
            self.define(node.target.id, node)
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Load):
            self.read(node.id, node)

        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if isinstance(node.ctx, ast.Load):
            self.read(node.attr, node)

        self.generic_visit(node)

    def visit_comment(self, lineno: int, line: str) -> None:
        if DISABLE_COMMENT_RE.search(line):
            self.disabled.add(self._file_line(self.filename, lineno))


def _filenames(
        files_re: Pattern[str],
        exclude_re: Pattern[str],
        tests_re: Pattern[str],
) -> Generator[tuple[str, bool]]:
    # TODO: zsplit is more correct than splitlines
    out = subprocess.check_output(('git', 'ls-files')).decode()
    for filename in out.splitlines():
        if (
                not files_re.search(filename) or
                exclude_re.search(filename) or
                not os.path.exists(filename) or
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

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str):
            match = ENTRYPOINT_RE.match(node.value)
            if match:
                self.visitor.read(match.group(1), node)
        self.generic_visit(node)


def parse_entry_points_setup_py(visitor: Visitor) -> None:
    if not os.path.exists('setup.py'):
        return

    with visitor.file_ctx('setup.py', is_test=False):
        ParsesEntryPoints(visitor).visit(_ast('setup.py'))


def parse_entry_points_setup_cfg(visitor: Visitor) -> None:
    if not os.path.exists('setup.cfg'):
        return

    with visitor.file_ctx('setup.cfg', is_test=False):
        parser = configparser.ConfigParser()
        parser.read('setup.cfg')
        if 'options.entry_points' not in parser:
            return

        section = parser['options.entry_points']
        for k, v in section.items():
            for line in v.strip().splitlines():
                match = ENTRYPOINT_RE.match(line)
                if match:
                    node = ast.Constant(match[1])
                    node = ast.fix_missing_locations(node)
                    visitor.read(match[1], node)


def main(argv: Sequence[str] | None = None) -> int:
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
    parser.add_argument(
        '--track-args', action='store_true',
        help='opt into unused argument tracking',
    )
    parser.add_argument(
        '--symbol-allowlist',
        default=os.devnull,
        help='filename for symbol allowlist, one symbol per line',
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

    with open(args.symbol_allowlist) as allowlist_f:
        allowed_symbols = frozenset(allowlist_f.read().splitlines())

    visitor = Visitor(track_args=args.track_args)

    parse_entry_points_setup_py(visitor)
    parse_entry_points_setup_cfg(visitor)

    files_re = re.compile(args.files)
    exclude_re = re.compile(args.exclude)
    tests_re = re.compile(args.tests)
    for filename, is_test in _filenames(files_re, exclude_re, tests_re):
        tree = _ast(filename)

        with visitor.file_ctx(filename, is_test=is_test):
            visitor.visit(tree)

            with open(filename, 'rb') as f:
                for tp, s, (lineno, _), _, _ in tokenize.tokenize(f.readline):
                    if tp == tokenize.COMMENT:
                        visitor.visit_comment(lineno, s)

    retv = 0

    visitor.previous_scopes.append(visitor.scopes.pop())
    unused_ignores = visitor.disabled.copy()
    for scope in visitor.previous_scopes:
        for k, v in scope.defines.items():
            if k not in scope.reads:
                unused_ignores.difference_update(v)
                v = v - visitor.disabled

            if k in allowed_symbols:
                pass
            elif k.startswith('__') and k.endswith('__'):
                pass  # skip magic methods, probably an interface
            elif k in {'cls', 'self'}:
                pass  # ignore conventional cls / self
            elif k not in scope.reads and not v:
                pass  # all references disabled
            elif k not in scope.reads and k not in scope.reads_tests:
                print(f'{k} is never read, defined in {", ".join(sorted(v))}')
                retv = 1
            elif k not in scope.reads:
                print(
                    f'{k} is only referenced in tests, '
                    f'defined in {", ".join(sorted(v))}',
                )
                retv = 1

    if unused_ignores:
        for ignore in sorted(unused_ignores):
            print(f'{ignore}: unused `# dead: disable`')
            retv = 1

    return retv


if __name__ == '__main__':
    raise SystemExit(main())
