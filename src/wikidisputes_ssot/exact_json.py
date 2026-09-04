from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from mmap import mmap

type ByteView = bytes | bytearray | memoryview | mmap


@dataclass(frozen=True)
class Span:
    start: int
    end: int

    @property
    def length(self) -> int:
        return self.end - self.start


WHITESPACE = frozenset(b" \t\r\n")


def skip_ws(data: ByteView, position: int, limit: int | None = None) -> int:
    bound = len(data) if limit is None else limit
    while position < bound and data[position] in WHITESPACE:
        position += 1
    return position


def string_end(data: ByteView, start: int, limit: int | None = None) -> int:
    bound = len(data) if limit is None else limit
    if start >= bound or data[start] != 0x22:
        raise ValueError(f"expected JSON string at byte {start}")
    position = start + 1
    while position < bound:
        byte = data[position]
        if byte == 0x22:
            return position + 1
        if byte == 0x5C:
            position += 2
        else:
            position += 1
    raise ValueError(f"unterminated JSON string at byte {start}")


def value_end(data: ByteView, start: int, limit: int | None = None) -> int:
    bound = len(data) if limit is None else limit
    position = skip_ws(data, start, bound)
    if position >= bound:
        raise ValueError("expected JSON value at end of input")
    opener = data[position]
    if opener == 0x22:
        return string_end(data, position, bound)
    if opener in (0x7B, 0x5B):  # { or [
        stack = [opener]
        position += 1
        while position < bound and stack:
            byte = data[position]
            if byte == 0x22:
                position = string_end(data, position, bound)
                continue
            if byte in (0x7B, 0x5B):
                stack.append(byte)
            elif byte == 0x7D:
                if stack[-1] != 0x7B:
                    raise ValueError(f"mismatched JSON object close at {position}")
                stack.pop()
            elif byte == 0x5D:
                if stack[-1] != 0x5B:
                    raise ValueError(f"mismatched JSON array close at {position}")
                stack.pop()
            position += 1
        if stack:
            raise ValueError(f"unterminated compound JSON value at {start}")
        return position
    while position < bound and data[position] not in b",]} \t\r\n":
        position += 1
    return position


def array_items(data: ByteView, array_span: Span) -> Iterator[Span]:
    position = skip_ws(data, array_span.start, array_span.end)
    if data[position] != 0x5B:
        raise ValueError(f"expected array at {position}")
    position += 1
    while True:
        position = skip_ws(data, position, array_span.end)
        if position >= array_span.end:
            raise ValueError("unterminated array")
        if data[position] == 0x5D:
            return
        end = value_end(data, position, array_span.end)
        yield Span(position, end)
        position = skip_ws(data, end, array_span.end)
        if data[position] == 0x2C:
            position += 1
            continue
        if data[position] == 0x5D:
            return
        raise ValueError(f"expected array separator at {position}")


def object_members(data: ByteView, object_span: Span) -> Iterator[tuple[bytes, Span]]:
    position = skip_ws(data, object_span.start, object_span.end)
    if data[position] != 0x7B:
        raise ValueError(f"expected object at {position}")
    position += 1
    while True:
        position = skip_ws(data, position, object_span.end)
        if data[position] == 0x7D:
            return
        key_end = string_end(data, position, object_span.end)
        key = bytes(data[position + 1 : key_end - 1])
        position = skip_ws(data, key_end, object_span.end)
        if data[position] != 0x3A:
            raise ValueError(f"expected key/value separator at {position}")
        value_start = skip_ws(data, position + 1, object_span.end)
        end = value_end(data, value_start, object_span.end)
        yield key, Span(value_start, end)
        position = skip_ws(data, end, object_span.end)
        if data[position] == 0x2C:
            position += 1
            continue
        if data[position] == 0x7D:
            return
        raise ValueError(f"expected object separator at {position}")


def member_span(data: ByteView, object_span: Span, key: bytes) -> Span:
    for observed_key, span in object_members(data, object_span):
        if observed_key == key:
            return span
    raise KeyError(key.decode("utf-8", errors="replace"))


def top_level_array_span(data: mmap) -> Span:
    start = skip_ws(data, 0)
    end = value_end(data, start)
    if data[start] != 0x5B or skip_ws(data, end) != len(data):
        raise ValueError("source file must contain exactly one top-level JSON array")
    return Span(start, end)
