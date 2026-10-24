"""Send benchmark sessions and queries to an AML-compatible memory service."""
import json
import os
from datetime import datetime, timezone

from .base import MemoryAdapter
from ..runtime.artifacts import contract_hash
from ..schemas import Evidence


class Adapter(MemoryAdapter):
    def __init__(self, *args):
        super().__init__(*args)
        from aml_sdk.client import MemoryClient
        self.client = MemoryClient(self.config["base_url"],
            auth_scheme=self.config.get("auth_scheme", "bearer"),
            token=os.environ.get(self.config.get("token_env", "AML_MEMORY_API_KEY")),
            timeout=self.config.get("timeout", 1800))
        self.add_sequence = 0

    def add(self, session):
        sequence = self.add_sequence + 1
        messages = []
        for message in session.messages:
            value = {"role": message.role, "content": message.text}
            date = message.timestamp or session.timestamp
            if date is not None:
                when = datetime.fromisoformat(date)
                if when.tzinfo is None:
                    when = when.replace(tzinfo=timezone.utc)
                value["timestamp"] = int(when.timestamp() * 1000)
            messages.append(value)
        # The sequence, not session_id alone, distinguishes repeated session
        # chunks. A restarted frozen history replays the same logical IDs.
        request_id = "benchmark:" + contract_hash([self.scope, sequence, session.session_id])
        self.client.add({"request_id": request_id, "user_id": self.scope,
                         "session_id": session.session_id, "messages": messages})
        self.add_sequence = sequence

    def finalize(self):
        pass

    def search(self, query, top_k):
        if type(top_k) is not int or top_k <= 0:
            raise ValueError("AML HTTP requires a positive integer top_k; null remains a research-mode setting")
        request = {"query": query.text, "user_id": self.scope, "top_k": top_k}
        if query.all_options:
            options = json.loads(query.all_options) if isinstance(query.all_options, str) else query.all_options
            if not isinstance(options, list) or not all(isinstance(option, str) for option in options):
                raise ValueError("HTTP choices require an explicit JSON string array")
            request["options"] = options
        return [Evidence(item.id, item.content) for item in self.client.search(request).data]

    def close(self):
        self.client.close()
