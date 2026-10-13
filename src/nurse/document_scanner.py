"""Fail-closed ClamAV INSTREAM client for private prescription uploads."""

import socket
import struct

from rest_framework.exceptions import APIException


class ScannerUnavailable(APIException):
    status_code = 503
    default_detail = {"code": "validation_unavailable"}
    default_code = "validation_unavailable"


class InfectedDocument(APIException):
    status_code = 422
    default_detail = {"code": "document_rejected"}
    default_code = "document_rejected"


def scan_bytes(content, socket_path, timeout=3.0, chunk_size=65536):
    """Scan bytes using a local clamd Unix socket; never fall back to no scan."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(timeout)
            client.connect(socket_path)
            client.sendall(b"zINSTREAM\0")
            for offset in range(0, len(content), chunk_size):
                chunk = content[offset : offset + chunk_size]
                client.sendall(struct.pack("!I", len(chunk)) + chunk)
            client.sendall(struct.pack("!I", 0))
            response = bytearray()
            while not response.endswith(b"\0"):
                part = client.recv(1024)
                if not part or len(response) > 4096:
                    raise ScannerUnavailable()
                response.extend(part)
    except (OSError, TimeoutError, ValueError) as error:
        raise ScannerUnavailable() from error

    result = bytes(response).decode("ascii", errors="replace").strip("\0\n")
    if result.endswith(" FOUND"):
        raise InfectedDocument()
    if not result.endswith(" OK"):
        raise ScannerUnavailable()
    return True
