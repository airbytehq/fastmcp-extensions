"""Unit tests for the awesome_python_template module."""

import pytest

import awesome_python_template


class TestAwesomePythonTemplate:
    """Test cases for the main module."""

    @pytest.mark.unit
    def test_hello(self):
        """Test the hello function."""
        result = awesome_python_template.hello()
        assert result == "Hello from awesome-python-template!"
        assert isinstance(result, str)

    @pytest.mark.unit
    def test_get_version(self):
        """Test the get_version function."""
        version = awesome_python_template.get_version()
        assert version == "0.1.0"
        assert isinstance(version, str)

    @pytest.mark.unit
    def test_version_attribute(self):
        """Test the __version__ attribute."""
        assert hasattr(awesome_python_template, "__version__")
        assert awesome_python_template.__version__ == "0.1.0"

    @pytest.mark.unit
    def test_all_exports(self):
        """Test that __all__ contains expected exports."""
        expected_exports = ["hello", "get_version", "__version__"]
        assert hasattr(awesome_python_template, "__all__")
        assert all(item in awesome_python_template.__all__ for item in expected_exports)
