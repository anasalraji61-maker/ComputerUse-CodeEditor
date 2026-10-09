"""الواجهة الموحدة لمزودي النماذج (ModelProvider)."""
from abc import ABC, abstractmethod


class ModelProvider(ABC):
    """واجهة موحدة لجميع مزودي نماذج الذكاء الاصطناعي (Fake, Anthropic, Gemini)."""

    @abstractmethod
    def generate(self, prompt: str, system_prompt: str = "") -> str:
        """توليد نص بناءً على البرومت والبرومت التوجيهي."""
        pass

    @abstractmethod
    def name(self) -> str:
        """اسم المزود."""
        pass
