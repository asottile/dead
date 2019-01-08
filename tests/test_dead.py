import ast

from dead import Visitor


def test_getattr_str() -> None:
    """Tests a call to getattr with a string literal is marked"""
    method_name = "foo"
    sample_code = f"""
class A:
    def {method_name}():
        pass
a = A()
getattr(a,{method_name})
        """
    # TODO: leave this kind of setup to a fixture
    visitor = Visitor()
    visitor.visit(ast.parse(sample_code, filename="test"))
    assert method_name in visitor.reads
