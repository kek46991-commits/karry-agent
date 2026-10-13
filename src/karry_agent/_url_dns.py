"""Isolated DNS worker; terminated by its parent when the deadline expires."""
import json
import socket
import sys

if __name__ == "__main__":
    try:
        answers = socket.getaddrinfo(sys.argv[1], int(sys.argv[2]), 0, socket.SOCK_STREAM)
        print(json.dumps(list(dict.fromkeys(answer[4][0] for answer in answers))))
    except (OSError, ValueError):
        sys.exit(1)
