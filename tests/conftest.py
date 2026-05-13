import pytest
import sys
import os
from unittest.mock import Mock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))


@pytest.fixture
def mock_ib():
    with patch("ib_async.IB") as mock_ib_cls:
        ib_instance = Mock()
        ib_instance.isConnected.return_value = True
        mock_ib_cls.return_value = ib_instance
        yield ib_instance


@pytest.fixture
def mock_get_ib_connection():
    with patch("build.get_ib_connection") as mock_get_ib:
        ib_instance = Mock()
        ib_instance.isConnected.return_value = True
        mock_get_ib.return_value = ib_instance
        yield mock_get_ib
