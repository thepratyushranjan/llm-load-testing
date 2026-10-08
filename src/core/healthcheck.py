import socket
import sys

import redis

from src.core import keys
from src.core.config import get_settings


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: python -m src.core.healthcheck <service>", file=sys.stderr)
        return 2
    key = keys.heartbeat_key(sys.argv[1], socket.gethostname())
    try:
        client = redis.from_url(get_settings().redis_url, socket_timeout=2)
        return 0 if client.exists(key) else 1
    except Exception as e:
        print(f"healthcheck failed: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
