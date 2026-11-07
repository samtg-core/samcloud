"""Client-owned plane-ID -> Hermes stored-ID map, not a transcript or queue."""
import fcntl
import json
import os
from pathlib import Path
import tempfile
import uuid


class BindingError(RuntimeError):
    pass


class Binding:
    """Hold one local writer for a conversation until its native client exits.

    The lock is advisory and local to this profile; it is not a plane lease.
    Persist the native key before accepting input. A crash in the gap between
    native creation and its first persisted prompt can leave an empty native
    draft: resume must report that error, never substitute a newer session.
    """

    def __init__(self, home, plane_id, *, resume):
        self.home = Path(home).resolve(strict=True)
        try:
            canonical = str(uuid.UUID(plane_id))
        except (ValueError, TypeError, AttributeError) as error:
            raise BindingError("The plane conversation ID must be a UUID") from error
        if canonical != plane_id:
            raise BindingError("Use the canonical plane conversation UUID")
        self.plane_id, self.resume = plane_id, resume
        self.directory = self.home / ".samcloud-client" / "conversations"
        self.path = self.directory / (plane_id + ".json")
        self._lock = None

    def __enter__(self):
        self.directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        self.directory.parent.chmod(0o700)
        self.directory.chmod(0o700)
        fd = os.open(self.directory / (self.plane_id + ".lock"),
                     os.O_CREAT | os.O_RDWR, 0o600)
        self._lock = os.fdopen(fd, "a+")
        try:
            os.fchmod(fd, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if self.resume:
                self.stored_id = self._read()
            elif self.path.exists():
                raise BindingError("Conversation already has a mapping; explicitly resume it")
            else:
                self.stored_id = None
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def _read(self):
        try:
            value = json.loads(self.path.read_text())
        except (OSError, ValueError) as error:
            raise BindingError("Cannot read the existing conversation mapping") from error
        if (not isinstance(value, dict)
                or set(value) != {"version", "profile_home", "plane_id", "stored_id"}
                or type(value["version"]) is not int or value["version"] != 1
                or value["profile_home"] != str(self.home)
                or value["plane_id"] != self.plane_id
                or not isinstance(value["stored_id"], str) or not value["stored_id"]):
            raise BindingError("Invalid conversation mapping; no fallback session selected")
        return value["stored_id"]

    def save(self, stored_id):
        if self._lock is None or self._lock.closed:
            raise BindingError("A mapping write requires its conversation lock")
        if not isinstance(stored_id, str) or not stored_id:
            raise BindingError("Hermes must supply a nonempty stored session ID")
        value = {"version": 1, "profile_home": str(self.home),
                 "plane_id": self.plane_id, "stored_id": stored_id}
        fd, temporary = tempfile.mkstemp(prefix=".mapping-", dir=self.directory)
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(value, stream)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            directory_fd = os.open(self.directory, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            self.stored_id = stored_id
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def __exit__(self, *_):
        if self._lock is not None:
            self._lock.close()
