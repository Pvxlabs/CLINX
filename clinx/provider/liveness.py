"""Interpret exact Provider observations; owns no durable state."""

ACTIVE = {"inprogress", "running", "active"}
TERMINAL = {"completed", "succeeded", "success", "failed", "error", "systemerror",
            "errored", "timeout", "timedout", "disconnected", "connectionlost",
            "connectionclosed", "cancelled", "canceled", "interrupted", "aborted"}

def normalized(value):
    from clinx.provider.app_server import _provider_call
    return _provider_call('observation.normalize', value=value)


def classify(observations, thread_id, turn_id):
    from clinx.provider.app_server import _provider_call
    return _provider_call('observation.classify', observations=observations,
                          thread_id=thread_id, turn_id=turn_id)
