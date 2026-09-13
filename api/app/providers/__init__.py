"""The two things Footnote buys in: embeddings and a language model.

Each has a real implementation and a deterministic one, behind the same
protocol. The deterministic ones are not mocks -- they produce plausible
answers and vectors without a network -- so the rest of the application, and
its tests, never need to know which is wired in.

Import the modules, not the factories: ``llm.get_llm_provider()`` is looked
up at call time so that tests can swap it.
"""
