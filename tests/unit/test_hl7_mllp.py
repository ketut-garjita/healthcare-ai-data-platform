import asyncio

import pytest

from healthcare_platform.ingestion.hl7_mllp import ack_code_for_result
from healthcare_platform.integrations.hl7.ack import build_ack
from healthcare_platform.integrations.hl7.mllp import (
    MLLP_CR,
    MLLP_END,
    MLLP_START,
    MllpFrameError,
    MllpServer,
)

MESSAGE = b"MSH|^~\\&|SND|SF|RCV|RF|20250101120000||ADT^A01|control-7|P|2.5.1\r"


def test_ack_swaps_sender_and_receiver_and_preserves_control_id() -> None:
    ack = build_ack(MESSAGE, "AA")
    assert ack.startswith(MLLP_START)
    assert ack.endswith(MLLP_END + MLLP_CR)
    text = ack[1:-2].decode()
    assert "|RCV|RF|SND|SF|" in text
    assert "MSA|AA|control-7" in text


def test_ack_codes_map_terminal_ingestion_outcomes() -> None:
    assert ack_code_for_result({"outcome": "accepted"}) == "AA"
    assert ack_code_for_result({"outcome": "unchanged"}) == "AA"
    assert ack_code_for_result({"outcome": "conflict"}) == "AR"
    assert ack_code_for_result({"outcome": "rejected"}) == "AR"
    assert ack_code_for_result({"outcome": "failed"}) == "AE"


def test_mllp_reads_multiple_frames_on_one_connection() -> None:
    async def run() -> None:
        received: list[bytes] = []

        async def handler(payload: bytes) -> bytes:
            received.append(payload)
            return b"ack"

        server = MllpServer(handler, host="127.0.0.1", port=0, max_message_bytes=2048)
        await server.start()
        assert server._server is not None
        port = server._server.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(MLLP_START + MESSAGE + MLLP_END + MLLP_CR)
        writer.write(MLLP_START + MESSAGE + MLLP_END + MLLP_CR)
        await writer.drain()
        assert await reader.readexactly(6) == b"ackack"
        assert received == [MESSAGE, MESSAGE]
        writer.close()
        await writer.wait_closed()
        await server.close()

    asyncio.run(run())


def test_mllp_rejects_oversized_and_malformed_frames() -> None:
    async def run() -> None:
        server = MllpServer(lambda payload: asyncio.sleep(0, result=b"ack"), max_message_bytes=5)
        reader = asyncio.StreamReader()
        reader.feed_data(MLLP_START + b"123456" + MLLP_END + MLLP_CR)
        with pytest.raises(MllpFrameError):
            await server._read_frame(reader)
        reader = asyncio.StreamReader()
        reader.feed_data(b"X")
        with pytest.raises(MllpFrameError):
            await server._read_frame(reader)

    asyncio.run(run())


def test_mllp_reassembles_tcp_fragmentation_without_losing_bytes() -> None:
    async def run() -> None:
        received: list[bytes] = []
        processed = asyncio.Event()

        async def handler(payload: bytes) -> bytes:
            received.append(payload)
            processed.set()
            return b"ack"

        server = MllpServer(handler, host="127.0.0.1", port=0)
        await server.start()
        assert server._server is not None
        port = server._server.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        split = len(MESSAGE) // 2
        writer.write(MLLP_START + MESSAGE[:split])
        await writer.drain()
        await asyncio.sleep(0.01)
        writer.write(MESSAGE[split:] + MLLP_END + MLLP_CR)
        await writer.drain()
        await asyncio.wait_for(processed.wait(), timeout=1)
        assert await reader.readexactly(3) == b"ack"
        assert received == [MESSAGE]
        writer.close()
        await writer.wait_closed()
        await server.close()

    asyncio.run(run())


