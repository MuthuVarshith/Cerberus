# Reproduction test for the sqlparse get_real_name bug, taken from the test added by
# upstream fix f66d12c245412f28c58f045b646eb53c0e691b8b (sqlparse, BSD-3-Clause).
# evaluation/external_repos.py derives this file from the fix commit at run time; this
# copy exists so the coding-agent demo in DEMO.md is a single command.
import sqlparse


def test_get_real_name_multi_part_dotted():
    # issue 332: for a fully-qualified name the real name is the component
    # after the *last* dot, not an intermediate one.
    ident = sqlparse.parse("db.schema.tbl.col")[0].tokens[0]
    assert 'col' == ident.get_real_name()
    assert 'col' == ident.get_name()
    # the parent object still anchors on the first dot
    assert 'db' == ident.get_parent_name()

    aliased = sqlparse.parse("x.y.z AS w")[0].tokens[0]
    assert 'z' == aliased.get_real_name()
    assert 'w' == aliased.get_alias()

    # two-part names and function calls stay correct
    assert 'b' == sqlparse.parse("a.b")[0].tokens[0].get_real_name()
    assert 'func' == sqlparse.parse("mydb.sch.func(1)")[0].tokens[0].get_real_name()
