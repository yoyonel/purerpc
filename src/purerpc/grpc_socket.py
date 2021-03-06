import sys
import enum
import socket
import datetime

import anyio
import async_exit_stack
from async_generator import async_generator, yield_, yield_from_
from purerpc.utils import is_darwin, is_windows
from purerpc.grpclib.exceptions import ProtocolError

from .grpclib.connection import GRPCConfiguration, GRPCConnection
from .grpclib.events import RequestReceived, RequestEnded, ResponseEnded, MessageReceived, WindowUpdated
from .grpclib.buffers import MessageWriteBuffer, MessageReadBuffer
from .grpclib.exceptions import StreamClosedError


class SocketWrapper(async_exit_stack.AsyncExitStack):
    def __init__(self, grpc_connection: GRPCConnection, sock: anyio.SocketStream):
        super().__init__()
        self._set_socket_options(sock)
        self._socket = sock
        self._grpc_connection = grpc_connection
        self._flush_event = anyio.create_event()
        self._running = True

    async def __aenter__(self):
        await super().__aenter__()
        task_group = await self.enter_async_context(anyio.create_task_group())
        await task_group.spawn(self._writer_thread)

        async def callback():
            self._running = False
            await self._flush_event.set()

        self.push_async_callback(callback)
        return self

    @staticmethod
    def _set_socket_options(sock: anyio.SocketStream):
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        if hasattr(socket, "TCP_KEEPIDLE"):
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 300)
        elif is_darwin():
            # Darwin specific option
            TCP_KEEPALIVE = 16
            sock.setsockopt(socket.IPPROTO_TCP, TCP_KEEPALIVE, 300)
        if not is_windows():
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 30)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 5)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    async def _writer_thread(self):
        while True:
            data = self._grpc_connection.data_to_send()
            if data:
                await self._socket.send_all(data)
            elif self._running:
                await self._flush_event.wait()
                self._flush_event.clear()
            else:
                return

    async def flush(self):
        """This maybe called from different threads."""
        await self._flush_event.set()

    async def recv(self, buffer_size: int):
        """This may only be called from single thread."""
        return await self._socket.receive_some(buffer_size)


class GRPCStreamState(enum.Enum):
    OPEN = 1
    HALF_CLOSED_REMOTE = 2
    HALF_CLOSED_LOCAL = 3
    CLOSED = 4


class GRPCStream:
    def __init__(self, grpc_connection: GRPCConnection, stream_id: int, socket: SocketWrapper,
                 grpc_socket: "GRPCSocket"):
        self._stream_id = stream_id
        self._grpc_connection = grpc_connection
        self._grpc_socket = grpc_socket
        self._socket = socket
        self._flow_control_update_event = anyio.create_event()
        self._incoming_events = anyio.create_queue(sys.maxsize)
        self._response_started = False
        self._state = GRPCStreamState.OPEN
        self._start_stream_event = None
        self._end_stream_event = None

    @property
    def state(self):
        return self._state

    @property
    def start_stream_event(self):
        return self._start_stream_event

    @property
    def end_stream_event(self):
        return self._end_stream_event

    @property
    def stream_id(self):
        return self._stream_id

    @property
    def client_side(self):
        return self._grpc_connection.config.client_side

    @property
    def debug_prefix(self):
        return "[CLIENT] " if self.client_side else "[SERVER] "

    def _close_remote(self):
        if self._state == GRPCStreamState.OPEN:
            self._state = GRPCStreamState.HALF_CLOSED_REMOTE
        elif self._state == GRPCStreamState.HALF_CLOSED_LOCAL:
            self._state = GRPCStreamState.CLOSED
            del self._grpc_socket._streams[self._stream_id]

    def _close_local(self):
        if self._state == GRPCStreamState.OPEN:
            self._state = GRPCStreamState.HALF_CLOSED_LOCAL
        elif self._state == GRPCStreamState.HALF_CLOSED_REMOTE:
            self._state = GRPCStreamState.CLOSED
            del self._grpc_socket._streams[self._stream_id]

    async def _set_flow_control_update(self):
        await self._flow_control_update_event.set()

    async def _wait_flow_control_update(self):
        await self._flow_control_update_event.wait()
        self._flow_control_update_event.clear()

    async def _send(self, message: bytes, compress=False):
        message_write_buffer = MessageWriteBuffer(self._grpc_connection.config.message_encoding,
                                                  self._grpc_connection.config.max_message_length)
        message_write_buffer.write_message(message, compress)
        while message_write_buffer:
            window_size = self._grpc_connection.flow_control_window(self._stream_id)
            if window_size <= 0:
                await self._wait_flow_control_update()
                continue
            num_data_to_send = min(window_size, len(message_write_buffer))
            data = message_write_buffer.data_to_send(num_data_to_send)
            self._grpc_connection.send_data(self._stream_id, data)
            await self._socket.flush()

    async def _receive(self):
        event = await self._incoming_events.get()
        if isinstance(event, MessageReceived):
            self._grpc_connection.acknowledge_received_data(self._stream_id,
                                                            event.flow_controlled_length)
            await self._socket.flush()
        elif isinstance(event, RequestEnded) or isinstance(event, ResponseEnded):
            assert self._end_stream_event is None
            self._end_stream_event = event
        else:
            assert self._start_stream_event is None
            self._start_stream_event = event
        return event

    async def close(self, status=None, content_type_suffix="", custom_metadata=()):
        if self.client_side and (status or custom_metadata):
            raise ValueError("Client side streams cannot be closed with non-default arguments")
        if self._state in (GRPCStreamState.HALF_CLOSED_LOCAL, GRPCStreamState.CLOSED):
            raise TypeError("Closing already closed stream")
        self._close_local()
        if self.client_side:
            try:
                self._grpc_connection.end_request(self._stream_id)
            except StreamClosedError:
                # Remote end already closed connection, do nothing here
                pass
        elif self._response_started:
            self._grpc_connection.end_response(self._stream_id, status, custom_metadata)
        else:
            self._grpc_connection.respond_status(self._stream_id, status,
                                                 content_type_suffix, custom_metadata)
        await self._socket.flush()

    async def start_response(self, content_type_suffix="", custom_metadata=()):
        if self.client_side:
            raise ValueError("Cannot start response on client-side socket")
        self._grpc_connection.start_response(self._stream_id, content_type_suffix, custom_metadata)
        self._response_started = True
        await self._socket.flush()


