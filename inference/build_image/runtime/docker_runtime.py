import os
import io
import tarfile

import docker
from docker.models.containers import Container

class DockerRuntime:
    def __init__(self, image: str, name: str = None, command=["/bin/bash", "-l"], **docker_kwargs):
        self.client = docker.from_env()
        self.image = image
        self.command = command
        self.name = name or make_name(image)
        self.docker_kwargs = docker_kwargs
        self.container: Container | None = None
        self.start_container()

    def start_container(self):
        existing = self.client.containers.list(all=True, filters={"name": self.name})
        if existing:
            ctr = existing[0]
            if ctr.status != "running":
                ctr.start()
            self.container = ctr
        else:
            self.container = self.client.containers.run(
                self.image, self.command,
                name=self.name, detach=True, tty=True, stdin_open=True,
                **self.docker_kwargs
            )

    def copy_to_container(self, src_path: str, dest_path: str):
        tar_stream = io.BytesIO()
        with tarfile.open(fileobj=tar_stream, mode="w") as tar:
            tar.add(src_path, arcname=os.path.basename(dest_path))
        tar_stream.seek(0)
        self.container.put_archive(os.path.dirname(dest_path), tar_stream.read())

    def stop(self):
        if self.container:
            self.container.stop()
            self.container.remove()
        self.client.close()
