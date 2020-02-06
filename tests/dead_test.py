import subprocess

import pytest

import dead


@pytest.mark.parametrize(
    's', ('# dead: disable', '#dead:disable', '# noqa: dead: disable'),
)
def test_dead_disable_regex_matching(s):
    assert dead.DISABLE_COMMENT_RE.search(s)


@pytest.mark.parametrize('s', ('# undead: disable', '# noqa'))
def test_dead_disable_regex_not_matching(s):
    assert not dead.DISABLE_COMMENT_RE.search(s)


@pytest.fixture
def git_dir(tmpdir):
    with tmpdir.as_cwd():
        subprocess.check_call(('git', 'init', '-q'))
        tmpdir.join('.gitignore').ensure()
        yield tmpdir


@pytest.mark.parametrize(
    's',
    (
        # assign
        'x = 1\nprint(x)\n',
        # function
        'def f(): ...\n'
        'print(f())\n',
        # async def
        'async def f(): ...\n'
        'print(f())\n',
        # class
        'class C: ...\n'
        'print(C())\n',
        # from import
        'from os.path import exists as wat\n'
        'print(wat)\n',
        # accessed from an attribute
        'import sys\n'
        'def unique_function(): ...\n'
        'sys.modules[__name__].unique_function()\n',
        # accessed from a variable type comment
        'MyStr = str\n'
        'x = "hi"  # type: MyStr\n'
        'print(x)\n',
        # accessed from a function type comment
        'MyStr = str  # type alias\n'
        'def f(): # type: () -> MyStr\n'
        '    ...\n'
        'f()\n',
        # magic methods are ok
        'class C:\n'
        '   def __str__(self): return "hi"\n'
        'print(C())\n',
        # disabled by comment
        'def unused(): ... # dead: disable\n',
        # exported in __all__
        'def f(): ...\n'
        '__all__ = ("f",)',
        'def g(): ...\n'
        'def f(): ...\n'
        '__all__ = ["f", g.__name__]',
    ),
)
def test_is_marked_as_used(git_dir, capsys, s):
    git_dir.join('f.py').write(s)
    subprocess.check_call(('git', 'add', '.'))
    assert not dead.main(())
    assert not any(capsys.readouterr())


def test_deleted_file_dont_raise_error(git_dir):
    module = git_dir.join('test-module.py')
    module.write('print(1)')
    subprocess.check_call(('git', 'add', '.'))
    module.remove()
    assert not dead.main(())


def test_setup_py_entrypoints_mark_as_used(git_dir, capsys):
    git_dir.join('setup.py').write(
        'from setuptools import setup\n'
        'setup(name="x", entry_points={"console_scripts": ["X=x:main"]})\n',
    )
    git_dir.join('x.py').write('def main(): ...')
    subprocess.check_call(('git', 'add', '.'))
    assert not dead.main(())
    assert not any(capsys.readouterr())


def test_setup_cfg_entry_points_marked_as_used(git_dir):
    git_dir.join('setup.cfg').write(
        '[options.entry_points]\n'
        'console_scripts=\n'
        '    X=x:main\n',
    )
    git_dir.join('x.py').write('def main(): ...')
    subprocess.check_call(('git', 'add', '.'))
    assert not dead.main(())


def test_setup_cfg_without_entry_points(git_dir):
    git_dir.join('setup.cfg').write(
        '[wheel]\n'
        'universal = 1\n',
    )
    subprocess.check_call(('git', 'add', '.'))
    assert not dead.main(())


def test_setup_cfg_without_attribute_entry_point(git_dir):
    git_dir.join('setup.cfg').write(
        '[options.entry_points]\n'
        '    pip-custom-platform = pip_custom_platform.pymonkey\n',
    )
    subprocess.check_call(('git', 'add', '.'))
    assert not dead.main(())


def test_never_referenced(git_dir, capsys):
    git_dir.join('f.py').write('x = 1')
    subprocess.check_call(('git', 'add', '.'))
    assert dead.main(())
    out, _ = capsys.readouterr()
    assert out == 'x is never read, defined in f.py:1\n'