# TODO: this name is not correct, should be something like GRPCConnection (but this name is already
# occupied)
class GRPCSocket(async_exit_stack.AsyncExitStack):
    StreamClass = GRPCStream

    def __init__(self, config: GRPCConfiguration, sock,
                 receive_buffer_size=1024*1024):
        super().__init__()
        self._grpc_connection = GRPCConnection(config=config)
        self._socket = SocketWrapper(self._grpc_connection, sock)
        self._receive_buffer_size = receive_buffer_size
        self._streams = {}  # type: Dict[int, GRPCStream]

    async def __aenter__(self):
        await super().__aenter__()
        self._socket = await self.enter_async_context(self._socket)
        self._grpc_connection.initiate_connection()
        await self._socket.flush()
        if self.client_side:
            task_group = await self.enter_async_context(anyio.create_task_group())
            self.push_async_callback(task_group.cancel_scope.cancel)
            await task_group.spawn(self._reader_thread)
        return self

    @property
    def client_side(self):
        return self._grpc_connection.config.client_side

    def _stream_ctor(self, stream_id):
        return self.StreamClass(self._grpc_connection, stream_id, self._socket, self)

    def _allocate_stream(self, stream_id):
        self._streams[stream_id] = self._stream_ctor(stream_id)
        return self._streams[stream_id]

    @async_generator
    async def _listen(self):
        while True:
            data = await self._socket.recv(self._receive_buffer_size)
            if not data:
                return
            events = self._grpc_connection.receive_data(data)
            await self._socket.flush()
            for event in events:
                if isinstance(event, WindowUpdated):
                    if event.stream_id == 0:
                        for stream in self._streams.values():
                            await stream._set_flow_control_update()
                    elif event.stream_id in self._streams:
                        await self._streams[event.stream_id]._set_flow_control_update()
                    continue
                elif isinstance(event, RequestReceived):
                    self._allocate_stream(event.stream_id)

                await self._streams[event.stream_id]._incoming_events.put(event)

                if isinstance(event, RequestReceived):
                    await yield_(self._streams[event.stream_id])
                elif isinstance(event, ResponseEnded) or isinstance(event, RequestEnded):
                    self._streams[event.stream_id]._close_remote()

    async def _reader_thread(self):
        async for _ in self._listen():
            raise ProtocolError("Received request on client end")

    @async_generator
    async def listen(self):
        if self.client_side:
            raise ValueError("Cannot listen client-side socket")
        await yield_from_(self._listen())

    async def start_request(self, scheme: str, service_name: str, method_name: str,
                            message_type=None, authority=None, timeout: datetime.timedelta=None,
                            content_type_suffix="", custom_metadata=()):
        if not self.client_side:
            raise ValueError("Cannot start request on server-side socket")
        stream_id = self._grpc_connection.get_next_available_stream_id()
        stream = self._allocate_stream(stream_id)
        self._grpc_connection.start_request(stream_id, scheme, service_name, method_name,
                                            message_type, authority, timeout,
                                            content_type_suffix, custom_metadata)
        await self._socket.flush()
        return stream
