import asyncio
import datetime
import logging
import os
import sys
import uuid
from collections import defaultdict
from json.decoder import JSONDecodeError

from .. import fileio
from .. import serde as json

logger = logging.getLogger(__name__)


class DurableBuffer:
    def __init__(self, dir_, key, loop, write_time=1.0):
        self._base_path = os.path.join(os.path.expanduser(dir_))
        self._message_path = os.path.join(self._base_path, "messages")
        self._manifest_path = os.path.join(self._base_path, f"manifest-{key}")
        self._loop = loop
        self.q = asyncio.Queue(loop=self._loop)
        self.deferrer = fileio.Deferrer(loop=self._loop)
        self._manifest_lock = asyncio.Lock(loop=self._loop)
        self._manifest_dirty = asyncio.Event(loop=self._loop)
        self._manifest_clean = asyncio.Event(loop=self._loop)
        self._write_time = write_time
        self.ready = asyncio.Event(loop=self._loop)
        self._loop.create_task(self.start_manifest())

    def clean(self):
        self._manifest_dirty.clear()
        self._manifest_clean.set()

    def dirty(self):
        self._manifest_dirty.set()
        self._manifest_clean.clear()

    async def start_manifest(self):
        try:
            os.makedirs(self._message_path, mode=0o700)
        except Exception:
            pass

        loaded_items = await self._read_manifest()

        for item in loaded_items:
            await self.q.put(item)

        self.ready.set()
        self._loop.create_task(self.manifest_writer(self._write_time))

    async def put(self, framed_message):
        await self.ready.wait()
        path = os.path.join(self._message_path, str(uuid.uuid4()))
        item = {
            "path": path,
            "expire_time": datetime.datetime.utcnow() + datetime.timedelta(minutes=5),
        }

        # using async with fileio.File here can cause us to run out of
        # file descriptors as the actual file work can be delayed behind other
        # file open events.  We want to run this job all in one shot.
        def _f():
            with open(path, "wb") as fp:
                if isinstance(framed_message, bytes):
                    fp.write(framed_message)
                else:
                    fp.writelines(framed_message)

        await self.deferrer.defer(_f)
        await self.put_ident(item)

    async def put_ident(self, ident):
        await self.q.put(ident)
        self.dirty()

    async def get(self):
        await self.ready.wait()
        while True:
            item = await self.q.get()
            self.dirty()
            try:
                if self.is_expired(item):
                    self.expire(item)
                    continue
                return item
            except (TypeError, KeyError):
                logger.debug(
                    "Something bad was in the durable buffer manifest: %s", item, exc_info=True
                )

    async def _read_manifest(self):
        try:
            async with fileio.File(self._manifest_path, "r") as fp:
                data = await fp.read()
        except FileNotFoundError:
            return []
        else:
            try:
                return json.loads(data)
            except JSONDecodeError:
                logger.error("failed to decode manifest: %s", data)
            except Exception:
                logger.exception("Unknown failure in decoding manifest: %s", data)
            finally:
                return []

    def _remove_path(self, path):
        if os.path.exists(path):
            os.remove(path)
        else:
            logger.info("Can't remove {}, doesn't exist".format(path))

    def is_expired(self, item):
        return item["expire_time"] < datetime.datetime.utcnow()

    async def expire(self, item):
        logger.info("Expiring message %s", item["path"])
        await self._deferrer.defer(self._remove_path, item["path"])

    async def expire_all(self):
        async with self._manifest_lock:
            old, self.q = self.q, asyncio.Queue(loop=self._loop)
            while old.qsize() > 0:
                item = await old.get()
                if self.is_expired(item):
                    await self.expire(item)
                else:
                    await self.q.put(item)
            self.dirty()

    async def manifest_writer(self, write_time):
        while True:
            await self._manifest_dirty.wait()
            async with self._manifest_lock:
                try:
                    async with fileio.File(self._manifest_path, "w") as fp:
                        await fp.write(json.dumps(list(self.q._queue)))
                    self.clean()
                except Exception:
                    logger.exception("Failed to write manifest for %s", self._manifest_path)
            await asyncio.sleep(write_time)


class FileBufferManager(defaultdict):
    def __init__(self, path, loop=asyncio.get_event_loop()):
        self.path = path
        self.loop = loop

    def __missing__(self, key):
        self[key] = DurableBuffer(self.path, key, self.loop)
        return self[key]
