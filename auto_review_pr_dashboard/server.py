"""The daemon's unix socket: attach handshake, snapshot push, commands, recycle.

One client at a time; every state change pushes a full snapshot, so a client
that missed messages resyncs without any diffing logic. Every
RECYCLE_EVERY_LOOPS loops the server hands the loop to a fresh daemon and
wipes `.tmp/` on the way.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import time
import traceback
from contextlib import suppress
from dataclasses import asdict
from io import TextIOWrapper
from pathlib import Path

from .config import RunConfig
from .daemon import (
    DAEMON_LOG_FILE_NAME,
    MAX_CLIENT_BUFFER_BYTES,
    PROTOCOL_VERSION,
    RuntimeMetadata,
    _remove_stale_runtime_files,
    _start_daemon_locked,
    _take_start_lock,
    encode_message,
    get_process_start_ticks,
    get_runtime_path,
    load_runtime_metadata,
)
from .models import LoopRecord
from .runner import TERMINATE_GRACE_SEC, LoopRunner, get_is_group_alive
from .store import Store

# Every Nth loop the daemon restarts itself and wipes .tmp/, which is what keeps
# loops/*.json and daemon.log bounded without a retention mechanism of its own.
RECYCLE_EVERY_LOOPS = 10


class SessionServer:
    def __init__(
        self,
        config: RunConfig,
        store: Store,
        cwd: str,
        socket_path: Path,
        run_id: str,
        handoff: dict | None = None,
    ):
        self.config = config
        self.store = store
        self.cwd = cwd
        self.socket_path = socket_path
        self.run_id = run_id
        self.started_at = time.time()
        self.runner = LoopRunner(
            config,
            store,
            on_event=self.emit_runner_event,
            recycle_every_loops=RECYCLE_EVERY_LOOPS,
        )
        self.status = "starting"
        self.phase_started_at = self.started_at
        self.recycle_count = 0
        # pgid -> start ticks of every agent running right now.
        self.agent_groups: dict[int, int | None] = {}
        if handoff is not None:
            # The loop counter, the countdown and the table carry over from the
            # daemon this one replaces, so the dashboard shows no gap.
            self.runner.loop_index = int(handoff["loop_index"])
            self.runner.paused = bool(handoff["paused"])
            self.runner.next_run_at = handoff["next_run_at"]
            self.recycle_count = int(handoff["recycle_count"])
            if handoff["record"] is not None:
                self.runner.record = LoopRecord.from_dict(handoff["record"])
                self.status = f"Loop #{self.runner.loop_index} · finished"
        self._event_loop: asyncio.AbstractEventLoop | None = None
        self._server: asyncio.Server | None = None
        self._client_writer: asyncio.StreamWriter | None = None
        # Every connection still being handled, current or not: _shutdown must
        # be able to cut all of them, and a replaced client keeps reading its
        # `replaced` message through its own handler until it ends.
        self._client_writers: set[asyncio.StreamWriter] = set()
        self._start_lock: TextIOWrapper | None = None

    async def run(self) -> None:
        self._event_loop = asyncio.get_running_loop()
        self._server = await asyncio.start_unix_server(
            self._handle_client, path=str(self.socket_path)
        )
        os.chmod(self.socket_path, 0o600)
        self._write_runtime_metadata()
        self._install_signal_handlers()
        is_crashed = False
        try:
            await self.runner.run_forever()
        except Exception:
            # A crash must clean up like a stop: no agents left behind, and the
            # reason in daemon.log, where the launcher's tail can show it.
            is_crashed = True
            self.runner.stop()
            self._log_crash()
            await self._await_agent_exits()
        finally:
            # A crash never recycles: a fresh daemon would only crash again.
            if self.runner.is_recycle_requested and not is_crashed:
                await self._recycle()
            else:
                await self._shutdown()
                # Holds no lock and the socket is gone, so this cwd's own pair goes too.
                _remove_stale_runtime_files(self.socket_path.parent)

    async def _await_agent_exits(self) -> None:
        """Give the SIGTERMs runner.stop() sent their grace, then SIGKILL."""
        deadline = time.monotonic() + TERMINATE_GRACE_SEC
        while time.monotonic() < deadline:
            if not any(get_is_group_alive(pgid) for pgid in self.agent_groups):
                return
            await asyncio.sleep(0.1)
        for pgid in self.agent_groups:
            with suppress(ProcessLookupError, PermissionError):
                os.killpg(pgid, signal.SIGKILL)

    def _log_crash(self) -> None:
        with suppress(OSError):
            log_path = self.store.base_dir / DAEMON_LOG_FILE_NAME
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as daemon_log:
                daemon_log.write(f"daemon crashed:\n{traceback.format_exc()}\n")

    def _install_signal_handlers(self) -> None:
        event_loop = asyncio.get_running_loop()
        for caught_signal in (signal.SIGINT, signal.SIGTERM):
            with suppress(NotImplementedError):
                event_loop.add_signal_handler(caught_signal, self.runner.stop)

    def _write_runtime_metadata(self) -> None:
        metadata = RuntimeMetadata(
            protocol_version=PROTOCOL_VERSION,
            run_id=self.run_id,
            pid=os.getpid(),
            cwd=self.cwd,
            socket_path=str(self.socket_path),
            started_at=self.started_at,
            config=asdict(self.config),
            agent_groups=tuple(self.agent_groups.items()),
        )
        runtime_path = get_runtime_path(self.store)
        temporary_path = runtime_path.with_suffix(".tmp")
        temporary_path.write_text(
            json.dumps(metadata.to_dict(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        temporary_path.replace(runtime_path)

    def emit_runner_event(self, name: str, *payload) -> None:
        if self._event_loop is None:
            return
        self._event_loop.call_soon_threadsafe(self._handle_runner_event, name, payload)

    def _handle_runner_event(self, name: str, payload: tuple) -> None:
        if name == "log":
            self._send_to_client({"type": "log", "key": payload[0], "line": payload[1]})
            return

        # Recorded in daemon.json only: the dashboard has no use for a pid, and
        # start_new_session makes each agent its own group leader, so pid is pgid.
        if name == "agent_started":
            self.agent_groups[payload[0]] = get_process_start_ticks(payload[0])
            self._write_runtime_metadata()
            return
        if name == "agent_finished":
            self.agent_groups.pop(payload[0], None)
            self._write_runtime_metadata()
            return

        if name == "loop_start":
            self.status = f"Loop #{payload[0]} · discovering"
            self.phase_started_at = time.time()
        elif name == "discovery_progress":
            done, total, _item = payload
            self.status = f"Loop #{self.runner.loop_index} · discovering {done}/{total}"
        elif name == "items_ready":
            self.status = f"Loop #{self.runner.loop_index} · running"
            self.phase_started_at = time.time()
        elif name == "loop_end":
            self.status = f"Loop #{self.runner.loop_index} · finished"
        elif name == "error":
            self._send_to_client(
                {"type": "error", "message": str(payload[0]), "fatal": False}
            )

        if name in ("idle_tick", "blocked_tick"):
            # A full snapshot per second would swamp a slow client, and an
            # unpaused countdown needs none: the client counts down by itself.
            # While paused the runner pushes the deadline out, so send that.
            if self.runner.paused:
                self._send_to_client(
                    {
                        "type": "tick",
                        "next_run_at": self.runner.next_run_at,
                        "blocked_until": self.runner.blocked_until,
                    }
                )
            return

        self._send_snapshot()

    def _get_snapshot(self) -> dict:
        return {
            "type": "snapshot",
            "config": asdict(self.config),
            "record": None
            if self.runner.record is None
            else self.runner.record.to_dict(),
            "status": self.status,
            "paused": self.runner.paused,
            "next_run_at": self.runner.next_run_at,
            "blocked_until": self.runner.blocked_until,
            "block_reason": self.runner.block_reason,
            "current_key": self.runner.current_key,
            "phase_started_at": self.phase_started_at,
            "recycle_count": self.recycle_count,
        }

    def _send_snapshot(self) -> None:
        self._send_to_client(self._get_snapshot())

    def _send_to_client(self, message: dict) -> None:
        writer = self._client_writer
        if writer is None or writer.is_closing():
            return
        if writer.transport.get_write_buffer_size() > MAX_CLIENT_BUFFER_BYTES:
            self._drop_client(writer)
            return
        try:
            writer.write(encode_message(message))
        except (ConnectionError, OSError):
            self._drop_client(writer)

    def _drop_client(self, writer: asyncio.StreamWriter) -> None:
        """Cut a client now: abort drops the buffers instead of draining them."""
        self._client_writers.discard(writer)
        if self._client_writer is writer:
            self._client_writer = None
        writer.transport.abort()

    async def _close_replaced_client(self, writer: asyncio.StreamWriter) -> None:
        if not writer.is_closing():
            replaced = {
                "type": "replaced",
                "message": "this dashboard was replaced by a newer --resume",
            }
            writer.write(encode_message(replaced))
            # Bounded: a replaced client that stopped reading must not stall
            # the attach that is replacing it.
            with suppress(asyncio.TimeoutError, ConnectionError, OSError):
                await asyncio.wait_for(writer.drain(), timeout=0.5)
        writer.close()

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self._client_writers.add(writer)
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=5)
            request = json.loads(line)
            error = self._get_attach_error(request)
            if error:
                writer.write(
                    encode_message({"type": "error", "message": error, "fatal": True})
                )
                await writer.drain()
                return

            replaced_writer = self._client_writer
            # Swap before any further await: concurrent attaches then chain,
            # each replacing exactly the client that was current when it landed.
            self._client_writer = writer
            if replaced_writer is not None and replaced_writer is not writer:
                await self._close_replaced_client(replaced_writer)
            writer.write(encode_message(self._get_snapshot()))
            await writer.drain()

            while line := await reader.readline():
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    self._send_notice("invalid command JSON", "error")
                    continue
                self._handle_command(message)
        except (asyncio.TimeoutError, ConnectionError, OSError, json.JSONDecodeError):
            pass
        finally:
            self._client_writers.discard(writer)
            if self._client_writer is writer:
                self._client_writer = None
            writer.close()
            # wait_closed, but bounded: a client that stopped reading must
            # never hold this handler open.
            with suppress(asyncio.TimeoutError, ConnectionError, OSError):
                await asyncio.wait_for(writer.wait_closed(), timeout=1)

    def _get_attach_error(self, request: dict) -> str:
        if request.get("type") != "attach":
            return "first message must be attach"
        if request.get("protocol_version") != PROTOCOL_VERSION:
            return "client protocol version does not match daemon"
        if request.get("run_id") != self.run_id:
            return "client run id does not match daemon"
        if request.get("cwd") != self.cwd:
            return "client working directory does not match daemon"
        return ""

    def _handle_command(self, message: dict) -> None:
        if message.get("type") != "command":
            self._send_notice("unknown session message", "error")
            return

        action = message.get("action")
        key = message.get("key")
        if action == "cancel":
            if not isinstance(key, str) or not self.runner.cancel(key):
                self._send_notice(f"{key or 'PR'} is not queued or running", "warning")
        elif action == "resume":
            if not isinstance(key, str) or not self.runner.resume(key):
                self._send_notice(f"{key or 'PR'} is not cancelled", "warning")
        elif action == "run_now":
            self.runner.run_now()
            self._send_snapshot()
        elif action == "toggle_pause":
            self.runner.toggle_pause()
        elif action == "stop":
            self._send_notice("stopping background daemon", "information")
            self.runner.stop()
        else:
            self._send_notice(f"unknown command: {action}", "error")

    def _send_notice(self, message: str, severity: str) -> None:
        self._send_to_client(
            {"type": "notice", "message": message, "severity": severity}
        )

    async def _recycle(self) -> None:
        """Hand the loop to a fresh daemon, wiping `.tmp/` on the way."""
        self._send_to_client(
            {
                "type": "recycling",
                "message": "daemon restarting to clear .tmp/ after "
                f"{RECYCLE_EVERY_LOOPS} loops",
            }
        )
        try:
            try:
                # Held until this process exits: releasing it before the successor
                # binds its socket would let another launch race it.
                self._start_lock = await asyncio.to_thread(
                    _take_start_lock, self.socket_path
                )
            finally:
                # Runs exactly once on every path: a recycle that failed before or
                # during the shutdown must still leave no stale socket or
                # daemon.json behind, and after a successful spawn a second
                # shutdown would delete the successor's socket.
                await self._shutdown()

            # Every child of the caller's `.tmp/`, other tools' files included -
            # the agreed scope. The name guard keeps a custom base_dir from
            # deleting more.
            tmp_dir = self.store.base_dir.parent
            if tmp_dir.name == ".tmp":
                for child in tmp_dir.iterdir():
                    try:
                        if child.is_dir() and not child.is_symlink():
                            shutil.rmtree(child, ignore_errors=True)
                        else:
                            child.unlink(missing_ok=True)
                    except OSError:
                        continue

            handoff = {
                "loop_index": self.runner.loop_index,
                "next_run_at": time.time() + self.config.interval_min * 60,
                "paused": self.runner.paused,
                "record": None
                if self.runner.record is None
                else self.runner.record.to_dict(),
                "recycle_count": self.recycle_count + 1,
            }
            _start_daemon_locked(self.config, self.store, handoff=handoff)
            # The successor's socket is live and this process still holds its
            # lock, so the sweep skips that pair and clears older stale ones.
            _remove_stale_runtime_files(self.socket_path.parent)
        except Exception as error:
            # Any recycle failure, not just SessionError. This process' own stdout
            # is a deleted file by now, so the successor's log is the only place
            # the reason can still be read.
            try:
                log_path = self.store.base_dir / DAEMON_LOG_FILE_NAME
                log_path.parent.mkdir(parents=True, exist_ok=True)
                with log_path.open("a", encoding="utf-8") as daemon_log:
                    daemon_log.write(f"recycle failed: {error}\n")
            except OSError:
                pass

    async def _shutdown(self) -> None:
        # list(): awaiting wait_closed lets handlers finish and discard from the
        # set; iterating the set itself would then resize mid-loop.
        for writer in list(self._client_writers):
            # abort(), never close(): a client that stopped reading holds a full
            # buffer, and a shutdown must not wait for it to drain.
            writer.transport.abort()
            with suppress(asyncio.TimeoutError, ConnectionError, OSError):
                await asyncio.wait_for(writer.wait_closed(), timeout=1)
        self._client_writers.clear()
        self._client_writer = None
        # A successor that already wrote its own daemon.json owns the socket
        # path, and unlinking it would delete the successor's live socket. While
        # this daemon still owns it, unlink before closing the listener: closing
        # first lets a successor bind the same path before the unlink.
        metadata = load_runtime_metadata(self.store)
        owns_runtime = metadata is not None and metadata.run_id == self.run_id
        if owns_runtime:
            self.socket_path.unlink(missing_ok=True)
        if self._server is not None:
            self._server.close()
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._server.wait_closed(), timeout=1)
        # Read again: a successor can write its daemon.json during that wait. No
        # await may sit between this check and the unlink.
        metadata = load_runtime_metadata(self.store)
        if metadata is not None and metadata.run_id == self.run_id:
            get_runtime_path(self.store).unlink(missing_ok=True)
