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
DISABLE_COMMENT_RE = re.compile(r'\bdead\s*:\s*disable')


class Visitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.filename = ''
        self.is_test = False
        self.reads: UsageMap = collections.defaultdict(set)
        self.defines: UsageMap = collections.defaultdict(set)
        self.reads_tests: UsageMap = collections.defaultdict(set)
        self.disabled: Set[FileLine] = set()

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

    def _file_line(self, filename: str, line: int) -> FileLine:
        return FileLine(f'{filename}:{line}')

    def definition_str(self, node: ast.AST) -> FileLine:
        return self._file_line(self.filename, node.lineno)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for name in node.names:
            self.reads_target[name.name].add(self.definition_str(node))
            if not self.is_test and name.asname:
                self.defines[name.asname].add(self.definition_str(node))

        self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        if not self.is_test:
            self.defines[node.name].add(self.definition_str(node))
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        if not self.is_test:
            self.defines[node.name].add(self.definition_str(node))
        self.generic_visit(node)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Assign(self, node: ast.Assign) -> None:
        if not self.is_test:
            for target in node.targets:
                if isinstance(target, ast.Name):
                    self.defines[target.id].add(self.definition_str(node))
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
            self.disabled.add(self._file_line(self.filename, lineno))

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

            with open(filename, 'rb') as f:
                for tp, s, (lineno, _), _, _ in tokenize.tokenize(f.readline):
                    if tp == tokenize.COMMENT:
                        visitor.visit_comment(lineno, s)

    retv = 0

    unused_ignores = visitor.disabled.copy()
    for k, v in visitor.defines.items():
        if k not in visitor.reads:
            unused_ignores.difference_update(v)
            v = v - visitor.disabled

        if k.startswith('__') and k.endswith('__'):
            pass  # skip magic methods, probably an interface
        elif k not in visitor.reads and not v - visitor.disabled:
            pass  # all references disabled
        elif k not in visitor.reads and k not in visitor.reads_tests:
            print(f'{k} is never read, defined in {", ".join(sorted(v))}')
            retv = 1
        elif k not in visitor.reads:
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
    exit(main())
