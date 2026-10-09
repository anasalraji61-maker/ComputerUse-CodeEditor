"""حزمة مزودي النماذج."""
from .base import ModelProvider
from .fake import FakeProvider

__all__ = ["ModelProvider", "FakeProvider"]
