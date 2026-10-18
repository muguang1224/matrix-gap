import unittest

from common import mg


class PythonSpecTests(unittest.TestCase):
    def test_at_least(self):
        self.assertEqual(mg.python_spec_versions(">=3.9"),
                         ["3.9", "3.10", "3.11", "3.12", "3.13", "3.14", "3.15"])

    def test_bounded(self):
        self.assertEqual(mg.python_spec_versions(">=3.9,<3.12"), ["3.9", "3.10", "3.11"])

    def test_compatible_release_two_parts(self):
        self.assertEqual(mg.python_spec_versions("~=3.9"), mg.python_spec_versions(">=3.9"))

    def test_compatible_release_three_parts(self):
        self.assertEqual(mg.python_spec_versions("~=3.9.1"), ["3.9"])

    def test_wildcard_equal(self):
        self.assertEqual(mg.python_spec_versions("==3.11.*"), ["3.11"])

    def test_wildcard_not_equal(self):
        got = mg.python_spec_versions(">=3.9,!=3.10.*")
        self.assertNotIn("3.10", got)
        self.assertIn("3.9", got)
        self.assertIn("3.11", got)

    def test_bare_version(self):
        self.assertEqual(mg.python_spec_versions("3.10"), ["3.10"])

    def test_greater_than(self):
        self.assertEqual(mg.python_spec_versions(">3.12"), ["3.13", "3.14", "3.15"])

    def test_invalid_raises(self):
        with self.assertRaises(ValueError):
            mg.python_spec_versions(">=three")

    def test_four_segment_version(self):
        self.assertEqual(mg.python_spec_versions(">=3.9.0.1"), mg.python_spec_versions(">=3.9"))

    def test_prerelease_suffix(self):
        self.assertEqual(mg.python_spec_versions(">=3.11rc1"), mg.python_spec_versions(">=3.11"))

    def test_single_component_wildcard(self):
        self.assertEqual(mg.python_spec_versions("==3.*"), mg.python_spec_versions(">=3.7"))

    def test_single_component_negative_wildcard(self):
        self.assertEqual(mg.python_spec_versions("!=3.*"), [])

    def test_major_only_upper_bound(self):
        self.assertEqual(mg.python_spec_versions(">=3.9,<4"),
                         mg.python_spec_versions(">=3.9"))

    def test_major_only_lower_bound(self):
        self.assertEqual(mg.python_spec_versions(">=3"), mg.python_spec_versions(">=3.0"))

    def test_patch_bound_keeps_minor_line(self):
        self.assertIn("3.9", mg.python_spec_versions(">=3.9.5"))

    def test_patch_exclusion_keeps_minor_line(self):
        self.assertIn("3.9", mg.python_spec_versions("!=3.9.0"))

    def test_whitespace(self):
        self.assertEqual(mg.python_spec_versions(" >= 3.9 , < 3.11 "), ["3.9", "3.10"])


class NodeSpecTests(unittest.TestCase):
    def test_at_least(self):
        self.assertEqual(mg.node_spec_versions(">=18"),
                         ["18", "19", "20", "21", "22", "23", "24", "25", "26"])

    def test_caret(self):
        self.assertEqual(mg.node_spec_versions("^18"), ["18"])

    def test_tilde(self):
        self.assertEqual(mg.node_spec_versions("~18.2"), ["18"])

    def test_x_range(self):
        self.assertEqual(mg.node_spec_versions("18.x"), ["18"])

    def test_space_is_and(self):
        self.assertEqual(mg.node_spec_versions(">=18 <21"), ["18", "19", "20"])

    def test_or(self):
        self.assertEqual(mg.node_spec_versions("18 || 20"), ["18", "20"])

    def test_spaces_after_operator(self):
        self.assertEqual(mg.node_spec_versions(">= 18"), mg.node_spec_versions(">=18"))

    def test_hyphen_range(self):
        self.assertEqual(mg.node_spec_versions("18 - 20"), ["18", "19", "20"])

    def test_star(self):
        self.assertEqual(len(mg.node_spec_versions("*")), len(mg.NODE_CANDIDATES))

    def test_dynamic_lts_raises(self):
        with self.assertRaises(mg.DynamicSpec):
            mg.node_spec_versions("lts/*")

    def test_dynamic_latest_raises(self):
        with self.assertRaises(mg.DynamicSpec):
            mg.node_spec_versions("latest")


class NormalizeTests(unittest.TestCase):
    def test_python_patch(self):
        self.assertEqual(mg.normalize_python("3.10.13"), "3.10")

    def test_python_unknown(self):
        self.assertIsNone(mg.normalize_python("3.x"))

    def test_node_v_prefix(self):
        self.assertEqual(mg.normalize_node("v20.11.1"), "20")

    def test_node_dynamic(self):
        self.assertIsNone(mg.normalize_node("lts/*"))

    def test_go(self):
        self.assertEqual(mg.normalize_go("1.22.3"), "1.22")
        self.assertEqual(mg.normalize_go("go1.22"), "1.22")
        self.assertIsNone(mg.normalize_go("stable"))

    def test_rust(self):
        self.assertEqual(mg.normalize_rust("1.70.0"), "1.70")
        self.assertIsNone(mg.normalize_rust("stable"))


if __name__ == "__main__":
    unittest.main()
