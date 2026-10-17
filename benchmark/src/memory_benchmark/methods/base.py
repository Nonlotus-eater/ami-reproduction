from abc import ABC, abstractmethod


class MemoryAdapter(ABC):
    def __init__(self, config, history, output, scope, gateway):
        self.config, self.history, self.output = config, history, output
        self.scope, self.gateway = scope, gateway

    @abstractmethod
    def add(self, session):
        pass

    @abstractmethod
    def finalize(self):
        pass

    @abstractmethod
    def search(self, query, top_k):
        pass

    def close(self):
        pass
