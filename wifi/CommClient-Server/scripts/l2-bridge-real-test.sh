#!/usr/bin/env bash
# Real L2 bridge end-to-end: create a Linux bridge, attach 2 TAP
# interfaces, send a frame between them via raw sockets, prove the
# bridge actually forwards.
set -e

echo "[1] cleanup any prior test state"
ip link delete helen-br0 2>/dev/null || true
ip link delete helen-tap0 2>/dev/null || true
ip link delete helen-tap1 2>/dev/null || true

echo "[2] create bridge + 2 TAPs"
ip link add name helen-br0 type bridge
ip link set helen-br0 up

ip tuntap add dev helen-tap0 mode tap
ip link set helen-tap0 master helen-br0
ip link set helen-tap0 up

ip tuntap add dev helen-tap1 mode tap
ip link set helen-tap1 master helen-br0
ip link set helen-tap1 up

echo "[3] verify bridge sees both ports:"
bridge link show | grep -E "helen-tap0|helen-tap1"

echo "[4] state after creation:"
ip -br link show helen-br0
ip -br link show helen-tap0
ip -br link show helen-tap1

echo "[5] data flow test via Python raw sockets"
python3 - <<'PYEOF'
import os, socket, fcntl, struct, threading, time

TUNSETIFF = 0x400454ca
IFF_TAP   = 0x0002
IFF_NO_PI = 0x1000

def open_tap(name):
    fd = os.open("/dev/net/tun", os.O_RDWR)
    ifr = struct.pack("16sH", name.encode(), IFF_TAP | IFF_NO_PI)
    fcntl.ioctl(fd, TUNSETIFF, ifr)
    return fd

# Both TAPs are already attached to the bridge. Open the file
# descriptors and send a unique Ethernet frame from tap0 → tap1.
fd0 = open_tap("helen-tap0")
fd1 = open_tap("helen-tap1")

received = []
stop = threading.Event()
PAYLOAD_MARKER = b"helen-l2-bridge-test"

def reader():
    # Read frames in a loop; bridge will deliver our test frame
    # alongside other Linux-generated multicast (MLDv2 IPv6 etc.).
    # Stop after we find our payload or after 4 seconds.
    import select
    deadline = time.time() + 4.0
    while time.time() < deadline and not stop.is_set():
        ready, _, _ = select.select([fd1], [], [], 0.5)
        if fd1 in ready:
            try:
                data = os.read(fd1, 4096)
                received.append(data)
                if PAYLOAD_MARKER in data:
                    return
            except Exception:
                return

t = threading.Thread(target=reader, daemon=True)
t.start()
time.sleep(0.3)  # let reader bind

# Build a simple Ethernet frame
dst_mac = b"\xff\xff\xff\xff\xff\xff"
src_mac = b"\x02\x00\x00\x00\x00\x42"
ethertype = b"\x88\xb5"  # local experimental
payload = PAYLOAD_MARKER
frame = dst_mac + src_mac + ethertype + payload

# Send it twice — bridge MAC learning may discard the first
os.write(fd0, frame)
time.sleep(0.1)
os.write(fd0, frame)
t.join(timeout=4.5)
stop.set()

# Look for our specific marker among everything received
matched = [f for f in received if PAYLOAD_MARKER in f]
print(f"[+] Total frames received via bridge: {len(received)}")
print(f"    Of those, {len(matched)} matched our test marker.")
if matched:
    f = matched[0]
    print(f"    payload: {f[14:].decode(errors='replace')}")
    print(f"    src MAC: {f[6:12].hex()}")
assert matched, (
    f"bridge did NOT deliver our test frame "
    f"(got {len(received)} other frames)"
)
print("[+] L2 bridge confirmed forwarding our specific test frame.")

os.close(fd0)
os.close(fd1)
PYEOF

echo "[6] cleanup"
ip link delete helen-tap0
ip link delete helen-tap1
ip link delete helen-br0

echo "[+] Real L2 bridge data-flow test complete."
