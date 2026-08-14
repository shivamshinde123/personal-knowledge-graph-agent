"""LLM provider layer: the abstraction every LLM call in the system goes through.

``base.ProviderInterface`` is the sole contract; ``local_provider`` (Ollama) and
``openrouter_provider`` implement it. No other layer may import a concrete
provider or call an LLM SDK directly.
"""
