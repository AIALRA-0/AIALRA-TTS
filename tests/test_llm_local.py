from ecse_localizer.llm_local import LocalLLMClient


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self):
        self.get_calls = 0
        self.post_calls = 0

    def get(self, _url, timeout):
        self.get_calls += 1
        return FakeResponse({"data": [{"id": "qwen2.5:14b-instruct"}]})

    def post(self, _url, json, timeout):
        self.post_calls += 1
        return FakeResponse({"choices": [{"message": {"content": '{"segments":[]}'}}]})


def test_local_llm_client_reuses_session_and_model_status_cache():
    client = LocalLLMClient(
        {
            "llm": {
                "endpoint": "http://127.0.0.1:11434/v1",
                "model_candidates": ["qwen2.5:14b-instruct"],
                "max_retries": 1,
                "timeout_seconds": 1,
            }
        }
    )
    fake = FakeSession()
    client.session = fake

    assert client.json_chat("system", "user", "{}") == {"segments": []}
    assert client.json_chat("system", "user", "{}") == {"segments": []}

    assert fake.get_calls == 1
    assert fake.post_calls == 2
