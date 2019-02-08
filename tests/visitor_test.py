import pytest

from dead import find_unused
from dead import Visitor


def _visit(tmpdir, files):
    visitor = Visitor()

    for filename, (src, is_test) in files.items():
        path = tmpdir.join(filename)
        path.write(src)
        visitor.visit_file(path, is_test=is_test)

    return list(find_unused(visitor))


@pytest.mark.parametrize(
    ('files', 'expected_error'),
    (
        # unused variable
        (
            {'a.py': ('x_variable = True\n', False)},
            'x_variable is never read, defined in',
        ),

        # unused function
        (
            {'a.py': ('\n\ndef x_function():\n    pass', False)},
            'x_function is never read, defined in',
        ),

        # unused class
        (
            {'a.py': ('\nclass XClass():\n    pass', False)},
            'XClass is never read, defined in',
        ),

        # used in tests
        (
            {
                'a.py': ('x_variable = True\n', False),
                'a_test.py': ('from a import x_variable', True),
            },
            'x_variable is only referenced in tests, defined in',
        ),
    ),
)
def test_unused_defines(tmpdir, files, expected_error):
    errors = _visit(tmpdir, files)
    assert errors
    assert errors[0].startswith(expected_error)


@pytest.mark.parametrize(
    'files',
    (
        # used in same module
        {'a.py': ('x_variable = True\nprint(x_variable)', False)},

        # used in other module
        {
            'a.py': ('x_variable = True\n', False),
            'b.py': ('import a\nprint(a.x_variable)', False),
        },
        {
            'a.py': ('x_variable = True\n', False),
            'b.py': ('from a import x_variable', False),
        },
    ),
)
def test_used_defines(tmpdir, files):
    has_error = bool(_visit(tmpdir, files))
    assert not has_error


@pytest.mark.parametrize(
    ('files', 'expect_error'),
    (
        # disable comment
        (
            {'a.py': ('\nclass XClass():  "# dead: disable"\npass', False)},
            True,
        ),
        (
            {'a.py': ('x_variable = True  # dead:disable', False)},
            False,
        ),
        (
            {'a.py': ('\nclass XClass():  # noqa dead: disable\n    pass', False)},
            False,
        ),

        # disabled comment for multiline definition
        (
            {'a.py': ('\ndef x_function(  # noqa dead: disable\n):\n    pass', False)},
            False,
        ),
    ),
)
def test_disable_comment(tmpdir, files, expect_error):
    has_error = bool(_visit(tmpdir, files))
    assert has_error is expect_error