def test_assignment_not_counted_as_reference(git_dir, capsys):
    git_dir.join('f.py').write('x = 1')
    git_dir.join('g.py').write('import f\nf.x = 2')
    subprocess.check_call(('git', 'add', '.'))
    assert dead.main(())
    out, _ = capsys.readouterr()
    assert out == 'x is never read, defined in f.py:1\n'


def test_only_referenced_in_tests(git_dir, capsys):
    git_dir.join('f.py').write('x = y = 1\n')
    git_dir.join('tests').ensure_dir().join('f_test.py').write(
        'from f import x, y\n'
        'def test(): assert x == 1\n'
        'class Test:\n'
        '   suite = "unit"\n'
        '   def test(self): assert y == 1\n',
    )
    subprocess.check_call(('git', 'add', '.'))
    assert dead.main(())
    out, _ = capsys.readouterr()
    assert out == (
        'x is only referenced in tests, defined in f.py:1\n'
        'y is only referenced in tests, defined in f.py:1\n'
    )


def test_unused_dead_disable_comment(git_dir, capsys):
    git_dir.join('f.py').write('x = 1  # dead: disable\nprint(x)\n')
    subprocess.check_call(('git', 'add', '.'))
    assert dead.main(())
    out, _ = capsys.readouterr()
    assert out == 'f.py:1: unused `# dead: disable`\n'


def test_partially_disabled(git_dir, capsys):
    git_dir.join('f.py').write(
        'x = 1\n'
        'x = 1  # dead: disable\n'
        'x = 1\n',
    )
    subprocess.check_call(('git', 'add', '.'))
    assert dead.main(())
    out, _ = capsys.readouterr()
    assert out == 'x is never read, defined in f.py:1, f.py:3\n'


def test_unused_argument(git_dir, capsys):
    git_dir.join('f.py').write('def f(a, *b, c, **d): return 1\nf')
    subprocess.check_call(('git', 'add', '.'))
    assert dead.main(())
    out, _ = capsys.readouterr()
    assert out == (
        'a is never read, defined in f.py:1\n'
        'b is never read, defined in f.py:1\n'
        'c is never read, defined in f.py:1\n'
        'd is never read, defined in f.py:1\n'
    )


def test_unused_argument_in_scope(git_dir, capsys):
    git_dir.join('f.py').write('def f(g): return 1\ndef g(): pass\ng\nf\n')
    subprocess.check_call(('git', 'add', '.'))
    assert dead.main(())
    out, _ = capsys.readouterr()
    assert out == 'g is never read, defined in f.py:1\n'


def test_using_an_argument(git_dir):
    git_dir.join('f.py').write('def f(g): return g\nf')
    subprocess.check_call(('git', 'add', '.'))
    assert not dead.main(())


def test_ignore_unused_arguments_stubs(git_dir):
    git_dir.join('f.py').write(
        'import abc\n'
        'from typing import overload\n'
        'class C:\n'
        '    @abc.abstractmethod\n'
        '    def func(self, arg1):\n'
        '        pass\n'
        'def func2(arg2):\n'
        '    pass\n'
        'def func3(arg3):\n'
        '    pass\n'
        '@overload\n'
        'def func4(arg4):\n'
        '    ...\n'
        'def func5(arg5):\n'
        '    """docstring but trivial"""\n'
        'def func6(arg6):\n'
        '    raise NotImplementedError\n'
        'def func7(arg7):\n'
        '    raise NotImplementedError()\n'
        'def func8(arg8):\n'
        '    """docstring plus raise"""\n'
        '    raise NotImplementedError()\n'
        'C.func, func2, func3, func4, func5, func6, func7, func8\n',
    )
    subprocess.check_call(('git', 'add', '.'))
    assert not dead.main(())


def test_ignored_arguments(git_dir):
    git_dir.join('f.py').write(
        'class C:\n'
        '    @classmethod\n'
        '    def f(cls): return 1\n'  # allow conventional `cls` method
        'C.f',
    )
    subprocess.check_call(('git', 'add', '.'))
    assert not dead.main(())
