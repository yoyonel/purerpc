import curio
import time
import purerpc
from generated.greeter_pb2 import HelloRequest
from generated.greeter_grpc import GreeterStub


async def worker(channel):
    stub = GreeterStub(channel)
    for i in range(100):
        data = "World" * 1
        response = await stub.SayHello(HelloRequest(name=data))
        assert(response.message == "Hello, " + data)


async def main_coro():
    # await curio.spawn(print_memory_growth_statistics(), daemon=True)
    async with purerpc.insecure_channel("localhost", 50055) as channel:
        for i in range(100):
            start = time.time()
            async with curio.TaskGroup() as task_group:
                for i in range(100):
                    await task_group.spawn(worker(channel))
            print("RPS: {}".format(10000 / (time.time() - start)))


def main():
    curio.run(main_coro)


if __name__ == "__main__":
    main()
