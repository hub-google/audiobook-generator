"""The GitHub gateway is read-only with respect to the durable cloud queue."""
from unittest.mock import Mock, patch

from src.dispatch_github_gateway import GitHubRunGateway
from src.queue_dispatcher import Dispatcher


def test_dispatcher_inherits_stateless_http_gateway():
    assert issubclass(Dispatcher, GitHubRunGateway)
    assert "request" not in Dispatcher.__dict__
    assert "run_by_id" not in Dispatcher.__dict__
    assert "reconcile" in Dispatcher.__dict__


def test_github_gateway_keeps_404_distinct_from_api_failures():
    dispatcher = object.__new__(Dispatcher)
    dispatcher.api = "https://api.github.com/repos/example/repo"
    dispatcher.headers = {"Authorization": "Bearer TEST"}
    with patch("src.dispatch_github_gateway.requests.get",
               return_value=Mock(status_code=404)) as get:
        assert dispatcher.run_by_id(101) is None
        assert get.call_count == 1
    denied = Mock(status_code=403, text="Forbidden")
    with patch("src.dispatch_github_gateway.requests.get", return_value=denied):
        try:
            dispatcher.run_by_id(101)
            raise AssertionError("403 may not be disguised as a missing Run")
        except RuntimeError as error:
            assert "403" in str(error)
