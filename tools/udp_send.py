#!/usr/bin/env python3
"""
Fire a control string at a running Setter and print the reply.

    python tools/udp_send.py 127.0.0.1 9990 "FADE 2 25"
    python tools/udp_send.py 10.1.1.5 9990 "PVW 3; CUT"
"""
import socket
import sys


def main():
    if len(sys.argv) < 4:
        print(__doc__)
        sys.exit(2)
    host, port, text = sys.argv[1], int(sys.argv[2]), " ".join(sys.argv[3:])
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(2.0)
    sock.sendto(text.encode("utf-8"), (host, port))
    try:
        data, _ = sock.recvfrom(4096)
        print(data.decode("utf-8", "replace").rstrip())
    except socket.timeout:
        print("(no reply within 2 s - is udp_reply on and the port right?)")


if __name__ == "__main__":
    main()
