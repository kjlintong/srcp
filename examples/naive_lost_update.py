"""Negative control: what happens without any coordination layer.

Two agents read the same file, then both write back. One update disappears.
"""

from __future__ import annotations

import pathlib
import tempfile
import threading


def main() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path = pathlib.Path(directory) / "plan.md"
        path.write_text("v0", encoding="utf-8")
        barrier = threading.Barrier(2)

        def agent(name: str, payload: str) -> None:
            observed = path.read_text(encoding="utf-8")  # both observe v0
            barrier.wait()
            path.write_text(observed + payload, encoding="utf-8")
            print(f"[{name}] wrote based on {observed!r}")

        threads = [
            threading.Thread(target=agent, args=("alice", " + A")),
            threading.Thread(target=agent, args=("bob", " + B")),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        final = path.read_text(encoding="utf-8")
        print(f"final = {final!r}")
        lost = "A" if "A" not in final else ("B" if "B" not in final else None)
        print(f"lost update = {lost!r}  <-- unsafe event")


if __name__ == "__main__":
    main()