def test_mllp_discards_interrupted_frame_and_accepts_next_client() -> None:
    async def run() -> None:
        received: list[bytes] = []

        async def handler(payload: bytes) -> bytes:
            received.append(payload)
            return b"ack"

        server = MllpServer(handler, host="127.0.0.1", port=0, max_connections=1)
        await server.start()
        assert server._server is not None
        port = server._server.sockets[0].getsockname()[1]
        partial_reader, partial_writer = await asyncio.open_connection("127.0.0.1", port)
        partial_writer.write(MLLP_START + MESSAGE[:12])
        await partial_writer.drain()
        partial_writer.close()
        await partial_writer.wait_closed()

        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(MLLP_START + MESSAGE + MLLP_END + MLLP_CR)
        await writer.drain()
        assert await reader.readexactly(3) == b"ack"
        assert received == [MESSAGE]
        assert server.active_connections <= 1
        writer.close()
        await writer.wait_closed()
        await server.close()

    asyncio.run(run())


def test_mllp_connection_admission_rejects_bursts_and_releases_slots() -> None:
    async def run() -> None:
        received: list[bytes] = []

        async def handler(payload: bytes) -> bytes:
            received.append(payload)
            return b"ack"

        server = MllpServer(
            handler,
            host="127.0.0.1",
            port=0,
            connection_timeout=2,
            read_timeout=2,
            max_connections=1,
        )
        await server.start()
        assert server._server is not None
        port = server._server.sockets[0].getsockname()[1]
        first_reader, first_writer = await asyncio.open_connection("127.0.0.1", port)
        for _ in range(20):
            if server.active_connections == 1:
                break
            await asyncio.sleep(0)
        assert server.active_connections == 1

        excess = [await asyncio.open_connection("127.0.0.1", port) for _ in range(8)]
        for waiting_reader, _ in excess:
            assert await asyncio.wait_for(waiting_reader.read(1), timeout=1) == b""
        assert server.active_connections == 1
        assert server.pending_connections == 0
        for _, waiting_writer in excess:
            waiting_writer.close()
            await waiting_writer.wait_closed()

        first_writer.close()
        await first_writer.wait_closed()
        for _ in range(20):
            if server.active_connections == 0:
                break
            await asyncio.sleep(0)
        assert server.active_connections == 0

        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(MLLP_START + MESSAGE + MLLP_END + MLLP_CR)
        await writer.drain()
        assert await reader.readexactly(3) == b"ack"
        assert received == [MESSAGE]
        writer.close()
        await writer.wait_closed()
        await server.close()
        assert server.active_connections == 0

    asyncio.run(run())


def test_mllp_admission_slots_release_on_timeout_exception_and_shutdown() -> None:
    async def run() -> None:
        server = MllpServer(
            lambda payload: asyncio.sleep(0, result=b"ack"),
            host="127.0.0.1",
            port=0,
            connection_timeout=0.05,
            read_timeout=0.2,
            max_connections=1,
        )
        await server.start()
        assert server._server is not None
        port = server._server.sockets[0].getsockname()[1]

        timeout_reader, timeout_writer = await asyncio.open_connection("127.0.0.1", port)
        assert await asyncio.wait_for(timeout_reader.read(1), timeout=1) == b""
        timeout_writer.close()
        await timeout_writer.wait_closed()
        for _ in range(20):
            if server.active_connections == 0:
                break
            await asyncio.sleep(0)
        assert server.active_connections == 0

        async def failing_handler(payload: bytes) -> bytes:
            raise RuntimeError("synthetic handler failure")

        server.handler = failing_handler
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(MLLP_START + MESSAGE + MLLP_END + MLLP_CR)
        await writer.drain()
        assert await asyncio.wait_for(reader.read(1), timeout=1) == b""
        writer.close()
        await writer.wait_closed()
        for _ in range(20):
            if server.active_connections == 0:
                break
            await asyncio.sleep(0)
        assert server.active_connections == 0

        server.handler = lambda payload: asyncio.sleep(0, result=b"ack")
        idle_reader, idle_writer = await asyncio.open_connection("127.0.0.1", port)
        for _ in range(20):
            if server.active_connections == 1:
                break
            await asyncio.sleep(0)
        assert server.active_connections == 1
        await server.close()
        assert server.active_connections == 0
        assert await asyncio.wait_for(idle_reader.read(1), timeout=1) == b""
        idle_writer.close()
        await idle_writer.wait_closed()

    asyncio.run(run())
