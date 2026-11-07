"""The TUI side of the daemon connection: attach, receive, command."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable

from .daemon import (
    MAX_CLIENT_BUFFER_BYTES,
    PROTOCOL_VERSION,
    RuntimeMetadata,
    SessionError,
    encode_message,
)


class SessionClient:
    def __init__(self, metadata: RuntimeMetadata):
        self.metadata = metadata
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self._reader_task: asyncio.Task | None = None
        self._closing = False
        self._ready = asyncio.Event()
        self._connect_error: Exception | None = None

    async def connect(self, on_message: Callable[[dict], None]) -> None:
        try:
            self.reader, self.writer = await asyncio.open_unix_connection(
                self.metadata.socket_path, limit=MAX_CLIENT_BUFFER_BYTES
            )
            await self._write(
                {
                    "type": "attach",
                    "protocol_version": PROTOCOL_VERSION,
                    "run_id": self.metadata.run_id,
                    "cwd": self.metadata.cwd,
                }
            )
            self._reader_task = asyncio.create_task(self._read_messages(on_message))
        except (ConnectionError, OSError, SessionError) as error:
            self._connect_error = error
            raise
        finally:
            self._ready.set()

    async def send_command(self, action: str, key: str | None = None) -> None:
        await self._ready.wait()
        if self._connect_error is not None:
            raise SessionError(str(self._connect_error)) from self._connect_error
        message = {"type": "command", "action": action}
        if key is not None:
            message["key"] = key
        await self._write(message)

    async def _write(self, message: dict) -> None:
        if self.writer is None or self.writer.is_closing():
            raise SessionError("daemon connection is closed")
        self.writer.write(encode_message(message))
        await self.writer.drain()

    async def _read_messages(self, on_message: Callable[[dict], None]) -> None:
        disconnected_unexpectedly = True
        try:
            while self.reader is not None:
                line = await self.reader.readline()
                if not line:
                    break
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    message = {
                        "type": "error",
                        "message": "daemon sent invalid JSON",
                        "fatal": True,
                    }
                if message.get("type") == "replaced":
                    disconnected_unexpectedly = False
                on_message(message)
        except (ConnectionError, OSError) as error:
            if not self._closing:
                on_message({"type": "error", "message": str(error), "fatal": True})
        finally:
            if disconnected_unexpectedly and not self._closing:
                on_message(
                    {
                        "type": "disconnected",
                        "message": "connection to background daemon closed",
                    }
                )

    def disconnect(self) -> None:
        self._closing = True
        self._ready.set()
        if self.writer is not None:
            self.writer.close()
