import unittest
from pathlib import Path

from common import mg, make_repo, cleanup


class TomlParserTests(unittest.TestCase):
    def test_project_keys(self):
        data = mg.parse_toml('[project]\nname = "x"\nrequires-python = ">=3.9"\n')
        self.assertEqual(data["project"]["requires-python"], ">=3.9")

    def test_multiline_array_and_comments(self):
        text = '''
[project]
classifiers = [
  # a comment
  "Programming Language :: Python :: 3.9",
  "Programming Language :: Python :: 3.10",  # trailing comment
]
'''
        data = mg.parse_toml(text)
        self.assertEqual(len(data["project"]["classifiers"]), 2)
        self.assertEqual(data["project"]["classifiers"][1],
                         "Programming Language :: Python :: 3.10")

    def test_escaped_string(self):
        data = mg.parse_toml('[project]\ndescription = "a \\"quoted\\" name"\n')
        self.assertEqual(data["project"]["description"], 'a "quoted" name')

    def test_literal_string(self):
        data = mg.parse_toml("[project]\ndescription = 'C:\\\\path'\n")
        self.assertEqual(data["project"]["description"], "C:\\\\path")

    def test_inline_table(self):
        data = mg.parse_toml('[project]\nauthors = [{name = "A", email = "a@b"}]\n')
        self.assertEqual(data["project"]["authors"][0]["name"], "A")

    def test_dotted_table(self):
        data = mg.parse_toml('[tool.foo.bar]\nx = 1\n')
        self.assertEqual(data["tool"]["foo"]["bar"]["x"], 1)

    def test_bare_date_is_string(self):
        data = mg.parse_toml('[project]\nreleased = 2024-01-01\n')
        self.assertEqual(data["project"]["released"], "2024-01-01")

    def test_multiline_basic_string(self):
        data = mg.parse_toml('[project]\ndescription = """line one\nline two"""\n')
        self.assertIn("line one", data["project"]["description"])
        self.assertIn("line two", data["project"]["description"])

    def test_array_of_tables_supported(self):
        data = mg.parse_toml('[[tool.mypy.overrides]]\nmodule = "a"\n'
                             '[[tool.mypy.overrides]]\nmodule = "b"\n')
        self.assertEqual([x["module"] for x in data["tool"]["mypy"]["overrides"]], ["a", "b"])

    def test_dotted_key_supported(self):
        data = mg.parse_toml('project.requires-python = ">=3.9"\n')
        self.assertEqual(data["project"]["requires-python"], ">=3.9")

    def test_array_of_tables_does_not_hide_project(self):
        data = mg.parse_toml('[project]\nrequires-python = ">=3.9"\n'
                             '[[tool.poetry.source]]\nname = "x"\n')
        self.assertEqual(data["project"]["requires-python"], ">=3.9")

    def test_unterminated_array_rejected(self):
        with self.assertRaises(mg.TomlError):
            mg.parse_toml('[project]\nclassifiers = [\n "a"\n')

    def test_array_keeps_brackets_in_strings(self):
        data = mg.parse_toml('[project]\nclassifiers = ["a [b] c", "d"]\n')
        self.assertEqual(data["project"]["classifiers"], ["a [b] c", "d"])

    def test_comment_inside_string_not_stripped(self):
        data = mg.parse_toml('[project]\ndescription = "a # b"\n')
        self.assertEqual(data["project"]["description"], "a # b")

    def test_load_toml_fallback_matches(self):
        root = make_repo({"pyproject.toml": '[project]\nrequires-python = ">=3.9"\n'})
        cleanup(self, root)
        data = mg.load_toml(root / "pyproject.toml", prefer_stdlib=False)
        self.assertEqual(data["project"]["requires-python"], ">=3.9")

    def test_load_toml_with_stdlib_when_available(self):
        root = make_repo({"pyproject.toml": '[project]\nrequires-python = ">=3.9"\n'})
        cleanup(self, root)
        data = mg.load_toml(root / "pyproject.toml", prefer_stdlib=True)
        self.assertEqual(data["project"]["requires-python"], ">=3.9")


if __name__ == "__main__":
    unittest.main()
