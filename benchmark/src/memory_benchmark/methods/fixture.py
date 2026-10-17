from .base import MemoryAdapter
from ..schemas import Evidence


class Adapter(MemoryAdapter):
    def __init__(self, *args):
        super().__init__(*args)
        self.memories = []
        self.finalized = False

    def add(self, session):
        if self.finalized:
            raise ValueError("Cannot write after finalize")
        self.memories.extend(Evidence(m.source_id, f"{m.speaker}: {m.text}", (m.source_id,),
                                     m.timestamp) for m in session.messages)

    def finalize(self):
        self.finalized = True

    def search(self, query, top_k):
        if not self.finalized:
            raise ValueError("Search before finalize")
        return self.memories[:top_k]
