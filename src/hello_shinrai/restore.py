"""Exact local restoration, derived from the MIT-licensed shinrai-connect client."""

from __future__ import annotations


class Restorer:
    """Restore replacements even when a replacement is split across stream chunks."""

    def __init__(self, mapping: dict[str, str]):
        if any(not original or not replacement or original == replacement for original, replacement in mapping.items()):
            raise ValueError("Invalid restoration map")
        if len(set(mapping.values())) != len(mapping):
            raise ValueError("Restoration requires unique replacements")
        self.reverse = {replacement: original for original, replacement in mapping.items()}
        self.keys = sorted(self.reverse, key=len, reverse=True)
        self.buffer = ""

    def feed(self, chunk: str, *, final: bool = False) -> str:
        self.buffer += chunk
        output: list[str] = []
        index = 0
        while index < len(self.buffer):
            tail = self.buffer[index:]
            if not final and any(key.startswith(tail) and key != tail for key in self.keys):
                break
            match = next((key for key in self.keys if tail.startswith(key)), None)
            if match:
                output.append(self.reverse[match])
                index += len(match)
            else:
                output.append(self.buffer[index])
                index += 1
        self.buffer = self.buffer[index:]
        return "".join(output)

    def finish(self) -> str:
        return self.feed("", final=True)


def restore(text: str, mapping: dict[str, str]) -> str:
    return Restorer(mapping).feed(text, final=True)
