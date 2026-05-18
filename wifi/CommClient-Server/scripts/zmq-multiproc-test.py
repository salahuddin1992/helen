"""Real ZMQ multi-process pub/sub test."""
import json
import multiprocessing
import socket
import time
import zmq


def _free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]; s.close()
    return p


def pub_proc(url):
    ctx = zmq.Context()
    pub = ctx.socket(zmq.PUB)
    pub.bind(url)
    time.sleep(0.6)  # subscriber connect window
    for i in range(8):
        pub.send_multipart(
            [b"helen.ml.x",
             json.dumps({"i": i, "src": "pub"}).encode()],
        )
        time.sleep(0.05)
    pub.close()


def sub_proc(url, queue):
    ctx = zmq.Context()
    sub = ctx.socket(zmq.SUB)
    sub.connect(url)
    sub.setsockopt(zmq.SUBSCRIBE, b"helen.ml.")
    sub.setsockopt(zmq.RCVTIMEO, 4000)
    received = []
    try:
        for _ in range(5):
            parts = sub.recv_multipart()
            received.append(json.loads(parts[1]))
    except zmq.error.Again:
        pass
    sub.close()
    queue.put(received)


if __name__ == "__main__":
    port = _free_port()
    url = f"tcp://127.0.0.1:{port}"
    ctx = multiprocessing.get_context("fork")
    q = ctx.Queue()
    sub = ctx.Process(target=sub_proc, args=(url, q))
    sub.start()
    time.sleep(0.4)
    pub = ctx.Process(target=pub_proc, args=(url,))
    pub.start()
    pub.join(8); sub.join(8)
    received = q.get(timeout=2)
    assert len(received) >= 1, f"got 0 messages"
    print(f"OK ZMQ multi-process: subscriber got {len(received)} msgs")
    for r in received[:3]:
        print(f"  {r}")
