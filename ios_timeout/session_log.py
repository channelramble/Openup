"""Bounded, memory-only transcripts independent of the GUI's visible scrollback."""

from collections import deque


OMISSION_MARKER = (
    "\n\n[Output omitted: the in-memory transcript limit was reached. "
    "Initial context and the latest output are retained.]\n\n"
)
_CHUNK_BYTES = 64 * 1024


class SessionLog:
    """Keep complete output up to a byte limit, then retain its beginning and end."""

    def __init__(self, max_bytes=16 * 1024 * 1024):
        if type(max_bytes) is not int or max_bytes < len(OMISSION_MARKER.encode()) + 64:
            raise ValueError("The transcript limit must leave room for context and its omission marker.")
        self.max_bytes = max_bytes
        self.clear()

    def clear(self):
        self._head = None
        self._chunks = deque()
        self._size = 0

    def append(self, text):
        data = text.encode("utf-8", "replace")
        if not data:
            return
        if self._head is None and self._size + len(data) > self.max_bytes:
            previous = b"".join(self._chunks)
            available = self.max_bytes - len(OMISSION_MARKER.encode())
            # Trim only incomplete UTF-8 characters at the omission boundaries.
            head_budget = available // 4
            head = previous[:head_budget] + data[:max(0, head_budget - len(previous))]
            self._head = head.decode("utf-8", "ignore").encode("utf-8")
            tail_budget = available - len(self._head)
            tail = (data[-tail_budget:] if len(data) >= tail_budget else
                    previous[-(tail_budget - len(data)):] + data)
            tail = tail.decode("utf-8", "ignore").encode("utf-8")
            self._chunks.clear()
            self._size = 0
            self._append_bytes(tail)
            return
        if self._head is None:
            self._append_bytes(data)
            return
        budget = self.max_bytes - len(self._head) - len(OMISSION_MARKER.encode())
        if len(data) >= budget:
            # Do not retain or allocate chunks for a single oversized update.
            data = data[-budget:].decode("utf-8", "ignore").encode("utf-8")
            self._chunks.clear()
            self._size = 0
        self._append_bytes(data)
        if self._head is not None:
            while self._size > budget:
                first = self._chunks[0]
                excess = self._size - budget
                if len(first) <= excess:
                    self._chunks.popleft()
                    self._size -= len(first)
                else:
                    del first[:excess]
                    self._size -= excess
            # A chunk boundary may split a character; remove at most its
            # remaining continuation bytes from the start of the retained tail.
            while self._chunks and 0x80 <= self._chunks[0][0] <= 0xBF:
                del self._chunks[0][0]
                self._size -= 1
                if not self._chunks[0]:
                    self._chunks.popleft()

    def _append_bytes(self, data):
        view = memoryview(data)
        offset = 0
        while offset < len(view):
            if not self._chunks or len(self._chunks[-1]) == _CHUNK_BYTES:
                self._chunks.append(bytearray())
            size = min(_CHUNK_BYTES - len(self._chunks[-1]), len(view) - offset)
            self._chunks[-1].extend(view[offset:offset + size])
            self._size += size
            offset += size

    def text(self):
        tail = b"".join(self._chunks).decode("utf-8")
        if self._head is None:
            return tail
        return self._head.decode("utf-8") + OMISSION_MARKER + tail
