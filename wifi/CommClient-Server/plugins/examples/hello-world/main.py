"""
Hello World — reference plugin for Helen.

Reacts to messages containing the word "hello" by replying in the same
channel. Demonstrates:

* hook registration (``on_message_created``)
* SDK usage (``helen_sdk.send_message``)
* settings access (``helen_sdk.kv_get`` for the greeting override)
"""


def on_message_created(payload):
    import helen_sdk    # provided by the loader at runtime

    content = (payload.get("content") or "").lower()
    if "hello" not in content:
        return {"skipped": True}

    greeting = helen_sdk.kv_get("greeting", "Hello back!")
    helen_sdk.send_message(
        channel_id=payload["channel_id"],
        content=greeting,
        metadata={"plugin": "hello-world"},
    )
    counter = (helen_sdk.kv_get("replies", 0) or 0) + 1
    helen_sdk.kv_set("replies", counter)
    return {"replied": True, "count": counter}
