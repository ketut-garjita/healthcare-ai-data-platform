"""Bounded asynchronous MLLP server independent from HL7 parsing and persistence."""

import asyncio
import logging
from collections.abc import Awaitable, Callable

MLLP_START = b"\x0b"
MLLP_END = b"\x1c"
MLLP_CR = b"\x0d"
MessageHandler = Callable[[bytes], Awaitable[bytes]]
logger = logging.getLogger(__name__)


class MllpFrameError(ValueError):
    """A malformed or oversized MLLP frame."""


class MllpServer:
    def __init__(
        self,
        handler: MessageHandler,
        *,
        host: str = "127.0.0.1",
        port: int = 2575,
        max_message_bytes: int = 1024 * 1024,
        connection_timeout: float = 10.0,
        read_timeout: float = 30.0,
        max_connections: int = 20,
    ) -> None:
        self.handler = handler
        self.host = host
        self.port = port
        if max_connections < 1:
            raise ValueError("max_connections must be at least one")
        self.max_message_bytes = max_message_bytes
        self.connection_timeout = connection_timeout
        self.read_timeout = read_timeout
        self.max_connections = max_connections
        self._admitted_connections = 0
        self._client_tasks: set[asyncio.Task[None]] = set()
        self._server: asyncio.AbstractServer | None = None

    @property
    def active_connections(self) -> int:
        """Number of admitted live client connections, including frame waiters."""
        return self._admitted_connections

    @property
    def pending_connections(self) -> int:
        """Connections waiting for admission; excess connections are closed immediately."""
        return 0

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._client_connected,
            self.host,
            self.port,
            limit=self.max_message_bytes + 4,
            backlog=100,
        )

    async def serve_forever(self) -> None:
        if self._server is None:
            await self.start()
        assert self._server is not None
        async with self._server:
            await self._server.serve_forever()

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        tasks = tuple(self._client_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _client_connected(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        # asyncio invokes a regular client callback synchronously from connection_made.
        # Reserve a slot before scheduling any task, so bursts cannot accumulate waiters.
        if self._admitted_connections >= self.max_connections:
            writer.close()
            return
        self._admitted_connections += 1
        task = asyncio.create_task(self._handle_client(reader, writer))
        self._client_tasks.add(task)
        task.add_done_callback(self._client_tasks.discard)

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        peer = writer.get_extra_info("peername")
        try:
            first_frame = True
            while True:
                message = await self._read_frame(
                    reader, self.connection_timeout if first_frame else self.read_timeout
                )
                first_frame = False
                response = await self.handler(message)
                writer.write(response)
                async with asyncio.timeout(self.read_timeout):
                    await writer.drain()
        except (asyncio.IncompleteReadError, TimeoutError, ConnectionError):
            pass
        except MllpFrameError as exc:
            logger.warning("Rejected malformed MLLP frame: %s", exc)
        except asyncio.LimitOverrunError:
            logger.warning("Rejected oversized MLLP frame")
        except Exception as exc:
            logger.error("MLLP message handler failed", extra={"error_type": type(exc).__name__})
        finally:
            self._admitted_connections -= 1
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                logger.debug("MLLP client connection closed", extra={"peer": str(peer)})

    async def _read_frame(
        self, reader: asyncio.StreamReader, timeout: float | None = None
    ) -> bytes:
        async with asyncio.timeout(timeout if timeout is not None else self.read_timeout):
            start = await reader.readexactly(1)
            if start != MLLP_START:
                raise MllpFrameError("frame does not start with MLLP start block")
            payload = await reader.readuntil(MLLP_END)
            if len(payload) - 1 > self.max_message_bytes:
                raise MllpFrameError("message exceeds configured size limit")
            if not payload.endswith(MLLP_END):
                raise MllpFrameError("frame has no end block")
            terminator = await reader.readexactly(1)
            if terminator != MLLP_CR:
                raise MllpFrameError("frame has no carriage return terminator")
            return payload[:-1]
